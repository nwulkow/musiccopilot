"""Transcribed notes -> an engraved score: bars, rhythmic values, rests, ties.

`tabs.StaffLayout` prints a note *name* in a line/space slot on a
time-proportional grid. That is a debugging view, not notation: a reader of a
piano part expects bars of note values with stems and beams, rests where
nothing sounds, ties across bar lines, a key signature and a clef per hand.
This module is that second thing.

The division of labour follows the one CLAUDE.md already sets out for the tab
grid. Every *musical* decision is made here - which hand a note belongs to,
how long it is written as, how it is spelled against the key - and the
renderer only draws glyphs. So the browser, the terminal and anything else
read the same piece; only the engraving differs.

The column grid is deliberately the same one `TabLayout`/`StaffLayout` use
(`col_of` is copied verbatim, not re-derived), because a score and a tab of
the same passage have to agree about where bar 17 is.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import NOTE_NAMES

# --- pitch spelling ---------------------------------------------------------

_LETTERS = "CDEFGAB"
_LETTER_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_SHARP_ORDER = "FCGDAEB"        # the order sharps are added to a signature
_FLAT_ORDER = "BEADGCF"         # ... and flats
_MIDDLE_C = 60

# Key signature (in sharps, negative = flats) for each tonic pitch class, at
# the enharmonic spelling actually used: A# major is written Bb major, and
# D# minor is written Eb minor. `detect_key` only ever names sharps, so
# without this a song in Bb would be engraved with ten sharps.
_MAJOR_SIG = {0: 0, 1: -5, 2: 2, 3: -3, 4: 4, 5: -1,
              6: 6, 7: 1, 8: -4, 9: 3, 10: -2, 11: 5}
_MINOR_SIG = {9: 0, 4: 1, 11: 2, 6: 3, 1: 4, 8: 5,
              3: -6, 10: -5, 5: -4, 0: -3, 7: -2, 2: -1}

# The written value of every duration, longest first, in quarter notes.
# `code` is the note-value name a renderer knows ("q" = quarter); `dots`
# multiplies it by 1.5. Anything shorter than a 32nd is not worth engraving
# from a transcription - it is jitter, not rhythm.
_VALUES: list[tuple[float, str, int]] = [
    (4.0, "w", 0), (3.0, "h", 1), (2.0, "h", 0), (1.5, "q", 1),
    (1.0, "q", 0), (0.75, "8", 1), (0.5, "8", 0), (0.375, "16", 1),
    (0.25, "16", 0), (0.125, "32", 0),
]


def _alterations(sig: int) -> dict[str, int]:
    """Which letters the key signature sharpens or flattens."""
    alter = dict.fromkeys(_LETTERS, 0)
    for letter in (_SHARP_ORDER[:sig] if sig > 0 else _FLAT_ORDER[:-sig]):
        alter[letter] = 1 if sig > 0 else -1
    return alter


def key_signature(key: str) -> tuple[str, int]:
    """`'E minor'` -> `('Em', 1)`: the signature's name and its sharp count.

    The name is what a renderer labels the signature with; the count is what
    `spell` needs to decide between `F#` and `Gb`.
    """
    root, _, mode = (key or "C major").partition(" ")
    mode = mode.strip().lower() or "major"
    try:
        pc = NOTE_NAMES.index(root.strip())
    except ValueError:
        pc, mode = 0, "major"
    minor = mode.startswith("min")
    sig = (_MINOR_SIG if minor else _MAJOR_SIG)[pc]
    # The tonic's own name has to be spelled against the signature too, or a
    # key written with flats gets a sharp name - Bb major called "A#".
    letter, acc, _ = spell(pc + 60, sig)
    return letter + {1: "#", -1: "b", 0: ""}[acc] + ("m" if minor else ""), sig


def spell(pitch: int, sig: int) -> tuple[str, int, int]:
    """A MIDI pitch as `(letter, alteration, octave)` in a key of `sig` sharps.

    Every letter that can sound the pitch is a candidate; they are ranked so
    that the signature's own spelling wins outright (it needs no printed
    accidental), then the one needing the fewest accidentals, then the one
    pointing the way the key does. That last tiebreak is why C major writes
    C# and Bb major writes Db for the same key; the one before it is why E
    major writes F natural rather than E#.
    """
    alter = _alterations(sig)
    pc = pitch % 12
    cands = [(l, a) for l in _LETTERS for a in (-1, 0, 1)
             if (_LETTER_PC[l] + a) % 12 == pc]
    letter, acc = min(cands, key=lambda c: (c[1] != alter[c[0]], abs(c[1]),
                                            (c[1] >= 0) != (sig >= 0)))
    # The octave belongs to the *spelling*, not the pitch: Cb4 sounds B3, and
    # writing it in octave 3 would put the head a seventh off the line.
    return letter, acc, round((pitch - acc - _LETTER_PC[letter]) / 12) - 1


def key_name(pitch: int, sig: int) -> str:
    """A pitch as a renderer-ready key string, e.g. `'f#/4'`."""
    letter, acc, octave = spell(pitch, sig)
    return f"{letter.lower()}{'#' * acc if acc > 0 else 'b' * -acc}/{octave}"


# --- the score --------------------------------------------------------------

@dataclass
class ScoreNote:
    """One written event: a chord, or a rest when `keys` is empty.

    `start`/`end` are the absolute seconds the *written* value covers, which
    is what a play-along cursor matches against - not the raw transcribed
    span, since the written note has been quantised to the bar grid.
    """
    keys: list[str]                 # spellings, low to high; empty = rest
    duration: str                   # "w" | "h" | "q" | "8" | "16" | "32"
    dots: int = 0
    start: float = 0.0
    end: float = 0.0
    tie: bool = False               # tied into the next written event
    pitches: list[int] = field(default_factory=list)
    ticks: int = 0                  # length in grid columns


@dataclass
class Measure:
    """One bar, with a voice per stave (index matches `Score.clefs`)."""
    number: int
    start: float
    end: float
    chord: str | None
    voices: list[list[ScoreNote]]


@dataclass
class Score:
    """A window of one stem, engraved. One stave for a single-hand part, two
    for anything that straddles middle C widely enough to need a grand staff."""
    clefs: list[str]
    key: str                        # signature name, e.g. "Em"
    sig: int                        # ... as a sharp count, negative for flats
    time: str                       # "4/4"
    tempo: float
    t0: float
    beats_per_bar: int
    subdiv: int
    first_bar: int
    measures: list[Measure]

    @property
    def n_staves(self) -> int:
        return len(self.clefs)


def pick_clef(pitches: list[int]) -> str:
    """Treble or bass, by where the notes sit against middle C - the same
    judgement `tabs.pick_clef` makes, on pitches rather than notes."""
    if not pitches:
        return "treble"
    ordered = sorted(pitches)
    median = ordered[len(ordered) // 2]
    return "bass" if median < _MIDDLE_C else "treble"


def _split_hands(notes: list) -> list[tuple[str, list]]:
    """One stave or two, and which notes go on each.

    A grand staff is only worth its second stave when the part really uses
    both hands: a bass line engraved as an empty treble stave over a busy
    bass one is harder to read than the single stave it should have been. So
    two staves need notes meaningfully on both sides of middle C *and* a
    range no one hand covers.
    """
    if not notes:
        return [("treble", [])]
    below = [n for n in notes if n.pitch < _MIDDLE_C]
    above = [n for n in notes if n.pitch >= _MIDDLE_C]
    span = max(n.pitch for n in notes) - min(n.pitch for n in notes)
    if below and above and min(len(below), len(above)) >= 0.15 * len(notes) and span >= 20:
        return [("treble", above), ("bass", below)]
    return [(pick_clef([n.pitch for n in notes]), notes)]


def _values(subdiv: int) -> list[tuple[int, str, int]]:
    """`_VALUES` in grid columns, dropping anything the grid cannot express.

    At `subdiv=4` a column is a sixteenth, so a dotted sixteenth is 1.5
    columns and simply is not writable on this grid; it drops out here rather
    than rounding into a lie further down.
    """
    out = []
    for quarters, code, dots in _VALUES:
        ticks = quarters * subdiv
        if ticks >= 1 and float(ticks).is_integer():
            out.append((int(ticks), code, dots))
    return out


def _fits(offset: int, ticks: int, dots: int, subdiv: int) -> bool:
    """Whether a value of this length may *start* at this offset in the bar.

    A quarter or anything shorter goes wherever it lands: an eighth on the
    second sixteenth of a beat, or a syncopated quarter on the off-beat, is
    ordinary rhythm and every reader knows it on sight. Longer values have to
    begin on a boundary of their own length, so a half note never starts on
    beat 2 and hides where beat 3 is; a dotted value is judged on its undotted
    length, which is what keeps the dotted quarter on a beat rather than
    anywhere at all.

    Being strict about the long values is what stops a transcription's late
    onsets turning whole bars into tied fragments - and being loose about the
    short ones is what stops the common case doing the same.
    """
    if ticks <= subdiv:
        return True
    return offset % (ticks * 2 // 3 if dots else ticks) == 0


def _write(events: list[tuple[int, int, list]], n_cols: int, per_bar: int,
           values: list[tuple[int, str, int]], t_of, sig: int,
           subdiv: int) -> list[list[ScoreNote]]:
    """Lay a voice's events out bar by bar, filling the gaps with rests.

    `events` are `(column, length, notes)` in time order and never overlap.
    Anything longer than one writable value - or crossing a bar line - comes
    out as tied pieces, because that is how it is read.
    """
    bars: list[list[ScoreNote]] = [[] for _ in range(max(1, n_cols // per_bar))]

    def value_for(offset: int, room: int, rest: bool):
        """The longest writable value that starts here and fits in the bar.

        Rests are held to the stricter rule - undotted, aligned to their own
        length - because a rest's whole job is to show where the beat is. A
        dotted quarter rest starting off the beat is legal and unreadable;
        two ordinary rests that land on beats are neither.
        """
        for ticks, code, dots in values:
            if ticks > room:
                continue
            if (not dots and offset % ticks == 0) if rest else _fits(offset, ticks, dots, subdiv):
                return ticks, code, dots
        return values[-1]

    def emit(col: int, length: int, pitches: list[int] | None) -> None:
        while length > 0 and col < n_cols:
            bar, offset = divmod(col, per_bar)
            if bar >= len(bars):
                return
            room = min(length, per_bar - offset)
            ticks, code, dots = value_for(offset, room, pitches is None)
            keys = [key_name(p, sig) for p in sorted(pitches)] if pitches else []
            bars[bar].append(ScoreNote(
                keys=keys, duration=code, dots=dots,
                start=t_of(col), end=t_of(col + ticks),
                tie=bool(pitches) and ticks < length,
                pitches=sorted(pitches) if pitches else [], ticks=ticks))
            col, length = col + ticks, length - ticks

    cursor = 0
    for col, length, notes in events:
        if col > cursor:
            emit(cursor, col - cursor, None)
        emit(col, length, [n.pitch for n in notes])
        cursor = col + length
    if cursor < n_cols:
        emit(cursor, n_cols - cursor, None)
    return bars


def _events(notes: list, col_of, n_cols: int, subdiv: int) -> list[tuple[int, int, list]]:
    """Quantise notes onto the grid and group simultaneous ones into chords.

    A written voice is one rhythm: notes that land on the same column are one
    chord, and a note is written no longer than the gap to the next onset -
    a transcription's overlapping tails are ringing, not counterpoint, and
    engraving them as separate voices would make the bar unreadable.

    Gaps shorter than an eighth are closed rather than written. A note that
    was released a sixteenth early is a player lifting a finger, not a rest,
    and printing it as one turns every bar into note-rest-note-rest confetti
    that reads nothing like what was played.
    """
    grouped: dict[int, list] = {}
    for n in notes:
        col = col_of(n.start)
        if 0 <= col < n_cols:
            grouped.setdefault(col, []).append(n)

    onsets = sorted(grouped)
    min_rest = max(1, subdiv // 2)
    out = []
    for i, col in enumerate(onsets):
        chord = grouped[col]
        held = max(col_of(n.end) for n in chord) - col
        limit = (onsets[i + 1] if i + 1 < len(onsets) else n_cols) - col
        length = max(1, min(held, limit))
        if limit - length < min_rest:
            length = limit
        out.append((col, length, chord))
    return out


def build_score(notes: list, *, tempo: float = 120.0, t0: float = 0.0,
                beats_per_bar: int = 4, subdiv: int = 4, first_bar: int = 1,
                key: str = "C major", chords: list | None = None,
                min_cols: int = 0) -> Score:
    """Engrave a window of notes.

    `tempo`, `t0`, `subdiv` and `first_bar` mean exactly what they mean to
    `TabLayout` - pass the same ones you sliced the notes with, or the bars
    are numbered for a different passage (CLAUDE.md, "Grid columns").
    """
    per_bar = beats_per_bar * subdiv
    values = _values(subdiv)
    sig_name, sig = key_signature(key)

    def col_of(t: float) -> int:
        return int(round((t - t0) * tempo / 60.0 * subdiv))

    def t_of(col: int) -> float:
        return t0 + col * 60.0 / tempo / subdiv

    cols = max([col_of(n.start) for n in notes] + [min_cols - 1]) + 1
    n_cols = max(per_bar, -(-cols // per_bar) * per_bar)

    hands = _split_hands(notes)
    voices = [_write(_events(hand, col_of, n_cols, subdiv), n_cols, per_bar,
                     values, t_of, sig, subdiv)
              for _, hand in hands]

    named = {}
    for ch in chords or []:
        col = col_of(ch.start)
        if 0 <= col < n_cols and ch.name != "N.C.":
            named.setdefault(col // per_bar, ch.name)

    measures = [
        Measure(number=b + first_bar, start=t_of(b * per_bar),
                end=t_of((b + 1) * per_bar), chord=named.get(b),
                voices=[v[b] for v in voices])
        for b in range(n_cols // per_bar)
    ]
    return Score(clefs=[c for c, _ in hands], key=sig_name, sig=sig,
                 time=f"{beats_per_bar}/4", tempo=tempo, t0=t0,
                 beats_per_bar=beats_per_bar, subdiv=subdiv,
                 first_bar=first_bar, measures=measures)
