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
    "lead_chord_penalty": 0.6,  # how much of a stem's claim to be leading a
                              # solo is taken away for playing chords rather
                              # than a line. A wholly chordal part keeps 0.4 of
                              # its note rate, a wholly single-note one all of
                              # it. Needed once a guitar can be split into a
                              # rhythm player and a lead: the rhythm guitar is
                              # then the runner-up under every solo, and
                              # `solo_density` is not clearable against a part
                              # that is strumming all the way through it -
                              # measured on waves-bon-jovi's split solo, the
                              # margin goes 1.45 (demoted to "Instrumental")
                              # at 0.0 to 2.35 at 0.6.
                              #
                              # Not higher, though it would help: a *piano*
                              # lead really does play chords and a line at
                              # once, and `piano` is in LEAD_STEMS. This is
                              # the value that settles the split-guitar case
                              # with room to spare without deciding that a
                              # chordal instrument can never solo
}

# --- chords, lines, and telling them apart -----------------------------------
# Knobs for texture.py. A note tracker emits one note at a time and never says
# which of them were struck together, so a strummed chord arrives as unrelated
# notes a few tens of milliseconds apart - printed as an arpeggio, and split
# between players by voices.py. These are the tests for putting a strum back
# together.
TEXTURE = {
    "link": 0.06,             # how long the pick may take to reach the *next*
                              # string. The chained limit is what tells a chord
                              # from a fast run: a run's onsets are a musical
                              # subdivision apart (a sixteenth at 120bpm is
                              # 125ms) while a strum's are pick-travel apart
                              # whatever the tempo. An anchored window instead
                              # of this welded 4% of a legato solo's notes into
                              # chords nobody played.
                              #
                              # Calibrated on real DI takes rather than on a
                              # synthetic strum, which is faster than anyone
                              # actually plays: going 0.04 -> 0.06 finds
                              # chords in 54% of a rhythm take's events rather
                              # than 49% (and 34% of a separated stem's rather
                              # than 29%) while leaving a lead take at 5% and
                              # still welding none of a legato solo. 0.08 is
                              # where the solo starts to fuse, so this is the
                              # last value that costs nothing.
    "span": 0.12,             # ... and how long the whole stroke may take. A
                              # separated stem smears a strum over 58ms at the
                              # median and 151ms at p90 (a real multitrack's
                              # rhythm track: 17ms and 66ms), so a tight bound
                              # here misses half of them.
    "max_gap": 12,            # the largest step between adjacent pitches of one
                              # voicing. Not a fitted number: across all 236
                              # intervals in OPEN_CHORDS and MOVABLE_SHAPES the
                              # widest gap any shape has is 11 (the maj7 with a
                              # muted string) and 99% are 8 or under. An octave
                              # is therefore wider than every chord this repo
                              # can print, and anything wider is a chord plus
                              # something else sounding over it. Measured
                              # against ground truth this one test lifts the
                              # ceiling on separating two players from 78% to
                              # 99%; a fretboard-reachability test in its place
                              # bought nothing (79.2% against 79.2%).

    # Where a player's mean `chordness` puts them, for describing a voice and
    # for `_distinct`'s texture clause. A rhythm part is not all chords (it
    # walks between them) and a lead is not all single notes (it doubles a
    # string), so these leave a wide middle band rather than splitting at 0.5.
    "rhythm_at": 0.45,
    "lead_at": 0.20,
}

