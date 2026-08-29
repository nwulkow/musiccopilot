"""Gemini integration: stylistic listening notes and guitar-solo generation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .config import (GEMINI_CLEAN_MODEL, GEMINI_MODEL, LLM_CLEAN_MAX_NOTES,
                     LLM_CLEAN_MAX_OUTPUT, LLM_CLEAN_MAX_SECONDS,
                     LLM_CLEAN_THINKING, LLM_NOTES_MAX_OUTPUT,
                     LLM_SOLO_MAX_OUTPUT, LLM_SOLO_THINKING, gemini_api_key)
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


CLEAN_SYSTEM = """You are a session guitarist cleaning up a machine transcription of a
recorded solo. The note list you are given was produced by an automatic pitch
tracker and is NOT a performance to preserve exactly - it is evidence of one,
and it has known failure modes:
- Pitch jitter chops one held or bent note into several short adjacent notes
  at nearly the same pitch. Merge these into the single note a player actually
  fretted, choosing the technique (bend/slide/hammer/pull/vibrato) that best
  explains the pitch movement across the fragments.
- The tracker occasionally fires a spurious grace note a semitone or two off
  right before or after a real note (background noise, string noise, bleed
  from another instrument). Drop these; they are not something a guitarist
  played on purpose.
- Extremely short notes (under ~1/16 of a beat) that do not connect two real
  pitches are almost always noise, not a fast lick - remove them unless the
  surrounding notes clearly form a fast, deliberate run.
- Octave errors (a note an octave away from its neighbours with otherwise
  identical timing) should be corrected to the octave that fits the phrase.

What you must NOT do: do not compose a new solo, do not add notes that were
not there in some form, do not "improve" the phrasing beyond removing
artifacts, and do not change the overall contour or rhythm of the line. The
result should sound like the same recorded performance, just transcribed the
way a competent human transcriber would have written it - fewer, cleaner
notes, real techniques instead of pitch staircases. If a note already looks
clean, pass it through unchanged.

