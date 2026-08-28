"""Constants shared across the pipeline: tunings, chord vocabulary, paths."""
from __future__ import annotations

import os
import sys
from pathlib import Path

SR = 44100                      # everything is resampled to this
OUTDIR = "analyzed_songs"       # per-song folder lives here, next to the audio
LEGACY_WORKDIR = ".musiccopilot"    # what OUTDIR used to be called

# --- Gemini -----------------------------------------------------------------
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
GEMINI_KEY_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

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
