"""A small guitar/bass synthesiser so generated parts can actually be heard.

Additive oscillator driven by an instantaneous-frequency curve, which makes
bends, slides and vibrato trivial, then amp shaping (drive + cabinet filter +
delay/reverb).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve, lfilter

from .config import SR
from .notes import Note

PRESETS = {
    # harmonics, harmonic rolloff, drive, decay (s), reverb, delay
    "lead":  dict(n_harm=14, rolloff=1.1, drive=4.0, decay=2.2, reverb=0.25, delay=0.18),
    "clean": dict(n_harm=10, rolloff=1.9, drive=1.2, decay=1.4, reverb=0.18, delay=0.0),
    "bass":  dict(n_harm=8, rolloff=1.5, drive=1.6, decay=1.1, reverb=0.05, delay=0.0),
}


def _freq_curve(note: Note, n: int, sr: int, prev_pitch: int | None) -> np.ndarray:
    """MIDI pitch over the life of the note, including articulation."""
    p = np.full(n, float(note.pitch))
    t = np.linspace(0, 1, n, endpoint=False)

    if note.technique == "bend" and note.bend:
        p += note.bend * np.clip(t / 0.35, 0, 1)               # bend up, then hold
    elif note.technique == "slide" and prev_pitch is not None:
        p = prev_pitch + (note.pitch - prev_pitch) * np.clip(t / 0.18, 0, 1)
    if note.technique == "vibrato":
        p += 0.3 * np.sin(2 * np.pi * 5.5 * np.arange(n) / sr) * np.clip((t - 0.25) / 0.2, 0, 1)

    return 440.0 * 2 ** ((p - 69.0) / 12.0)


def _envelope(n: int, sr: int, technique: str, decay: float) -> np.ndarray:
    """Amplitude shape for one note: fast attack, exponential decay, short fade-out.

    Legato techniques get a slower attack so the pick transient doesn't show
    up on a hammer/pull/slide, and a palm mute shortens the decay instead of
    changing the attack.
    """
    attack = int(sr * (0.02 if technique in ("hammer", "pull", "slide") else 0.004))
    if technique == "palm_mute":
        decay = 0.28
    env = np.exp(-np.arange(n) / (decay * sr))
    env[:attack] *= np.linspace(0, 1, attack)
    env[-min(n, int(sr * 0.02)):] *= np.linspace(1, 0, min(n, int(sr * 0.02)))
    return env


def render_note(note: Note, sr: int = SR, preset: str = "lead",
                prev_pitch: int | None = None) -> np.ndarray:
    """Synthesise one note as a dry (pre-amp) additive-harmonics signal.

    `prev_pitch` is only used for a slide's starting pitch. Harmonics above
    Nyquist are dropped rather than aliased.
    """
    cfg = PRESETS[preset]
    n = max(64, int(note.duration * sr))
    freq = _freq_curve(note, n, sr, prev_pitch)
    phase = 2 * np.pi * np.cumsum(freq) / sr

    y = np.zeros(n)
    for k in range(1, cfg["n_harm"] + 1):
        if (freq * k).max() > sr / 2:                          # anti-alias
            break
        y += (1.0 / k ** cfg["rolloff"]) * np.sin(k * phase + k * 0.7)
    return y * _envelope(n, sr, note.technique, cfg["decay"]) * note.velocity


def _amp(y: np.ndarray, sr: int, cfg: dict) -> np.ndarray:
    """Amp chain: normalise -> overdrive -> cabinet -> delay -> reverb."""
    if (peak := np.abs(y).max()) > 0:
        y = y / peak                     # so `drive` means the same thing always
    y = np.tanh(cfg["drive"] * y) / np.tanh(cfg["drive"])       # overdrive
    y = lfilter(*_lowpass(3800, sr), y)                         # speaker cabinet
    y = lfilter(*_highpass(85, sr), y)
    if cfg["delay"]:
        dry, d = y.copy(), int(cfg["delay"] * sr)
        for i, g in enumerate((0.32, 0.16, 0.07), start=1):
            y[i * d:] += g * dry[: -i * d]
    if cfg["reverb"]:
        n = int(0.7 * sr)
        ir = np.random.randn(n) * np.exp(-np.arange(n) / (0.22 * sr))
        y = y + cfg["reverb"] * fftconvolve(y, ir / np.sqrt((ir ** 2).sum()), mode="same")
    return _normalize(y)


def _normalize(y: np.ndarray, peak: float = 0.9) -> np.ndarray:
    """Scale so the loudest sample hits `peak`; leaves silence untouched."""
    m = np.abs(y).max()
    return y / m * peak if m > 0 else y


def _lowpass(fc: float, sr: int):
    """One-pole IIR lowpass at `fc` Hz, as (b, a) coefficients for `lfilter`."""
    a = np.exp(-2 * np.pi * fc / sr)
    return [1 - a], [1, -a]


def _highpass(fc: float, sr: int):
    """One-pole IIR highpass at `fc` Hz, as (b, a) coefficients for `lfilter`."""
    a = np.exp(-2 * np.pi * fc / sr)
    return [(1 + a) / 2, -(1 + a) / 2], [1, -a]


def render(notes: list[Note], sr: int = SR, preset: str = "lead",
           duration: float | None = None, t0: float = 0.0) -> np.ndarray:
    """Render a note list to mono audio, starting the timeline at `t0`."""
    if not notes:
        return np.zeros(int(sr * (duration or 1.0)))
    end = duration if duration is not None else max(n.end for n in notes) - t0 + 2.0
    out = np.zeros(int(sr * end) + sr)

    prev = None
    for note in sorted(notes, key=lambda n: n.start):
        sig = render_note(note, sr, preset, prev)
        i = int((note.start - t0) * sr)
        if i < 0:
            sig, i = sig[-i:], 0
        out[i:i + len(sig)] += sig[: len(out) - i]
        prev = note.pitch
    return _amp(out, sr, PRESETS[preset])


def render_chords(chords, sr: int = SR, strum: float = 0.02,
                  t0: float = 0.0, duration: float | None = None) -> np.ndarray:
    """Strum a chord track as a clean backing bed (no stems required)."""
    from .tabs import chord_voicing        # imported here to keep synth standalone

    notes: list[Note] = []
    for ch in chords:
        if ch.root < 0:
            continue
        for i, pitch in enumerate(chord_voicing(ch.name, ch.root, ch.quality)):
            notes.append(Note(ch.start + i * strum,        # strum, not a block chord
                              min(ch.end, ch.start + 2.0), pitch, 0.35))
    return render(notes, sr, "clean", duration, t0)


def mix(*tracks: np.ndarray, gains: list[float] | None = None,
        normalize: float = 0.9) -> np.ndarray:
    """Sum tracks of possibly different lengths (padding the shorter ones with
    silence) and normalise the result to `normalize` peak."""
    tracks = [t for t in tracks if t is not None and t.size]
    if not tracks:
        return np.zeros(1)
    gains = gains or [1.0] * len(tracks)
    n = max(len(t) for t in tracks)
    out = np.zeros(n)
    for t, g in zip(tracks, gains):
        out[: len(t)] += g * t
    peak = np.abs(out).max()
    return out / peak * normalize if peak > 0 else out


def click_track(beats: int, tempo: float, sr: int = SR,
                beats_per_bar: int = 4, accent_first: bool = True) -> np.ndarray:
    """A count-in click: a short pitched blip on each beat.

    The downbeat is a fifth higher so a bar's worth of clicks tells you where
    beat one is - counting in "1 2 3 4" is useless if all four sound alike.
    """
    spb = 60.0 / max(tempo, 1e-6)
    # Exactly `beats` beats long: the click track is prepended to the music, so
    # any padding after the last blip delays the downbeat by that much and the
    # count-in stops counting you in. The last blip's decay is simply clipped -
    # it is a click, and the music arriving is what should cut it off anyway.
    y = np.zeros(int(round(beats * spb * sr)), dtype=np.float32)
    for b in range(beats):
        f = 1600.0 if (accent_first and b % beats_per_bar == 0) else 1050.0
        n = int(0.035 * sr)
        t = np.arange(n) / sr
        blip = np.sin(2 * np.pi * f * t) * np.exp(-t * 55.0)
        i = int(round(b * spb * sr))
        fit = min(n, len(y) - i)
        y[i:i + fit] += blip[:fit].astype(np.float32) * 0.5
    return y


def write(path: str | Path, y: np.ndarray, sr: int = SR) -> Path:
    """Write audio to `path` as a wav, clipping to [-1, 1] and creating parent dirs."""
    import soundfile as sf
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(y, -1, 1), sr)
    return Path(path)


def play(path: str | Path) -> None:
    """Play a wav through the OS default player (best effort)."""
    import platform
    import subprocess
    cmd = {"Darwin": ["afplay"], "Linux": ["aplay"], "Windows": ["cmd", "/c", "start", ""]}
    try:
        subprocess.run(cmd[platform.system()] + [str(path)], check=False)
    except (KeyError, FileNotFoundError):
        print(f"Play it yourself: {path}")
