"""Chords, lines, and telling one from the other.

A guitar part has a *texture*: a rhythm player strikes several strings at once
and a lead player plays one note at a time. Nothing upstream of here knows
that. A note tracker answers "what pitch is sounding?" one frame at a time, so
a strummed E5 comes back as three unrelated notes that happen to start near
each other, and the two facts a reader most wants - *these were struck
together* and *that one was not* - are the two the note list does not carry.

This module recovers them, and everything else here is downstream of that one
grouping.

**Why it matters twice over.** A strum whose notes are not grouped is printed
as an arpeggio: `tabs` places a note in the column its onset falls in, and a
separated stem's strum arrives smeared across 58ms at the median and 151ms at
the 90th percentile (measured over this repo's material), which at any ordinary
tempo is one to two sixteenth columns. So one strummed chord becomes a
cascade down the staff - the single most visible way a tab here reads as
something nobody played. And a strum whose notes are not grouped is also
*split between players*: `voices.py` clusters notes, one at a time, on cues
that vary across the strings of one chord, so the same chord can land half on
`guitar` and half on `guitar-2`. Six strings and one pick is one player, and
the fix for both is the same - stop treating a note as the unit.

**What makes a strum, and what does not.** Two stages. First the notes that
arrived at once are clustered in time, then that cluster is cut into the
voicings inside it.

- **`link` is the pick crossing one string to the next, `span` is how long the
  whole stroke may take.** The chained one is the load-bearing half. A plain
  window anchored on the first note cannot tell a chord from a fast run at
  all - a sixteenth at 120bpm is 125ms, which is inside any window wide enough
  to catch a smeared strum - whereas a strum's *successive* onsets are tens of
  milliseconds apart because that is how long a pick takes to travel, and a
  run's are a musical subdivision apart however fast the tempo. Measured on a
  legato solo over a strummed part (ground truth below), an anchored 120ms
  window welded 4% of the solo's notes into chords that were never played; the
  same window chained at 60ms welds none. `link` is calibrated on real DI
  takes rather than on a synthetic strum, which is faster than anyone actually
  plays: 60ms finds a chord in 54% of a rhythm take's events against 49% at
  40ms, leaves a lead take at 5% either way, and is the last value before a
  legato solo starts to fuse.
- **No two adjacent pitches in the cluster are more than `max_gap` apart**, or
  it is cut there. This is a fact about guitars rather than a fitted
  threshold: a voicing sits on adjacent strings, so sorting its pitches gives
  neighbours 3-7 semitones apart. Measured over every shape in
  `config.OPEN_CHORDS` and `config.MOVABLE_SHAPES` - 236 intervals - the
  largest gap any of them has is 11 semitones, in the one `maj7` shape with a
  muted string in the middle, and 99% are 8 or under. An octave is therefore
  wider than every chord this repo knows how to print, and a wider gap than
  that means the two notes are not neighbours in one shape: they are a chord
  and something else sounding over it.

Cutting the cluster afterwards rather than testing each note as it joins is
what makes the grouping independent of the order the notes arrive in. A note
an octave and a half over a bare fifth looks unreachable until the third note
of the chord turns up in between, and a test applied one note at a time gets a
different answer depending on which of them the tracker happened to emit
first.

**Ground truth.** A known power-chord part and a known lead line, rendered
separately, summed, transcribed, and each transcribed note traced back to the
part it came from - in three mixes: panned apart, both dead centre, and a fast
legato solo whose notes ring into each other. The measure is the *ceiling* the
grouping puts on any later attempt to separate the two players: the share of
notes that would land right if every group went wholly to the player most of
it came from. Simultaneity alone caps that at 78%, because a fifth of the
groups already hold both players. The octave cut lifts it to 95-99% while
still finding a chord in about half of all events, and welds none of the solo.

Two tests were built, measured and dropped. Requiring a group's notes to *ring
together* changed nothing (a legato line's notes overlap exactly as a chord's
do), and asking `tabs._placement` whether one hand could hold the group bought
nothing at all - 79.2% against 79.2% - because the open low E and the open
high e are two octaves apart and perfectly reachable, so "one hand could play
it" is not "one hand did".

**Alignment moves onsets and nothing else.** A grouped strum is written back
with every note starting at the earliest of them - the moment the pick reached
the first string, which is the beat a player counts and the only one of the
onsets that is not late. Ends are left exactly as they were: a chord whose
bass note rings under a damped top is a real thing, and the tab reads onsets.
The pass is idempotent - it is run to a fixed point for exactly that reason -
so re-running it over notes that have been through it changes nothing.

This is deliberately *not* part of `clean.py`. That module's whole contract is
that it only ever removes, because everything it knows is evidence that
something is not there; this one moves notes and removes none, on evidence
that is musical rather than acoustic. They run one after the other and stay
separate modules for that reason.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from .config import TEXTURE
from .notes import Note

# Bumped whenever the grouping or the alignment changes what it emits.
# `pipeline.transcribe_notes` treats a stem whose cached notes were shaped by
# an older revision as a cache miss, exactly as it does for a different
# backend or an older `clean.REVISION` - without that, an improvement here
# would only ever reach songs nobody had analysed yet.
REVISION = 1

#: How many times `align` may re-group before giving up on settling. Two is
#: enough for everything measured here; the cap is only so that a pathological
#: list cannot spin.
_ALIGN_PASSES = 4


def strums(notes: list[Note]) -> list[list[Note]]:
    """Group `notes` into the events they were struck as, in onset order.

    Every note comes back in exactly one group, and a note nobody struck
    anything else with is a group of one - so this is a partition, and a
    caller can flatten it without checking what it got. Groups are in onset
    order and each is sorted by pitch, the order everything downstream reads a
    chord in.
    """
    link, span, max_gap = TEXTURE["link"], TEXTURE["span"], TEXTURE["max_gap"]
    out: list[list[Note]] = []
    for cluster in _clusters(notes, link, span):
        out.extend(_voicings(cluster, max_gap))
    return sorted(out, key=lambda g: (g[0].start, g[0].pitch))


def _clusters(notes: list[Note], link: float, span: float) -> list[list[Note]]:
    """Notes that arrived at once: chained by `link`, bounded by `span`."""
    clusters: list[list[Note]] = []
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        cur = clusters[-1] if clusters else None
        if (cur is not None
                and n.start - cur[-1].start <= link
                and n.start - cur[0].start <= span
                # One string each: the same pitch twice at the same instant is
                # two hands, because nothing on a fretboard can play it.
                and all(q.pitch != n.pitch for q in cur)):
            cur.append(n)
        else:
            clusters.append([n])
    return clusters


def _voicings(cluster: list[Note], max_gap: int) -> list[list[Note]]:
    """Cut one simultaneity into the shapes inside it, at the wide pitch gaps."""
    group = sorted(cluster, key=lambda n: n.pitch)
    out, part = [], [group[0]]
    for prev, n in zip(group, group[1:]):
        if n.pitch - prev.pitch > max_gap:
            out.append(part)
            part = [n]
        else:
            part.append(n)
    out.append(part)
    return out


def align(notes: list[Note]) -> list[Note]:
    """Pull each strum's notes onto the one onset they were struck at.

    The earliest onset in the group wins: that is when the pick reached the
    first string, it is the beat the player counted, and it is the only
    candidate that never moves a note *later* into the beat after it. Returns
    copies in the caller's own order - the note list is usually
    `Song.notes[stem]` and is not this function's to edit in place.

    **Run to a fixed point, because one pass is not one.** Pulling a strum
    together changes the order the notes are in, and `strums` chains its
    window from each note to the next, so a group can become reachable on the
    second pass that was not on the first. Left at one pass this shows up
    rarely (one note in 1071 on one cached stem here) and is still wrong in a
    way that matters: `notes_clean.json` re-shapes a cache when either
    revision moves, so re-running this over notes that have already been
    through it has to be a no-op, or a stem would drift a note at a time every
    time the checking changed. Onsets only ever move earlier and only ever
    onto an onset that was already in the list, so the loop is monotone on a
    finite set and settles - in practice on the second pass.
    """
    if not notes:
        return notes
    out = notes
    for _ in range(_ALIGN_PASSES):
        moved: dict[int, float] = {}
        for group in strums(out):
            if len(group) > 1:
                t = min(n.start for n in group)
                moved.update({id(n): t for n in group if n.start != t})
        if not moved:
            break
        out = [replace(n, start=moved[id(n)]) if id(n) in moved else n for n in out]
    return out


def chordness(notes: list[Note]) -> np.ndarray:
    """How chordal each note is, in the order `notes` came in: 0..1.

    A note struck on its own scores 0, one of a pair (a power chord's root and
    fifth, the smallest thing anyone strums) scores 0.5, and one of three or
    more scores 1. It is a per-note cue rather than a per-player verdict
    because that is what `voices.py` needs to average over a player, and it is
    graded rather than a flag because a two-note group is genuinely weaker
    evidence of a rhythm part than a five-note one.
    """
    out = np.zeros(len(notes), dtype=np.float32)
    where = {id(n): i for i, n in enumerate(notes)}
    for group in strums(notes):
        score = min(1.0, (len(group) - 1) / 2.0)
        for n in group:
            out[where[id(n)]] = score
    return out


def role(scores: np.ndarray, weight: np.ndarray | None = None) -> str:
    """What a player with these per-note `chordness` scores is doing, in words.

    Weighted by what each note is worth where the caller has that, because a
    rhythm part's chords carry the energy and a stray transcription artefact
    should not get an equal vote in describing the player.
    """
    if not len(scores):
        return "silent"
    w = np.ones(len(scores)) if weight is None else np.asarray(weight, dtype=float)
    mean = float((scores * w).sum() / max(w.sum(), 1e-9))
    if mean >= TEXTURE["rhythm_at"]:
        return "chords"
    if mean <= TEXTURE["lead_at"]:
        return "single notes"
    return "chords and single notes"
