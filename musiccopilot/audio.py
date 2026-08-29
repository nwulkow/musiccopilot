"""Audio loading and stem separation (Demucs)."""
from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from .config import SR, base_stem, workdir_for

# htdemucs_6s splits into these; the plain htdemucs model has no guitar/piano.
SIX_STEMS = ["drums", "bass", "other", "vocals", "guitar", "piano"]


def load(path: str | Path, sr: int = SR, mono: bool = True) -> np.ndarray:
    """Decode an audio file, resampled to `sr` (needs ffmpeg on PATH for mp3)."""
    y, _ = librosa.load(str(path), sr=sr, mono=mono)
    return y


def save(path: str | Path, y: np.ndarray, sr: int = SR) -> Path:
    """Write a (mono or stereo) signal to a wav, creating parent dirs as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), y.T if y.ndim > 1 else y, sr)
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
    out = workdir_for(path) / "stems"
    out.mkdir(parents=True, exist_ok=True)
    cached = {p.stem: p for p in out.glob("*.wav")}
    if cached and not force:
        return cached

    from demucs.api import Separator, save_audio

    def _run(dev: str):
        """Separate on `dev` and write each stem straight to `out`."""
        sep = Separator(model=model, device=dev, progress=True)
        _, stems = sep.separate_audio_file(Path(path))
        for name, source in stems.items():
            save_audio(source, str(out / f"{name}.wav"), samplerate=sep.samplerate)

    dev = _device(device)
    try:
        _run(dev)
    except Exception as exc:                       # noqa: BLE001 - mps/cuda quirks
        if dev == "cpu":
            raise
        print(f"[separate] {dev} failed ({exc}); retrying on cpu")
        _run("cpu")
    return {p.stem: p for p in out.glob("*.wav")}


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
