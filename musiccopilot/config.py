"""Constants shared across the pipeline: tunings, chord vocabulary, paths."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SR = 44100                      # everything is resampled to this
OUTDIR = "analyzed_songs"       # per-song folder lives here, next to the audio
LEGACY_WORKDIR = ".musiccopilot"    # what OUTDIR used to be called

# --- stem names ---------------------------------------------------------------
# The six htdemucs_6s outputs are the pipeline's whole instrument vocabulary:
# they are simultaneously wav filenames, `Song.stems` keys, `--stem` values,
# `PITCH_RANGE` keys, `TRANSCRIBE_STEMS` and `LEAD_STEMS` entries. Separation
# can only ever produce one of each, so name and instrument used to be the same
# thing.
#
# An imported multitrack (`daw.py`) breaks that: a band has two guitarists and
# a singer with a backing vocal, and summing them back together would throw
# away exactly the separation that made importing worth doing. So a stem may
# now carry a `-2` suffix, and every lookup keyed by instrument goes through
# `base_stem` rather than the name itself - `guitar-2` is a guitar in each way
# that matters (pitch window, tuning, claim on being a solo's lead) while
# staying its own stem, its own notes file and its own tab.
STEM_NAMES = ("drums", "bass", "other", "vocals", "guitar", "piano")

_STEM_SUFFIX = re.compile(r"-\d+$")


def base_stem(stem: str) -> str:
    """The canonical instrument a stem name belongs to: `guitar-2` -> `guitar`.

    Anything that is not a suffixed canonical name comes back unchanged, so
    `mix` (what a song with no stems at all transcribes as) and any name a
    future importer invents still resolve to themselves and fall through the
    same `.get(name, default)` lookups they always did.
    """
    root = _STEM_SUFFIX.sub("", stem)
    return root if root in STEM_NAMES else stem

# --- Gemini -----------------------------------------------------------------
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_KEY_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

# Cleaning a transcription is mechanical - merge pitch-jitter fragments, drop
# noise-floor grace notes, fix octaves - and it runs at temperature 0.2 for
# that reason. It does not need the reasoning tier that writing a solo does,
# so it gets the cheap model. `GEMINI_CLEAN_MODEL=gemini-3.5-flash` puts it
# back on the composing model if a cleanup ever looks careless.
GEMINI_CLEAN_MODEL = os.getenv("GEMINI_CLEAN_MODEL", "gemini-3.5-flash-lite")

# --- what one Gemini button is allowed to cost ------------------------------
# `clean_solo` is the expensive shape: it echoes its input back, so the window
# is billed twice - once in, once out - and thinking tokens are billed as
# output on top. A whole-song window is not a bigger version of the intended
# request, it is a different request: crystallize's guitar stem is 1438 notes
# over the whole song (~65k tokens in, as many again out) against 75 notes for
# the guitar solo it was built for (~1.9k). These caps are what keep the
# button a snippet button; `_window`'s own "no passage means the whole song"
# on the web side is what made overshooting the default.
LLM_CLEAN_MAX_NOTES = int(os.getenv("MUSICCOPILOT_CLEAN_MAX_NOTES", "250"))
LLM_CLEAN_MAX_SECONDS = float(os.getenv("MUSICCOPILOT_CLEAN_MAX_SECONDS", "75"))

# Thinking tokens are billed as output and count against `max_output_tokens`,
# so the two knobs have to be set together: a budget that eats the ceiling
# truncates the JSON and the parse fails after you have paid for it. Cleanup
# gets a small fixed budget (it is a pass over given data, not a decision);
# composition keeps a real one.
LLM_CLEAN_THINKING = 512
LLM_SOLO_THINKING = 4096
# Ceilings, not targets. Nothing here has a natural stopping point the model
# is obliged to respect, and an unbounded response is an unbounded bill.
LLM_CLEAN_MAX_OUTPUT = 16000      # ~35 tokens/note at the 250-note cap, + budget
LLM_SOLO_MAX_OUTPUT = 12000
LLM_NOTES_MAX_OUTPUT = 2048       # the prompt asks for ~250 words

# --- pitch / notes ----------------------------------------------------------
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def pitch_name(midi: int) -> str:
    """MIDI note number to scientific pitch notation, e.g. 60 -> "C4"."""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


# --- fretboards (MIDI pitch of each open string, low -> high) ---------------
TUNINGS = {
    "guitar": (40, 45, 50, 55, 59, 64),   # E2 A2 D3 G3 B3 E4
    "bass": (28, 33, 38, 43),             # E1 A1 D2 G2
}
STRING_LABELS = {"guitar": ["E", "A", "D", "G", "B", "e"], "bass": ["E", "A", "D", "G"]}
MAX_FRET = 22


def fretboard_for(stem: str) -> str | None:
    """The tuning a stem should be read on, or None if it has no fretboard.

    A fretboard is a lie for a stem with no strings (piano, vocals, demucs'
    catch-all "other"), which is why callers branch on this to render a staff
    instead - see CLAUDE.md, "Stems without a fretboard get a staff".
    """
    root = base_stem(stem)
    return root if root in TUNINGS else None

# Frequency windows used to constrain note transcription per instrument.
PITCH_RANGE = {
    "guitar": (75.0, 1400.0),
    "bass": (30.0, 400.0),
    "vocals": (70.0, 1200.0),
    "piano": (30.0, 3000.0),
    "other": (60.0, 2000.0),
}

# --- chords -----------------------------------------------------------------
# Interval sets used both for template matching and for building tab shapes.
CHORD_QUALITIES = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "7": (0, 4, 7, 10),
    "min7": (0, 3, 7, 10),
    "maj7": (0, 4, 7, 11),
    "sus4": (0, 5, 7),
    "dim": (0, 3, 6),
    "5": (0, 7),
}
QUALITY_SUFFIX = {"maj": "", "min": "m", "7": "7", "min7": "m7",
                  "maj7": "maj7", "sus4": "sus4", "dim": "dim", "5": "5"}

# Cosine matching structurally favours sparse templates (a power chord can never
# score worse than the triad containing it), and real songs are mostly plain
# triads. This prior, added to each template's score, corrects both.
QUALITY_BIAS = {"maj": 0.035, "min": 0.035, "7": 0.005, "min7": 0.005,
                "maj7": -0.030, "sus4": -0.020, "dim": -0.035, "5": -0.045}

# --- song form ---------------------------------------------------------------
# Knobs for form.py. Pop/rock arrangements are built from a handful of repeated
# blocks, almost always in multiples of 4 bars, so the segmenter is allowed to
# be opinionated about that.
FORM = {
    "min_bars": 4,            # anything shorter is absorbed into its neighbour
    "max_parts": 16,
    "k_range": (3, 9),        # how many distinct kinds of material to look for
    "vocal_threshold": 0.30,  # fraction of a part covered by singing -> "vocal"
    "solo_density": 1.6,      # how far clear of the next stem the lead must be
                              # for an instrumental part to count as a solo (a
                              # ratio, not notes/sec - see form._assign)
    "loop_agreement": 0.75,   # how exactly a chord loop has to repeat to count
    "same_loop": 0.85,        # ... and how exactly two repeats have to agree to be
                              # called the same; claiming "same chords" wrongly is
                              # worse for a player than flagging a variation
    "snap_bars": 4,           # boundaries are nudged onto this bar multiple
}

# Open-position voicings, low E -> high e ('x' = muted). Preferred when they fit.
OPEN_CHORDS = {
    "C": "x32010", "C7": "x32310", "Cmaj7": "x32000",
    "A": "x02220", "Am": "x02210", "A7": "x02020", "Am7": "x02010", "Amaj7": "x02120",
    "G": "320003", "G7": "320001", "Gmaj7": "320002",
    "E": "022100", "Em": "022000", "E7": "020100", "Em7": "020000",
    "D": "xx0232", "Dm": "xx0231", "D7": "xx0212", "Dm7": "xx0211", "Dmaj7": "xx0222",
    "F": "133211", "Fmaj7": "xx3210", "B7": "x21202", "Bm": "x24432",
    "Asus4": "x02230", "Dsus4": "xx0233", "Esus4": "022200",
    "E5": "022xxx", "A5": "x022xx", "D5": "xx023x", "G5": "355xxx",
}

# Movable guitar shapes: fret offsets from the root fret, per string low->high.
# None = muted string. Root string index says where the root note sits.
MOVABLE_SHAPES = {
    ("maj", 0): [0, 2, 2, 1, 0, 0],        # E-shape barre
    ("min", 0): [0, 2, 2, 0, 0, 0],
    ("7", 0): [0, 2, 0, 1, 0, 0],
    ("min7", 0): [0, 2, 0, 0, 0, 0],
    ("5", 0): [0, 2, 2, None, None, None],
    ("maj7", 0): [0, None, 1, 1, 0, None],
    ("maj", 1): [None, 0, 2, 2, 2, 0],     # A-shape barre
    ("min", 1): [None, 0, 2, 2, 1, 0],
    ("7", 1): [None, 0, 2, 0, 2, 0],
    ("min7", 1): [None, 0, 2, 0, 1, 0],
    ("5", 1): [None, 0, 2, 2, None, None],
    ("maj7", 1): [None, 0, 2, 1, 2, 0],
    ("sus4", 0): [0, 2, 2, 2, 0, 0],
    ("sus4", 1): [None, 0, 2, 2, 3, 0],
    ("dim", 1): [None, 0, 1, 2, 1, None],
}


def workdir_for(audio_path: str | Path) -> Path:
    """Everything we know about one song lives here; created on demand.

    `analyzed_songs/<song>/` next to the audio file, or under $MUSICCOPILOT_OUT
    if that is set. A cache left over from the old `.musiccopilot/` layout is
    moved across rather than recomputed - stem separation is far too slow to
    throw away.
    """
    p = Path(audio_path).expanduser().resolve()
    root = Path(os.getenv("MUSICCOPILOT_OUT") or p.parent / OUTDIR).expanduser()
    d = root / p.stem

    legacy = p.parent / LEGACY_WORKDIR / p.stem
    if legacy.is_dir() and not d.exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(d)
        print(f"[musiccopilot] moved {legacy} -> {d}", file=sys.stderr)
        if not any(legacy.parent.iterdir()):
            legacy.parent.rmdir()

    d.mkdir(parents=True, exist_ok=True)
    return d


def gemini_api_key() -> str | None:
    """First of GEMINI_API_KEY / GOOGLE_API_KEY set in the environment, if any.

    Nothing loads `.env` for you - export the key yourself before running.
    """
    for env in GEMINI_KEY_ENV:
        if os.getenv(env):
            return os.environ[env]
    return None
