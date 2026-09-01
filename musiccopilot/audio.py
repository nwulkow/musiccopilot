"""Audio loading and stem separation (Demucs)."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from .config import SR, base_stem, workdir_for

# htdemucs_6s splits into these; the plain htdemucs model has no guitar/piano.
SIX_STEMS = ["drums", "bass", "other", "vocals", "guitar", "piano"]

# A separated stem is 16-bit stereo WAV by nature, but nothing downstream reads
# it that way - every transcriber loads mono, CREPE resamples to 16k, Basic
# Pitch to 22.05k. AAC has hardware decode on Apple silicon (lower battery cost
# than Opus when several stems decode at once under a play-along) and, at 64
# kbps, transcription and chord detection land at or above the floor of simply
# re-running the analysis on the uncompressed file - see docs/IOS_PORT.md §9
# for the measurements this constant is chosen from. Do not lower the bitrate
# without re-measuring against that ground truth.
STEM_EXT = ".m4a"
STEM_BITRATE = "64k"


def load(path: str | Path, sr: int = SR, mono: bool = True) -> np.ndarray:
    """Decode an audio file, resampled to `sr` (needs ffmpeg on PATH for mp3/m4a)."""
    y, _ = librosa.load(str(path), sr=sr, mono=mono)
    return y


def save(path: str | Path, y: np.ndarray, sr: int = SR) -> Path:
    """Write a (mono or stereo) signal, creating parent dirs as needed.

    The container is whatever `path`'s suffix says: `.wav` (or anything else
    libsndfile writes) goes straight through soundfile, `STEM_EXT` goes through
    ffmpeg as AAC. A stem is always written mono here even if `y` is stereo -
    the one exception is `save_stem`, which keeps the channels `voices.py`
    needs its pan cue from.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == STEM_EXT:
        return _save_lossy(path, y, sr)
    sf.write(str(path), y.T if y.ndim > 1 else y, sr)
    return path


def save_stem(path: str | Path, y: np.ndarray, sr: int = SR, *,
              stereo: bool = False) -> Path:
    """Write a separated stem: mono unless `stereo` (only `voices.py`'s
    candidates need their pan preserved, per config.VOICES["stems"])."""
    if y.ndim > 1 and not stereo:
        y = y.mean(axis=0)
    return save(path, y, sr)


def _save_lossy(path: Path, y: np.ndarray, sr: int) -> Path:
    """AAC via ffmpeg: libsndfile has no AAC encoder, so this shells out rather
    than adding a second audio dependency for one format."""
    channels = 2 if y.ndim > 1 else 1
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        sf.write(tmp.name, y.T if y.ndim > 1 else y, sr)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp.name,
             "-ac", str(channels), "-c:a", "aac", "-b:a", STEM_BITRATE, str(path)],
            check=True)
    return path


def _device(preferred: str | None = None) -> str:
    """Best available torch device: explicit choice, else cuda/mps/cpu in that order."""
    import torch
    if preferred:
        return preferred
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def separate(path: str | Path, model: str = "htdemucs_6s",
             device: str | None = None, force: bool = False) -> dict[str, Path]:
    """Split a song into instrument stems. Cached: reruns are free.

    Returns {stem_name: wav_path}. Falls back to CPU if the accelerator chokes.
    """
    from .config import VOICES

    out = workdir_for(path) / "stems"
    out.mkdir(parents=True, exist_ok=True)
    # Matches a cache from before stem compression (`.wav`) as well as
    # `STEM_EXT`, the same as `Song.open` - a song is not re-separated just
    # because the default format changed since it was last analysed.
    cached = {p.stem: p for p in out.glob("*.wav")}
    cached.update({p.stem: p for p in out.glob(f"*{STEM_EXT}")})
    if cached and not force:
        return cached

    from demucs.api import Separator

    def _run(dev: str):
        """Separate on `dev` and write each stem straight to `out`."""
        sep = Separator(model=model, device=dev, progress=True)
        _, stems = sep.separate_audio_file(Path(path))
        for name, source in stems.items():
            # `demucs.audio.prevent_clip`'s own rescale strategy, replicated
            # here rather than imported so this stays a numpy boundary:
            # separation can produce peaks a hair over full scale.
            y = source.numpy()
            y = y / max(1.01 * float(np.abs(y).max()), 1.0)
            save_stem(out / f"{name}{STEM_EXT}", y, sep.samplerate,
                      stereo=base_stem(name) in VOICES["stems"])

    dev = _device(device)
    try:
        _run(dev)
    except Exception as exc:                       # noqa: BLE001 - mps/cuda quirks
        if dev == "cpu":
            raise
        print(f"[separate] {dev} failed ({exc}); retrying on cpu")
        _run("cpu")
    return {p.stem: p for p in out.glob(f"*{STEM_EXT}")}


def excerpt(y: np.ndarray, start: float, end: float, sr: int = SR,
            fade: float = 0.02) -> np.ndarray:
    """A time slice of a (mono or stereo) signal, faded so it does not click."""
    a, b = max(0, int(start * sr)), int(end * sr)
    seg = np.array(y[..., a:b], dtype=np.float32, copy=True)
    n = min(int(fade * sr), seg.shape[-1] // 2)
    if n:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        seg[..., :n] *= ramp
        seg[..., -n:] *= ramp[::-1]
    return seg


def mix(stem_paths: dict[str, Path], include: list[str], sr: int = SR) -> np.ndarray:
    """Sum a subset of stems back together (e.g. a backing track).

    `include` is exact stem names. Callers that mean *instruments* - "every
    guitar", however many an imported multitrack has - go through `stems_of`
    first, so that widening stays visible at the call site rather than hiding
    in here where a precise exclusion would silently be undone.
    """
    parts = [load(p, sr) for name, p in stem_paths.items() if name in include]
    if not parts:
        return np.zeros(1, dtype=np.float32)
    n = max(len(p) for p in parts)
    total = np.zeros(n, dtype=np.float32)
    for p in parts:
        total[: len(p)] += p
    return total


def stems_of(stem_paths: dict[str, Path], instruments: list[str]) -> list[str]:
    """Every stem belonging to one of `instruments` - `guitar` finds `guitar-2` too."""
    return [name for name in stem_paths if base_stem(name) in instruments]


def harmonic_bed(stem_paths: dict[str, Path], sr: int = SR) -> np.ndarray:
    """Pitched content only (no drums, no vocals) - the best input for chords."""
    return mix(stem_paths, stems_of(stem_paths, ["bass", "other", "guitar", "piano"]), sr)