# --- more than one player in a stem ------------------------------------------
# Separation gives one `guitar` file however many guitarists played, so a band
# with a rhythm part, a lead and an acoustic gets all three stacked onto one
# fretboard. `voices.py` looks inside such a stem and splits it again; these
# are its knobs.
#
# The three cues it clusters on are weighted deliberately unevenly. `timbre`
# is the energy at each of a note's own harmonics, which is the only cue that
# stays true when both guitars play the same riff in the same octave. `pan` is
# the strongest single cue when it is there at all - two rhythm guitars are
# almost always spread left and right - and demucs preserves the mix's stereo
# image, so it survives separation. `register` is small on purpose: it is real
# (an acoustic strums where a lead sings) but one guitarist who plays a low
# riff and then a high solo would be split in two by it if it led.
VOICES = {
    "stems": ("guitar",),     # instruments worth looking inside at all
    "max": 3,                 # more players than this in one stem is not a band
    "n_fft": 4096,            # 10.8Hz bins: enough to resolve a low E's
                              # harmonics, which is where two guitars overlap
    "hop": 1024,
    "harmonics": 8,           # how far up the series the timbre cue reads
    # Measured on two known tracks summed and split back apart: this ratio
    # reads 90% of notes to the right player when the two are panned, 85% when
    # they are only half panned, and 72% when they sit dead centre and tone is
    # all there is to go on. Turning pan up past here wins the panned case by
    # a point and loses twenty on the centred one.
    "timbre": 1.0,
    "pan": 0.9,
    "register": 0.25,
    # How chordal the event is - 0 for a note struck alone, 1 for three or
    # more struck together. The cue a per-note clustering could never have,
    # and the one that answers "which of these two is the rhythm player".
    # `min_concurrency` is what keeps it honest; see `_concurrency`.
    "texture": 0.6,

    # What makes a second player real. Cluster separation (a silhouette) is
    # NOT one of these, and cannot be: measured across five single-instrument
    # stems and four two-player mixes it ran 0.22-0.43 on both, so one guitar
    # cut in half scores as convincingly as two guitars do. What does separate
    # them is *which cue* the halves differ on - two players differ in where
    # they sit or in how they sound, while one player cut in half differs in
    # register and in whatever register drags along with it.
    "min_pan_gap": 0.15,      # audibly apart in the stereo image ...
    "min_gap_z": 1.5,         # ... by more than the spread within each of them
    "min_tone_gap": 0.18,     # or, with no pan to go on, this far apart in
                              # tone - and only if tone separates them better
                              # than pitch does
                              # or one of them is playing chords while the
                              # other plays single notes - which is measured
                              # against TEXTURE["rhythm_at"] / ["lead_at"]
                              # rather than as a gap, because "all chords"
                              # against "some chords" is a wide gap and one
                              # guitarist. Both ends have to hold, and they
                              # have to be playing at once:
    "min_interleave": 0.14,   # ... where "at once" means: in nine windows out
                              # of ten, the quieter of the two textures still
                              # holds this share of the window. Measured 0.18,
                              # 0.17 and 0.29 on three two-player mixes against
                              # 0.00 and 0.06 on two one-guitarist ones, 0.00
                              # on a real lead DI take and on crystallize, and
                              # 0.11 on a real rhythm DI take - which is the
                              # nearest miss and what sets this. The margin is
                              # thin and the threshold sits in the middle of
                              # it; it only ever *guards* the texture clause,
                              # since pan decides any mix that has some
    "min_notes": 40,          # fewer than this and there is nothing to cluster
    "min_events": 30,         # ... and a chord is one event however many
                              # strings it has, so a stem can clear `min_notes`
                              # on ten strummed chords and still have nothing
                              # to cluster
    "min_share": 0.13,        # a "player" with less of the stem than this is
                              # separation residue, not a second guitarist
    "min_level": 0.002,       # RMS under this and the stem is demucs' noise
                              # floor for an instrument the song does not have
}

# --- checking a transcription against the audio ------------------------------
# Knobs for clean.py. Every tracker in notes.py answers "what pitch is this?"
# and none of them answers "is anything here?", so these are the thresholds for
# asking the second question afterwards.
#
# The two dB numbers were measured, not guessed. Across the songs in this repo,
# notes read off a stem the band actually played sit ~10 dB under that stem's
# own loud level; notes read off a stem separation merely produced (the piano
# in a song with no piano) sit 50-60 dB under it, because they come from the
# noise floor. `note_floor_db` is set well inside that gap - loose enough to
# keep the quiet end of real playing (the 5th percentile of a real stem's notes
# is around -30), tight enough that the residue does not survive it.
CLEAN = {
    "note_floor_db": -35.0,   # a note's own pitch band must reach this far
                              # below the stem's loud level, or nothing played
    "presence_db": -25.0,     # ... and a whole stem this far under the loudest
                              # stem in the song is residue, not an instrument
    "measure_seconds": 0.30,  # a note is judged on its attack and early
                              # sustain; its own decay is not evidence against it
    "merge_gap": 0.06,        # how far apart same-pitch fragments may sit and
                              # still be one note; further is a rest
    "attack_db": 1.0,         # ... and a junction the band rises this far
                              # across was a second pick, so it is left alone.
                              # A struck string gets louder; measurement noise
                              # is about a dB, which is all the slack this is
    "chord_window": 0.08,     # how far apart two notes of one strum can land

    # What a guitar's own partials measure, on isolated notes in this repo's
    # material: an octave up sits ~10 dB under the fundamental, a twelfth ~17,
    # two octaves ~17.5. A "note" no louder than this over a note sounding
    # underneath it has not been shown to be a note at all.
    "overtone_rolloff": {12: -10.0, 19: -17.0, 24: -17.5, 28: -22.0, 31: -24.0},
    "overtone_margin_db": 3.0,   # how far over the prediction still counts as
                                 # explained; the slack is for a partial that
                                 # lands on a resonance
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
