"""Lyric transcription from the separated vocal stem (Whisper)."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class Line:
    """One transcribed lyric line/segment from Whisper."""
    start: float
    end: float
    text: str


def transcribe(vocal_path: str | Path, model_size: str = "base",
               language: str | None = None) -> list[Line]:
    """Run Whisper on the isolated vocals - far cleaner than on the full mix."""
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(vocal_path), language=language, vad_filter=True)
        return [Line(round(s.start, 2), round(s.end, 2), s.text.strip())
                for s in segments if s.text.strip()]
    except ImportError:
        pass

    import whisper                                  # openai-whisper fallback
    result = whisper.load_model(model_size).transcribe(str(vocal_path), language=language)
    return [Line(round(s["start"], 2), round(s["end"], 2), s["text"].strip())
            for s in result["segments"] if s["text"].strip()]


def to_dicts(lines: list[Line]) -> list[dict]:
    """JSON-ready rows for `lyrics.json`."""
    return [asdict(l) for l in lines]


def from_dicts(rows: list[dict]) -> list[Line]:
    """Inverse of `to_dicts`."""
    return [Line(**r) for r in rows]


def text(lines: list[Line]) -> str:
    """The full transcript, one line per Line, for feeding to Gemini/prompts."""
    return "\n".join(l.text for l in lines)


def in_window(lines: list[Line], start: float, end: float) -> list[Line]:
    """Lines overlapping [start, end) - used to find a part's sung words."""
    return [l for l in lines if l.end > start and l.start < end]
