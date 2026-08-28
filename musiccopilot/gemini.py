"""Gemini integration: stylistic listening notes and guitar-solo generation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .config import GEMINI_MODEL, gemini_api_key
from .notes import Note

TECHNIQUES = Literal["normal", "bend", "slide", "hammer", "pull", "vibrato", "palm_mute"]

SOLO_SYSTEM = """You are a session guitarist and transcriber.
You write solos as note data. Rules:
- `beat` is measured in beats from the start of the solo section (0.0 = downbeat).
- `midi` must be 40-88 (guitar range, E2-E6). Stay mostly in one or two positions.
- Notes may overlap slightly for legato, but the line is monophonic.
- Use rests: not every beat needs a note. Phrase in 2- or 4-bar sentences,
  leave breathing space, and land strong chord tones on downbeats.
- Use `bend` (with bend_semitones 1.0 or 2.0), `vibrato` on long notes, and
  hammer/pull/slide for fast legato runs. Set bend_semitones to 0 otherwise.
- Respect the key, the chord changes and the requested character."""


class SoloNote(BaseModel):
    """One generated note, in the beat-relative units Gemini reasons in.

    `solo_to_notes` converts these to absolute-time `Note`s.
    """

    beat: float = Field(description="start, in beats from the section start")
    duration: float = Field(description="length in beats")
    midi: int = Field(description="MIDI pitch, 40-88")
    technique: TECHNIQUES
    bend_semitones: float = Field(description="0 unless technique is bend")
    velocity: float = Field(description="0.2 quiet to 1.0 accented")


class Solo(BaseModel):
    """The full structured response from `suggest_solo` - passed to Gemini as `response_schema`."""

    title: str
    scale: str = Field(description="e.g. 'A minor pentatonic with added b5'")
    explanation: str = Field(description="2-4 sentences on the phrasing choices")
    notes: list[SoloNote]


def _client():
    """Build a genai Client, importing google.genai lazily (see CLAUDE.md's lazy-imports note)."""
    from google import genai
    key = gemini_api_key()
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in your environment.")
    return genai.Client(api_key=key)


def _config(**kw):
    """Thin wrapper around `types.GenerateContentConfig` so callers don't import google.genai."""
    from google.genai import types
    return types.GenerateContentConfig(**kw)


def section_context(analysis, start: float, end: float, extra: dict | None = None) -> str:
    """Everything the model needs to know about the slot it is soloing over."""
    ctx = {
        "key": analysis.key,
        "tempo_bpm": round(analysis.tempo, 1),
        "beats_per_bar": analysis.beats_per_bar,
        "section_seconds": [round(start, 2), round(end, 2)],
        "bars": round((end - start) * analysis.tempo / 60 / analysis.beats_per_bar, 1),
        "chord_progression": analysis.progression(start, end),
        "chords_timed": [{"t": round(c.start - start, 2), "chord": c.name}
                         for c in analysis.chords if c.end > start and c.start < end][:64],
    }
    ctx.update(extra or {})
    return json.dumps(ctx, indent=2)


def suggest_solo(prompt: str, analysis, start: float, end: float,
                 extra: dict | None = None, model: str = GEMINI_MODEL,
                 temperature: float = 1.0) -> Solo:
    """Ask Gemini for a solo over a section, returned as structured note data."""
    contents = (
        f"Write a guitar solo for this section.\n\n"
        f"Musical context:\n{section_context(analysis, start, end, extra)}\n\n"
        f"What the player asked for: {prompt}\n\n"
        f"Fill roughly the whole section. Return JSON only."
    )
    resp = _client().models.generate_content(
        model=model, contents=contents,
        config=_config(system_instruction=SOLO_SYSTEM, temperature=temperature,
                       response_mime_type="application/json", response_schema=Solo),
    )
    return resp.parsed if resp.parsed else Solo.model_validate_json(resp.text)


def solo_to_notes(solo: Solo, tempo: float, t0: float = 0.0) -> list[Note]:
    """Convert beat-relative LLM output into absolute-time playable notes."""
    spb = 60.0 / tempo
    out = []
    for n in sorted(solo.notes, key=lambda n: n.beat):
        start = t0 + n.beat * spb
        out.append(Note(
            start=start,
            end=start + max(0.05, n.duration * spb),
            pitch=int(min(88, max(40, n.midi))),
            velocity=float(min(1.0, max(0.2, n.velocity))),
            technique=n.technique,
            bend=float(n.bend_semitones),
        ))
    return out


def listening_notes(audio_path: str | Path, analysis, model: str = GEMINI_MODEL) -> str:
    """Have Gemini listen to the track and describe its musical patterns."""
    client = _client()
    f = client.files.upload(file=str(audio_path))
    contents = [f, (
        "Describe this song's musical patterns for a musician who wants to play "
        "along. Cover: groove and feel, song form, harmonic devices, guitar/bass "
        "roles and tone, notable riffs or hooks with rough timestamps, and which "
        "scales fit for soloing. Be specific and concise (max ~250 words).\n\n"
        f"Automated analysis so far:\n{json.dumps({'key': analysis.key, 'tempo': round(analysis.tempo, 1), 'progression': analysis.progression(0, analysis.duration)[:24]}, indent=2)}"
    )]
    return client.models.generate_content(
        model=model, contents=contents,
        config=_config(temperature=0.4),
    ).text.strip()