Timing units match the input: `beat` is beats from the start of the window
given to you (0.0 = its downbeat)."""


class CleanedSolo(BaseModel):
    """Structured response from `clean_solo` - a decluttered version of the input notes."""

    changes: str = Field(description="1-3 sentences on what was merged, dropped, or fixed")
    notes: list[SoloNote]


def _client():
    """Build a genai Client, importing google.genai lazily (see CLAUDE.md's lazy-imports note)."""
    from google import genai
    key = gemini_api_key()
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in your environment.")
    return genai.Client(api_key=key)


def _config(**kw):
    """Thin wrapper around `types.GenerateContentConfig` so callers don't import google.genai.

    Explicitly disables automatic function calling: nothing here passes `tools`,
    but leaving AFC on its default makes `generate_content` log a one-time
    "use Chat.send_message instead" warning on every process regardless, since
    the SDK checks the AFC config before it checks whether any tools exist.
    """
    from google.genai import types
    kw.setdefault("automatic_function_calling",
                  types.AutomaticFunctionCallingConfig(disable=True))
    if (budget := kw.pop("thinking_budget", None)) is not None:
        kw["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
    return types.GenerateContentConfig(**kw)


class TooLongToClean(ValueError):
    """Raised when a cleanup window is past what the button is allowed to cost.

    Carries the measured size so a caller can say *how far* over the request
    is - "the whole song" and "one bar too many" want different advice.
    """

    def __init__(self, notes: int, seconds: float):
        self.notes, self.seconds = notes, seconds
        over = ("the whole song" if notes > 4 * LLM_CLEAN_MAX_NOTES else "this passage")
        super().__init__(
            f"{over} is too long to clean: {notes} notes over {seconds:.0f}s "
            f"(limit {LLM_CLEAN_MAX_NOTES} notes / {LLM_CLEAN_MAX_SECONDS:.0f}s). "
            f"Pick a part or a bar range and clean that."
        )


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
        f"Write a high quality, playable, realistic, expressive guitar solo for this section.\n\n"
        f"Musical context:\n{section_context(analysis, start, end, extra)}\n\n"
        f"What the player asked for: {prompt}\n\n"
        f"Fill roughly the whole section. Return JSON only."
    )
    # Bind the client instead of chaining off `_client()` directly: an unnamed
    # client can be garbage-collected mid-request (tenacity retries reuse its
    # httpx client across attempts), which surfaces as a misleading "client
    # has been closed" error instead of the real one.
    client = _client()
    resp = client.models.generate_content(
        model=model, contents=contents,
        config=_config(system_instruction=SOLO_SYSTEM, temperature=temperature,
                       response_mime_type="application/json", response_schema=Solo,
                       thinking_budget=LLM_SOLO_THINKING,
                       max_output_tokens=LLM_SOLO_MAX_OUTPUT),
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


def notes_to_solonotes(notes: list[Note], tempo: float, t0: float = 0.0) -> list[SoloNote]:
    """Inverse of `solo_to_notes`: absolute-time `Note`s -> beat-relative units,
    so a transcribed window can be handed to Gemini in the same shape it writes.

    Everything is rounded, because these numbers are *sent* and every digit is
    a billed token. A raw `(n.start - t0) * bps` prints as
    `14.341232328869047` - 17 significant figures of a float that came from a
    10ms CREPE frame in the first place. Three decimals of a beat is 1.5ms at
    123bpm, well inside the transcriber's own resolution, and it halves the
    payload (13.4k chars -> 7.5k on crystallize's guitar solo).
    """
    bps = tempo / 60.0
    return [
        SoloNote(
            beat=round((n.start - t0) * bps, 3),
            duration=round(max(0.05, n.duration) * bps, 3),
            midi=n.pitch,
            technique=n.technique if n.technique in TECHNIQUES.__args__ else "normal",
            bend_semitones=round(float(n.bend), 2),
            velocity=round(float(min(1.0, max(0.2, n.velocity))), 2),
        )
        for n in sorted(notes, key=lambda n: n.start)
    ]


def clean_window_cost(notes: list[Note], start: float, end: float) -> tuple[bool, int, float]:
    """Whether `clean_solo` will accept this window, and the size that decided it.

    Split out from `clean_solo` so a caller can ask *before* spending anything -
    the web front end greys the button out and the CLI refuses up front, rather
    than both discovering the limit from a failed job. Enforcement still lives
    in `clean_solo`; this only answers the same question early.
    """
    n, secs = len(notes), max(0.0, end - start)
    return (n <= LLM_CLEAN_MAX_NOTES and secs <= LLM_CLEAN_MAX_SECONDS), n, secs


def clean_solo(notes: list[Note], analysis, start: float, end: float,
              model: str = GEMINI_CLEAN_MODEL, temperature: float = 0.2) -> CleanedSolo:
    """Ask Gemini to declutter a transcribed note window into a realistic tab.

    Machine transcription over-segments: pitch jitter on a held or bent note
    becomes several near-duplicate notes, and the noise floor left behind by
    stem separation fires spurious grace notes. This asks the model to merge
    and prune those artifacts back into what a guitarist actually played,
    without composing anything new - see `CLEAN_SYSTEM`. Low temperature by
    default: this is cleanup, not composition, so it should be the least
    surprising pass over the data that still fixes the obvious mess.

    **This is a snippet operation, and the limit is enforced here** rather than
    at each caller, so the browser and the terminal cannot disagree about what
    is too big (the same reason `app._window` calls `cli._window`). The shape
    of the call is why: the model echoes the window back, so the notes are
    billed twice and thinking is billed on top of that. A part is ~75 notes; a
    whole song is ~1400, and asking for one is asking for a hundred times the
    bill of the thing the button is for. See `TooLongToClean`.

    The payload is compact JSON, not `indent=2`: the indentation was about a
    fifth of the tokens and the model never reads it.
    """
    ok, n, secs = clean_window_cost(notes, start, end)
    if not ok:
        raise TooLongToClean(n, secs)

    solo_notes = notes_to_solonotes(notes, analysis.tempo, t0=start)
    contents = (
        f"Clean up this transcribed solo.\n\n"
        f"Musical context:\n{section_context(analysis, start, end)}\n\n"
        f"Transcribed notes (machine output, {len(solo_notes)} notes):\n"
        f"{json.dumps([n.model_dump() for n in solo_notes], separators=(',', ':'))}\n\n"
        f"Return the cleaned note list as JSON only."
    )
    client = _client()
    resp = client.models.generate_content(
        model=model, contents=contents,
        config=_config(system_instruction=CLEAN_SYSTEM, temperature=temperature,
                       response_mime_type="application/json", response_schema=CleanedSolo,
                       thinking_budget=LLM_CLEAN_THINKING,
                       max_output_tokens=LLM_CLEAN_MAX_OUTPUT),
    )
    return resp.parsed if resp.parsed else CleanedSolo.model_validate_json(resp.text)


def listening_notes(audio_path: str | Path, analysis, model: str = GEMINI_MODEL) -> str:
    """Have Gemini listen to the track and describe its musical patterns.

    The only call here that sends audio, and therefore the only one whose cost
    is set by the length of the song rather than the size of a passage: Gemini
    bills audio at a flat rate per second, so a 4-minute track is ~8.5k input
    tokens before a word of prompt. That is once per song and it is cached in
    `llm_notes.txt`, which is why this is opt-in (`analyze --llm`) and not part
    of a plain analysis - and why the web front end asks first.

    The uploaded file is deleted afterwards. Files API storage expires by
    itself after 48h, but leaving every song ever analysed sitting in the
    project's quota serves nobody.
    """
    client = _client()
    f = client.files.upload(file=str(audio_path))
    contents = [f, (
        "Describe this song's musical patterns for a musician who wants to play "
        "along. Cover: groove and feel, song form, harmonic devices, guitar/bass "
        "roles and tone, notable riffs or hooks with rough timestamps, and which "
        "scales fit for soloing. Be specific and concise (max ~250 words).\n\n"
        f"Automated analysis so far:\n{json.dumps({'key': analysis.key, 'tempo': round(analysis.tempo, 1), 'progression': analysis.progression(0, analysis.duration)[:24]}, indent=2)}"
    )]
    try:
        return client.models.generate_content(
            model=model, contents=contents,
            config=_config(temperature=0.4, thinking_budget=LLM_CLEAN_THINKING,
                           max_output_tokens=LLM_NOTES_MAX_OUTPUT),
        ).text.strip()
    finally:
        try:
            client.files.delete(name=f.name)
        except Exception:                                 # noqa: BLE001
            pass          # a leftover upload expires on its own; never fail the run for it
