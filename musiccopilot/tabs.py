"""Fretboard mapping and ASCII tablature rendering."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .config import (MAX_FRET, MOVABLE_SHAPES, NOTE_NAMES, OPEN_CHORDS,
                     STRING_LABELS, TUNINGS)
from .notes import Note


@dataclass
class Fretted:
    """A note placed at a specific string/fret - `fret_notes`' output, and
    what `_cell`/`render_tab` render from."""
    note: Note
    string: int        # index into the tuning, 0 = lowest string
    fret: int


# --- placing notes on the neck ----------------------------------------------

def _candidates(pitch: int, tuning) -> list[tuple[int, int]]:
    """Every (string, fret) that can sound `pitch` on this tuning, within
    MAX_FRET. A pitch below the lowest open string yields nothing."""
    return [(s, pitch - open_p) for s, open_p in enumerate(tuning)
            if 0 <= pitch - open_p <= MAX_FRET]


# A guitarist's left hand sits in a *position* - a four-fret box - and plays
# whatever falls under it, crossing strings rather than sliding up and down one
# string. Costing each note only against the previous one misses that: every
# pitch is reachable on the high string somewhere, so a purely local optimiser
# walks the melody up the E string and climbs the neck instead of staying in
# the box. So the Viterbi state here is the hand position, each event is placed
# as a whole against it (`_placement`), and the hand pays to move even when the
# individual note interval is small.

BOX = 4                    # frets under the hand without shifting
_OPEN_BONUS = 0.10         # an open string is easy, but not free everywhere


def _position_cost(fret: int, hand: int, open_penalty: float = 0.0) -> float:
    """Cost of fretting `fret` with the hand anchored at `hand`."""
    if fret == 0:
        # Open strings belong to open position; up the neck they mean letting
        # go of the box, so they stop being a bargain. Bounded, because
        # letting go is no harder from the twelfth fret than from the fifth -
        # unbounded, this charged an open E5 shape more than a barre and was
        # half of why a strummed verse drifted up the neck and stayed there.
        return -_OPEN_BONUS + 0.10 * min(hand, BOX) + open_penalty
    reach = fret - hand
    if 0 <= reach < BOX:
        return 0.02 * reach                    # under the fingers already
    # outside the box: a stretch, and increasingly implausible
    return 0.35 + 0.5 * (reach - BOX + 1 if reach >= BOX else -reach)


# Everything under the hand costs about the same (0.02 a fret across the box),
# so two readings of a strummed chord - the open shape, and a barre seven frets
# up - come out within a hundredth of each other, and the winner is then decided
# by whatever the hand happened to be doing beforehand. `_hand_cost` makes that
# accidental choice permanent: coming back down a fifth costs 4.5, which no
# per-event saving of 0.02 will ever repay, so one event that genuinely wants a
# high hand drags the rest of the passage up with it. Crystallize's second verse
# went up to the seventh and printed the open E5 it starts on as a five-string
# barre for the rest of the part.
#
# So low positions win ties. That is true of playing (given two equally
# comfortable readings people take the one nearer the nut) and it is true of
# this tab in particular, which prints chord names above the grid: a reader
# seeing `E5` wants the shape that name means. It is deliberately small enough
# to settle a tie and nothing more - a real solo up at the ninth costs several
# points down at the nut, and pays a fraction of one without noticing.
_LOW_BIAS = 0.02


def _hand_cost(prev_hand: int, hand: int) -> float:
    """Shifting the whole hand along the neck. Small shifts are ordinary
    playing; big jumps need a reason, so they cost superlinearly."""
    if prev_hand == hand:
        return 0.0
    d = abs(prev_hand - hand)
    return 0.30 + 0.18 * d + 0.06 * d * d


def _string_cost(a: int, b: int) -> float:
    """Crossing strings. Cheap - this is what the hand position is *for* - but
    not free, so a note stays on the string it is already on when it can."""
    return 0.06 * abs(a - b)


def _hand_options(tuning) -> list[int]:
    """Candidate anchor frets: open position plus every box up the neck.

    `tuning` is unused - the box range is the same shape for guitar and bass -
    but kept in the signature to mirror `_open_penalty`, which does need it.
    """
    return list(range(0, MAX_FRET - BOX + 2))


# Rather than print a fret nobody would reach for, leave the note out. Priced
# against `_position_cost`, which charges 0.5 a fret past the box: dropping
# wins once a note would need about four frets of stretch.
#
# Only ever offered to a note in a *chord*, and that restriction is the whole
# safety of it. A chord can have more notes than the hand has strings to put
# them on - a phantom partial stacked on a six-note strum leaves nothing free
# but the far end of the neck, and a tab that omits it is closer to the truth
# than one that prints `D22`. A single note cannot be in that position: there
# is always somewhere to put it, and hiding one because the hand happens to be
# elsewhere would silently delete the highest note of a phrase. Whether a lone
# high note is real is `clean.py`'s question, not the fretboard's.
_DROP_COST = 2.2
# A chord's notes climb the strings as they climb in pitch. Voicings that
# cross do exist, so this is a nudge and not a rule.
_CROSS_COST = 0.25


@lru_cache(maxsize=8192)
def _placement(pitches: tuple[int, ...], tuning: tuple[int, ...], hand: int,
               open_penalty: float) -> tuple[float, int, tuple[int | None, ...]]:
    """Cheapest way one hand at `hand` can hold this event, as
    `(cost, lowest string used, string per pitch)` - None where a note is
    better left out than placed.

    `pitches` must be ascending; the string each one lands on is chosen
    jointly, by a dynamic program over which strings are still free, so no
    note's placement can strand the notes after it. States are (notes placed,
    strings used, last string), which is at most 6 x 64 x 7 - small enough to
    solve exactly, and memoised because a song plays the same chord in the
    same position over and over.
    """
    droppable = len(pitches) > 1
    # state: (used-string bitmask, last string used) -> (cost, choices)
    best: dict[tuple[int, int], tuple[float, tuple[int | None, ...]]] = {(0, -1): (0.0, ())}
    for pitch in pitches:
        cands = _candidates(pitch, tuning)
        nxt: dict[tuple[int, int], tuple[float, tuple[int | None, ...]]] = {}
        for (mask, last), (c, chosen) in best.items():
            # a pitch off the end of the neck has nowhere to go but out
            options = [(c + _DROP_COST, (mask, last), None)] \
                if droppable or not cands else []
            for s, f in cands:
                if mask & (1 << s):
                    continue
                cost = c + _position_cost(f, hand, open_penalty) \
                    + _CROSS_COST * max(0, last - s)
                options.append((cost, (mask | (1 << s), s), s))
            for cost, key, s in options:
                if key not in nxt or cost < nxt[key][0]:
                    nxt[key] = (cost, chosen + (s,))
        best = nxt
    cost, chosen = min(best.values(), key=lambda v: v[0])
    placed = [s for s in chosen if s is not None]
    return cost + _LOW_BIAS * hand, (min(placed) if placed else 0), chosen


def _open_penalty(notes: list[Note], tuning) -> float:
    """How much to discourage open strings for *this* passage.

    Open position is genuinely right for a lot of playing, but it is also the
    cheapest thing on the neck, so on a short window it wins by default even
    when the phrase is plainly a lead line up at the fifth fret. The give-away
    is the melody's own floor: if every note in the passage could only be
    played above the nut anyway, the hand was never in open position, and an
    occasional low note should not drag the whole phrase down there.

    Judged on the melody's *lowest* notes rather than its median: a lead line
    that dips to an open string once still lives up the neck.
    """
    if not notes:
        return 0.0
    lowest = min(tuning)
    floor = float(np.percentile([n.pitch for n in notes], 10))
    # semitones above the lowest open string that the quiet end of the phrase
    # sits at; a solo an octave up scores 12 and pays accordingly
    return float(np.clip((floor - lowest - 7) / 12.0, 0.0, 1.0)) * 0.55


def _group(notes: list[Note], tol: float = 0.05) -> list[list[Note]]:
    """Cluster near-simultaneous notes (a chord stab) into one event.

    Each event comes back in pitch order, not onset order: a strum's notes
    land a few milliseconds apart in whatever order the tracker found them,
    and everything downstream reads an event as a chord from the bottom up.
    """
    groups: list[list[Note]] = []
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        if groups and n.start - groups[-1][0].start <= tol:
            groups[-1].append(n)
        else:
            groups.append([n])
    return [sorted(g, key=lambda n: n.pitch) for g in groups]


# --- reading one line out of a stem -----------------------------------------
#
# A guitar stem is one file however many parts were played into it, so the tab
# of a verse is a riff and a strummed chord and an arpeggio all on the same six
# strings at once - and the line you were trying to learn is somewhere in
# there. `voices.py` is the answer when the parts were played by *different
# people* (it splits the stem itself); this is the answer when they were not,
# or when the split has already happened and one player is still doing two
# things. It chooses nothing about what is true - both halves are notes the
# stem really contains - so it lives at display time and changes no cache.

# Balanced against each other on purpose: an octave below the top of your own
# chord costs about what an octave leap in the line costs (_STEP is capped at
# a twelfth of a semitone-leap each), so neither "always take the top note" nor
# "never move" can win outright. Tilt _UNDER up and the reading becomes a
# skyline; tilt it down and the line sinks into whatever inner voice moves
# least, which on a strummed part is the root note going nowhere.
_STEP = 0.12          # per semitone of melodic leap, capped at an octave
_UNDER = 1.0          # per octave below the top of its own event
_QUIET = 0.5          # for being the quiet one of that event
_RESTART = 1.2        # seconds of silence after which the line starts afresh


def split_melody(notes: list[Note]) -> tuple[list[Note], list[Note]]:
    """Separate `notes` into the line being played and what is under it.

    One note per event goes to the line, chosen so the line as a whole holds
    together: a Viterbi over the events, paying for melodic leaps, for sitting
    below the top of its own chord, and for being the quiet note in it. The
    leap term is what does the real work - a strummed chord's top note and a
    lick's next note are indistinguishable one event at a time, and only look
    different as a path. Picking the highest note of every event instead (a
    skyline) hops onto whichever chord tone happens to be on top and reads as
    an arpeggio, which is the thing this exists to get out of the way.

    A gap longer than `_RESTART` ends a phrase rather than being leapt across:
    after a bar of rest the hand may be anywhere, and charging for the
    interval would drag the next phrase towards the last one's register.
    """
    events = _group(notes)
    if not events:
        return [], []

    def emission(n: Note, event: list[Note]) -> float:
        return _UNDER * (event[-1].pitch - n.pitch) / 12.0 + _QUIET * (1.0 - n.velocity)

    cost = [np.array([emission(n, events[0]) for n in events[0]])]
    back: list[np.ndarray] = []
    for prev, event in zip(events, events[1:]):
        restart = event[0].start - max(n.end for n in prev) > _RESTART
        step = np.array([[0.0 if restart else _STEP * min(abs(n.pitch - p.pitch), 12)
                          for p in prev] for n in event])
        m = cost[-1][None, :] + step
        back.append(m.argmin(axis=1))
        cost.append(m.min(axis=1) + np.array([emission(n, event) for n in event]))

    path = [int(np.argmin(cost[-1]))]
    for b in reversed(back):
        path.append(int(b[path[-1]]))
    path.reverse()

    line = [event[k] for event, k in zip(events, path)]
    chosen = {id(n) for n in line}
    return line, [n for n in notes if id(n) not in chosen]


def pick_instrument(notes: list[Note]) -> str:
    """Guitar or bass, judged by where the notes actually sit."""
    if not notes:
        return "guitar"
    return "bass" if float(np.median([n.pitch for n in notes])) < TUNINGS["guitar"][0] else "guitar"


def fret_notes(notes: list[Note], instrument: str = "guitar") -> list[Fretted]:
    """Choose playable string/fret positions, minimising hand movement (Viterbi).

    The state is the hand position, not the individual note's (string, fret):
    a guitarist keeps the hand in one four-fret box and crosses strings inside
    it, and only a state that remembers where the hand is can prefer that over
    running the melody up a single string.

    Each event is placed *as a whole* against each candidate hand
    (`_placement`), rather than by anchoring its lowest note and then finding
    somewhere for the rest. Anchoring first is what produced the tab's worst
    lie: B3 + E4 + G4 would take the open B and open e for the first two
    notes, leaving G4 nothing under the hand but the twelfth fret of the G
    string - while the shape a guitarist actually plays (G4/B5/e3, three
    fingers in one box) was never considered, because the anchor had already
    been committed. Placing the event jointly finds that shape, and the same
    search is what lets a note be *left out* rather than printed at a fret
    nobody would reach for.
    """
    tuning = TUNINGS[instrument]
    groups = [[n for n in g if _candidates(n.pitch, tuning)] for g in _group(notes)]
    groups = [g for g in groups if g]
    if not groups:
        return []
    hands = _hand_options(tuning)
    open_pen = round(_open_penalty([n for g in groups for n in g], tuning), 2)
    shapes = [[_placement(tuple(n.pitch for n in g), tuning, h, open_pen) for h in hands]
              for g in groups]

    # [to, from] - the same shift costs the same wherever in the song it is
    shift = np.array([[_hand_cost(a, b) for a in hands] for b in hands])
    lows = [np.array([low for _, low, _ in s]) for s in shapes]

    cost = [np.array([c for c, _, _ in shapes[0]])]
    back: list[np.ndarray] = []
    for i in range(1, len(groups)):
        cross = _string_cost(lows[i][:, None], lows[i - 1][None, :])
        m = cost[-1][None, :] + shift + cross
        back.append(m.argmin(axis=1))
        cost.append(m.min(axis=1) + np.array([c for c, _, _ in shapes[i]]))

    path = [int(np.argmin(cost[-1]))]
    for b in reversed(back):
        path.append(int(b[path[-1]]))
    path.reverse()

    out: list[Fretted] = []
    for group, shape, k in zip(groups, shapes, path):
        for n, s in zip(group, shape[k][2]):
            if s is not None:
                out.append(Fretted(n, s, n.pitch - tuning[s]))
    return out


# --- rendering ---------------------------------------------------------------

LEGEND = "b bend   ~ vibrato   h hammer-on   p pull-off   / slide   m palm mute"


def _cell(fr: Fretted) -> str:
    """Render one tab cell, e.g. '5', '7b9', '5~', 'h3'.

    A bend prints as `fret + round(bend)`: `bend` is measured in `_segment_contour`
    from `base`, the pitch the note is *written* as, not from wherever the pitch
    contour happened to start - so the target fret here lands on the same origin
    the number was measured from. Quantised to 0.5/1.0/1.5/2.0 semitones upstream,
    so `round` just turns that into fret arithmetic. If the target would land at
    or below the current fret (bend rounds to nothing, or the measurement is
    noise) it is not a bend worth notating - print the plain fret instead.
    """
    t, f = fr.note.technique, fr.fret
    if t == "bend":
        # a bend that lands where it started is not a bend; print the note
        target = f + int(round(fr.note.bend or 2))
        return f"{f}b{target}" if target > f else str(f)
    return {"vibrato": f"{f}~", "hammer": f"h{f}", "pull": f"p{f}",
            "slide": f"/{f}", "palm_mute": f"{f}m"}.get(t, str(f))


class TabLayout:
    """The laid-out grid behind a rendered tab.

    `render_tab` used to compute the column geometry and throw it away, which
    is fine for printing but not for the play-along view: to draw a cursor at
    the current moment you need to know which *screen column* a given second
    maps to, and that depends on the per-column widths the layout chose. So the
    geometry is an object now, and both the static tab and the live one read it.
    """

    def __init__(self, fretted: list[Fretted], instrument: str = "guitar", *,
                 tempo: float = 120.0, t0: float = 0.0, beats_per_bar: int = 4,
                 subdiv: int = 4, max_width: int = 92, first_bar: int = 1,
                 chords: list | None = None, min_cols: int = 0):
        """Lay out the grid: bucket every note into a beat-subdivided column,
        size columns to their widest cell, and wrap columns into per-line
        systems that fit `max_width`. `t0` and `first_bar` must match whatever
        window the notes were sliced from, or the grid and the printed bar
        numbers land on the wrong moment (see CLAUDE.md's "Grid columns")."""
        self.instrument, self.tempo, self.t0 = instrument, tempo, t0
        # Kept so a consumer can pair a grid cell back with the note that made
        # it (technique, pitch, timing) without recomputing the column maths -
        # the web renderer draws the cells this layout placed rather than
        # re-implementing `col_of` in JavaScript.
        self.fretted = list(fretted)
        self.subdiv, self.beats_per_bar, self.first_bar = subdiv, beats_per_bar, first_bar
        self.labels = STRING_LABELS[instrument]
        self.n_str = len(self.labels)
        self.per_bar = beats_per_bar * subdiv

        cols = max([self.col_of(f.note.start) for f in fretted] + [min_cols - 1]) + 1
        self.n_cols = max(self.per_bar, int(np.ceil(cols / self.per_bar) * self.per_bar))

        self.grid = [["-"] * self.n_cols for _ in range(self.n_str)]
        for fr in fretted:
            if 0 <= (c := self.col_of(fr.note.start)) < self.n_cols:
                self.grid[fr.string][c] = _cell(fr)

        self.names = [""] * self.n_cols
        for ch in chords or []:
            if 0 <= (c := self.col_of(ch.start)) < self.n_cols and ch.name != "N.C.":
                self.names[c] = ch.name

        # one dash of breathing room after every cell keeps digits readable
        self.widths = [max(len(self.grid[s][c]) for s in range(self.n_str)) + 1
                       for c in range(self.n_cols)]
        bar_w = [1 + sum(self.widths[b:b + self.per_bar])
                 for b in range(0, self.n_cols, self.per_bar)]
        self.per_line = max(1, min(len(bar_w), (max_width - 4) // max(bar_w)))
        self.bars = [f"bar {c // self.per_bar + first_bar}" if c % self.per_bar == 0 else ""
                     for c in range(self.n_cols)]

    # --- time <-> column ----------------------------------------------------
    def col_of(self, t: float) -> int:
        """Seconds -> grid column, relative to `t0` at `tempo`/`subdiv`."""
        return int(round((t - self.t0) * self.tempo / 60.0 * self.subdiv))

    def time_of(self, col: int) -> float:
        """Inverse of `col_of` - the moment a column's cell falls on, used to
        drive the play-along cursor from a column back to a playback position."""
        return self.t0 + col * 60.0 / self.tempo / self.subdiv

    def line_of(self, col: int) -> int:
        """Which rendered system (line-block) a column falls on."""
        return int(col // (self.per_bar * self.per_line))

    def cols_of_line(self, line: int) -> range:
        """The columns rendered in system `line`, clipped to the grid's end."""
        span = self.per_bar * self.per_line
        return range(line * span, min((line + 1) * span, self.n_cols))

    @property
    def n_lines(self) -> int:
        """How many systems the whole grid wraps into."""
        return int(np.ceil(self.n_cols / (self.per_bar * self.per_line)))

    def x_of(self, col: int) -> int:
        """Screen x (in characters, within a rendered row) of a column's cell."""
        cols = self.cols_of_line(self.line_of(col))
        x = 3                                   # the string-label gutter
        for c in cols:
            if c % self.per_bar == 0:
                x += 1                          # the bar line
            if c == col:
                return x
            x += self.widths[c]
        return x

    # --- rendering ----------------------------------------------------------
    def _row(self, cols, cell, fill) -> str:
        """One rendered line: a bar separator before every bar-start column,
        each cell (from the `cell(c)` callback) left-padded to its column's
        width with `fill`, closed by a trailing bar line."""
        return "".join(("|" if c % self.per_bar == 0 else "") + cell(c).ljust(self.widths[c], fill)
                       for c in cols) + "|"

    def _label_row(self, cols, text: list[str]) -> str:
        """Free-floating labels (chords, bar numbers) aligned to their column."""
        buf, pos = list(self._row(cols, lambda c: "", " ")), 0
        for c in cols:
            pos += 1 if c % self.per_bar == 0 else 0
            for i, ch in enumerate(text[c][: len(buf) - pos]):
                buf[pos + i] = ch
            pos += self.widths[c]
        return "".join(buf).rstrip()

    def line_rows(self, line: int) -> list[str]:
        """The rendered rows of one system: chords, strings (high on top), bars."""
        cols = self.cols_of_line(line)
        rows = ["   " + self._label_row(cols, self.names)]
        for s in reversed(range(self.n_str)):
            rows.append(f"{self.labels[s]:<2} " + self._row(cols, lambda c: self.grid[s][c], "-"))
        rows.append("   " + self._label_row(cols, self.bars))
        return rows

    def render(self) -> str:
        """The full tab: every system's rows, stacked with a blank line between."""
        out = []
        for line in range(self.n_lines):
            out.extend(self.line_rows(line))
            out.append("")
        return "\n".join(out).rstrip()


def render_tab(fretted: list[Fretted], instrument: str = "guitar", **kw) -> str:
    """ASCII tab on a beat grid. `subdiv` = grid steps per beat (4 = 16ths).

    Chord names sit above the grid, aligned to the beat they start on.
    `first_bar` is the bar number the passage starts on in the song, so the
    numbers under the staff are the ones you can seek to.
    """
    if not fretted:
        return f"(no notes fall in {instrument} range)"
    return TabLayout(fretted, instrument, **kw).render()


# --- staff notation (for stems a fretboard would misrepresent) -------------

# Line/space slot for each natural, counted up from a clef's bottom line
# (slot 0). Accidentals sit on their natural's slot - a text staff has no
# room to shift a sharp sideways, so `_spelling` prints the accidental in
# the note's name instead of moving the head.
_NATURAL_SLOT = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}
_MIDDLE_C = 60          # C4
CLEFS = {"treble": 64, "bass": 43}     # bottom line: E4, G2
N_STAFF_LINES = 5


def pick_clef(notes: list[Note]) -> str:
    """Treble or bass, judged the same way `pick_instrument` picks guitar or
    bass: by where the notes actually sit, against middle C."""
    if not notes:
        return "treble"
    return "bass" if float(np.median([n.pitch for n in notes])) < _MIDDLE_C else "treble"


def _slot(pitch: int, clef: str) -> int:
    """Line/space slot on `clef`, 0 at that clef's bottom line, rising one
    per natural letter name (so an octave is 7 slots, not 12 semitones)."""
    bottom = CLEFS[clef]
    letter, octave = NOTE_NAMES[pitch % 12][0], pitch // 12 - 1
    ref_letter, ref_octave = NOTE_NAMES[bottom % 12][0], bottom // 12 - 1
    return (octave - ref_octave) * 7 + (_NATURAL_SLOT[letter] - _NATURAL_SLOT[ref_letter])


def _spelling(pitch: int) -> str:
    """Note name without octave, e.g. 'C#', for printing at a staff position."""
    return NOTE_NAMES[pitch % 12]


class StaffLayout:
    """A text staff: one clef, sized to the notes actually in the window
    (plus a couple of ledger rows either side), not a fixed grand-staff span.

    Piano (and anything else without a `TUNINGS` entry) has no fretboard to
    place notes on, so this is the `--stem piano` counterpart to `TabLayout`:
    same beat-subdivided column grid and bar wrapping (`col_of`/`time_of` are
    identical), but each row is a staff line/space instead of a string, and a
    note is a letter name sitting in its slot rather than a fret number.
    Clef is picked once per window (`pick_clef`), the way guitar/bass tuning
    is picked once per window today - a part doesn't change hands mid-phrase.
    """

    LEDGER_PAD = 2   # rows of ledger space kept above/below the outermost note

    def __init__(self, notes: list[Note], *, clef: str | None = None,
                 tempo: float = 120.0, t0: float = 0.0, beats_per_bar: int = 4,
                 subdiv: int = 4, max_width: int = 92, first_bar: int = 1,
                 chords: list | None = None, min_cols: int = 0):
        self.instrument = "staff"   # so callers that branch on TabLayout.instrument (e.g. bass synth voice) still work
        self.clef = clef or pick_clef(notes)
        self.tempo, self.t0 = tempo, t0
        self.subdiv, self.beats_per_bar, self.first_bar = subdiv, beats_per_bar, first_bar
        self.per_bar = beats_per_bar * subdiv
        notes = sorted(notes, key=lambda n: (n.start, n.pitch))
        self.notes = notes          # see TabLayout.fretted

        slots = [_slot(n.pitch, self.clef) for n in notes]
        top_line = (N_STAFF_LINES - 1) * 2         # the staff's top line, in row-slots
        lo = min([0] + slots) - self.LEDGER_PAD
        hi = max([top_line] + slots) + self.LEDGER_PAD
        # rows run top (highest slot) to bottom, one row per line *and* space
        self.n_rows = hi - lo + 1
        self._lo = lo

        cols = max([self.col_of(n.start) for n in notes] + [min_cols - 1]) + 1
        self.n_cols = max(self.per_bar, int(np.ceil(cols / self.per_bar) * self.per_bar))

        self.grid = [[""] * self.n_cols for _ in range(self.n_rows)]
        for n in notes:
            if 0 <= (c := self.col_of(n.start)) < self.n_cols:
                row = self._row_of(_slot(n.pitch, self.clef))
                self.grid[row][c] = _spelling(n.pitch)

        self.chord_names = [""] * self.n_cols
        for ch in chords or []:
            if 0 <= (c := self.col_of(ch.start)) < self.n_cols and ch.name != "N.C.":
                self.chord_names[c] = ch.name

        self.widths = [max((len(self.grid[r][c]) for r in range(self.n_rows)), default=0) + 1
                       for c in range(self.n_cols)]
        bar_w = [1 + sum(self.widths[b:b + self.per_bar])
                 for b in range(0, self.n_cols, self.per_bar)]
        self.per_line = max(1, min(len(bar_w), (max_width - 6) // max(bar_w)))
        self.bars = [f"bar {c // self.per_bar + first_bar}" if c % self.per_bar == 0 else ""
                     for c in range(self.n_cols)]

    # --- time <-> column (identical to TabLayout, kept in step deliberately) ---
    def col_of(self, t: float) -> int:
        return int(round((t - self.t0) * self.tempo / 60.0 * self.subdiv))

    def time_of(self, col: int) -> float:
        return self.t0 + col * 60.0 / self.tempo / self.subdiv

    def _row_of(self, slot: int) -> int:
        """Staff slot -> row index, counting down from the top."""
        return self._lo + self.n_rows - 1 - slot

    def _is_line(self, row: int) -> bool:
        """Whether a row (a staff line, a space, or a ledger row above/below
        the five printed lines) falls on a drawn line - printed staff lines
        sit on even slots (0, 2, 4, 6, 8), and so does every ledger line."""
        slot = self._lo + (self.n_rows - 1 - row)
        return slot % 2 == 0

    # --- rendering ----------------------------------------------------------
    def line_of(self, col: int) -> int:
        return int(col // (self.per_bar * self.per_line))

    def cols_of_line(self, line: int) -> range:
        span = self.per_bar * self.per_line
        return range(line * span, min((line + 1) * span, self.n_cols))

    @property
    def n_lines(self) -> int:
        return int(np.ceil(self.n_cols / (self.per_bar * self.per_line)))

    def _row_width(self, cols) -> int:
        """Character width of one rendered row: every cell plus a separator
        before each bar-start column and a closing one at the end - the same
        convention `TabLayout._row` uses, so line/space/label rows all agree."""
        return sum(self.widths[c] for c in cols) + sum(1 for c in cols if c % self.per_bar == 0) + 1

    def _row(self, cols, row: int) -> str:
        """One rendered staff row: a bar separator ('|') before every bar-start
        column on a drawn line (a plain space on a space, so only the five
        printed staff lines - plus ledger lines - draw a barline), each cell
        padded to its column's width, closed the same way."""
        fill, sep = ("-", "|") if self._is_line(row) else (" ", " ")
        cells = "".join((sep if c % self.per_bar == 0 else "")
                        + self.grid[row][c].ljust(self.widths[c], fill) for c in cols)
        return cells + sep

    def _label_row(self, cols, text: list[str]) -> str:
        """Free-floating labels (chords, bar numbers) aligned to their column."""
        buf, pos = [" "] * self._row_width(cols), 0
        for c in cols:
            pos += 1 if c % self.per_bar == 0 else 0
            for i, ch in enumerate(text[c][: len(buf) - pos]):
                buf[pos + i] = ch
            pos += self.widths[c]
        return "".join(buf).rstrip()

    def _gutter(self, row: int) -> str:
        """Left-edge label: the clef, and 'C' at middle C (if it's in range)."""
        slot = self._lo + (self.n_rows - 1 - row)
        if slot == _slot(_MIDDLE_C, self.clef):
            return "C  "
        if row == 0:
            return f"{self.clef[:2]} "
        return "   "

    def line_rows(self, line: int) -> list[str]:
        """The rendered rows of one system: chords, the staff (high to low,
        middle C labelled if present), bars."""
        cols = self.cols_of_line(line)
        rows = ["   " + self._label_row(cols, self.chord_names)]
        for row in range(self.n_rows):
            rows.append(self._gutter(row) + self._row(cols, row))
        rows.append("   " + self._label_row(cols, self.bars))
        return rows

    def render(self) -> str:
        out = []
        for line in range(self.n_lines):
            out.extend(self.line_rows(line))
            out.append("")
        return "\n".join(out).rstrip()


def render_staff(notes: list[Note], **kw) -> str:
    """Text staff for a stem with no fretboard (piano, vocals, other): each
    note prints as a letter name in its line/space slot rather than a fret
    number. `kw` matches `render_tab` - tempo/t0/subdiv/first_bar/chords."""
    if not notes:
        return "(no notes in this window)"
    return StaffLayout(notes, **kw).render()


def tab_for(notes: list[Note], instrument: str | None = None, **kw) -> str:
    """Tab a note list; `instrument=None` picks guitar or bass from the range."""
    instrument = instrument or pick_instrument(notes)
    return render_tab(fret_notes(notes, instrument), instrument, **kw)


# --- chord shapes -------------------------------------------------------------

def _fmt(frets: list) -> str:
    """'x32010' style, bracketing frets above 9 so it stays unambiguous."""
    return "".join("x" if f is None else (str(f) if f < 10 else f"({f})") for f in frets)


def chord_frets(name: str, root: int, quality: str) -> list[int | None] | None:
    """Preferred fingering: the open voicing if there is one, else the lowest
    movable barre shape. `None` entries are muted strings."""
    if name in OPEN_CHORDS:
        return [None if c == "x" else int(c) for c in OPEN_CHORDS[name]]

    best = None
    for anchor in (0, 1):                       # E-shape, then A-shape
        shape = MOVABLE_SHAPES.get((quality, anchor))
        if shape is None:
            continue
        base = (root - TUNINGS["guitar"][anchor]) % 12
        top = base + max(f for f in shape if f is not None)
        if top > 15 or (best is not None and base >= best[0]):
            continue
        best = (base, [None if f is None else base + f for f in shape])
    return best[1] if best else None


def chord_voicing(name: str, root: int, quality: str) -> list[int]:
    """MIDI pitches of that fingering, low string to high - what you'd hear."""
    frets = chord_frets(name, root, quality) or []
    return [TUNINGS["guitar"][i] + f for i, f in enumerate(frets) if f is not None]


def chord_shape(name: str, root: int, quality: str) -> str | None:
    """One printable line for a chord chart, e.g. 'G      320003'."""
    frets = chord_frets(name, root, quality)
    return f"{name:<6} {_fmt(frets)}" if frets else None


def chord_chart(chords: list) -> str:
    """One fingering per distinct chord in a progression."""
    seen, rows = set(), []
    for ch in chords:
        if ch.name in seen or ch.root < 0:
            continue
        seen.add(ch.name)
        if (s := chord_shape(ch.name, ch.root, ch.quality)):
            rows.append("  " + s)
    return "\n".join(rows) if rows else "(no chord shapes)"


def scale_notes(key: str) -> list[str]:
    """Pentatonic note pool for a key - handy context for solo prompts."""
    root_name, mode = key.split()
    root = NOTE_NAMES.index(root_name)
    steps = [0, 3, 5, 7, 10] if mode == "minor" else [0, 2, 4, 7, 9]
    return [NOTE_NAMES[(root + s) % 12] for s in steps]
