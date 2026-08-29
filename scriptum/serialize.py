"""Dataclasses and tab layouts -> JSON the Vue client can draw.

The important one is `layout_json`. The browser never recomputes where a note
sits: `TabLayout`/`StaffLayout` already solved that (`col_of`, the per-column
widths, the bar wrapping), and CLAUDE.md is explicit that the grid maths is
load-bearing - a caller that uses a different `t0` or `first_bar` shifts the
tab. So the layout stays the single source of truth and this module just reads
its geometry out: columns with their absolute time, bar number and chord, and
cells addressed by (row, column). The client draws that table and nothing else.
"""
from __future__ import annotations

from dataclasses import asdict

from musiccopilot.config import STRING_LABELS
from musiccopilot.tabs import (N_STAFF_LINES, StaffLayout, TabLayout, _cell,
                               _slot)


def note_json(n) -> dict:
    """One transcribed note, plus the derived fields the client would
    otherwise have to recompute (name, duration)."""
    return {
        "start": round(n.start, 4),
        "end": round(n.end, 4),
        "pitch": n.pitch,
        "name": n.name,
        "velocity": round(float(n.velocity), 3),
        "technique": n.technique,
        "bend": round(float(n.bend), 3),
    }


def chord_json(c) -> dict:
    """One chord span from the chord track."""
    return {"start": round(c.start, 3), "end": round(c.end, 3),
            "name": c.name, "root": c.root, "quality": c.quality}


def section_json(s) -> dict:
    """One raw `detect_structure` segment (the letter map, pre-naming)."""
    return {"start": round(s.start, 3), "end": round(s.end, 3),
            "label": s.label, "kind": s.kind, "energy": round(float(s.energy), 4)}


def line_json(l) -> dict:
    """One lyric line."""
    return {"start": round(l.start, 3), "end": round(l.end, 3), "text": l.text}


def part_json(p) -> dict:
    """One named part. `name` and `slug` are properties, so `asdict` misses
    them - but they are the two fields the client addresses parts by."""
    d = asdict(p)
    d["name"] = p.name
    d["slug"] = p.slug
    d["loop_text"] = p.loop_text()
    return d


def analysis_json(a) -> dict:
    """The cached analysis, minus the beat/downbeat arrays' full precision."""
    return {
        "duration": round(a.duration, 3),
        "tempo": round(a.tempo, 2),
        "beats_per_bar": a.beats_per_bar,
        "key": a.key,
        "beat_times": [round(t, 3) for t in a.beat_times],
        "downbeats": [round(t, 3) for t in a.downbeats],
        "chords": [chord_json(c) for c in a.chords],
        "sections": [section_json(s) for s in a.sections],
    }


def form_json(f) -> dict:
    """The arrangement: the bar grid plus the named parts sitting on it."""
    return {
        "tempo": round(f.tempo, 2),
        "beats_per_bar": f.beats_per_bar,
        "key": f.key,
        "bar_times": [round(t, 3) for t in f.bar_times],
        "parts": [part_json(p) for p in f.parts],
        "outline": f.outline(),
    }


# --- the grid ---------------------------------------------------------------

def _columns(layout, cells_by_col: dict[int, list[dict]]) -> list[dict]:
    """Every grid column, with the geometry the client needs to lay it out.

    `time` is what a play-along cursor is matched against, `line` is which
    wrapped system the column belongs to, and `bar`/`bar_start` drive the bar
    lines and their numbers. All of it comes from the layout.
    """
    out = []
    for c in range(layout.n_cols):
        bar_start = c % layout.per_bar == 0
        out.append({
            "i": c,
            "t": round(layout.time_of(c), 4),
            "line": layout.line_of(c),
            "bar": c // layout.per_bar + layout.first_bar,
            "bar_start": bar_start,
            "beat": (c % layout.per_bar) // layout.subdiv + 1,
            "on_beat": c % layout.subdiv == 0,
            "chord": (layout.names if isinstance(layout, TabLayout)
                      else layout.chord_names)[c] or None,
            "cells": cells_by_col.get(c, []),
        })
    return out


def _tab_cells(layout: TabLayout) -> dict[int, list[dict]]:
    """Fret cells keyed by column.

    Rows are strings in *rendered* order - high string first - which is how a
    tab is read and how `line_rows` prints it, so the client can index rows
    top-down without flipping anything.
    """
    n = layout.n_str
    out: dict[int, list[dict]] = {}
    for fr in layout.fretted:
        c = layout.col_of(fr.note.start)
        if not (0 <= c < layout.n_cols):
            continue
        out.setdefault(c, []).append({
            "row": n - 1 - fr.string,           # rendered top-down
            "text": _cell(fr),
            "fret": fr.fret,
            "string": fr.string,
            "pitch": fr.note.pitch,
            "name": fr.note.name,
            "technique": fr.note.technique,
            "bend": round(float(fr.note.bend), 3),
            "start": round(fr.note.start, 4),
            "end": round(fr.note.end, 4),
        })
    return out


def _staff_cells(layout: StaffLayout) -> dict[int, list[dict]]:
    """Note-name cells keyed by column, on staff rows."""
    out: dict[int, list[dict]] = {}
    for note in layout.notes:
        c = layout.col_of(note.start)
        if not (0 <= c < layout.n_cols):
            continue
        row = layout._row_of(_slot(note.pitch, layout.clef))
        if not (0 <= row < layout.n_rows):
            continue
        out.setdefault(c, []).append({
            "row": row,
            "text": note.name[:-1],             # spelling without the octave
            "pitch": note.pitch,
            "name": note.name,
            "technique": note.technique,
            "bend": round(float(note.bend), 3),
            "start": round(note.start, 4),
            "end": round(note.end, 4),
        })
    return out


def layout_json(layout, *, title: str = "", stem: str = "",
                start: float = 0.0, end: float = 0.0) -> dict:
    """A `TabLayout` or `StaffLayout` as a drawable grid.

    `kind` tells the client which renderer to use; `rows` describes the y axis
    (strings for a tab, staff lines/spaces for a staff) and `columns` the x
    axis. `text` is the layout's own ASCII rendering, kept so the client can
    offer a copy/paste view that is byte-identical to the CLI's.
    """
    is_tab = isinstance(layout, TabLayout)
    if is_tab:
        labels = STRING_LABELS[layout.instrument]
        rows = [{"label": labels[s], "line": True, "string": s}
                for s in reversed(range(layout.n_str))]
        cells = _tab_cells(layout)
    else:
        top = (N_STAFF_LINES - 1) * 2
        rows = [{"label": "", "line": layout._is_line(r),
                 # inside the five printed lines, as opposed to a ledger row
                 "staff": 0 <= (layout._lo + layout.n_rows - 1 - r) <= top}
                for r in range(layout.n_rows)]
        cells = _staff_cells(layout)

    return {
        "kind": "tab" if is_tab else "staff",
        "instrument": layout.instrument,
        "clef": None if is_tab else layout.clef,
        "stem": stem,
        "title": title,
        "start": round(start, 3),
        "end": round(end, 3),
        "tempo": round(layout.tempo, 2),
        "t0": round(layout.t0, 4),
        "subdiv": layout.subdiv,
        "beats_per_bar": layout.beats_per_bar,
        "per_bar": layout.per_bar,
        "per_line": layout.per_line,
        "first_bar": layout.first_bar,
        "n_cols": layout.n_cols,
        "n_rows": len(rows),
        "n_lines": layout.n_lines,
        "rows": rows,
        "columns": _columns(layout, cells),
        "text": layout.render(),
    }


# --- engraved notation ------------------------------------------------------

def _score_note(n) -> dict:
    """One written event. A rest is a note with no keys, which is how the
    renderer tells them apart - same as `ScoreNote` itself."""
    return {
        "keys": n.keys,
        "duration": n.duration,
        "dots": n.dots,
        "start": round(n.start, 4),
        "end": round(n.end, 4),
        "tie": n.tie,
        "pitches": n.pitches,
    }


def score_json(score, *, title: str = "", stem: str = "",
               start: float = 0.0, end: float = 0.0) -> dict:
    """A `score.Score` as engravable JSON.

    The counterpart to `layout_json` for stems that deserve real notation
    rather than a grid. The same division of labour applies, one level up:
    every musical decision - hands, note values, spelling, where the rests
    go - was made in `musiccopilot.score`, and the client only turns that
    into glyphs. It cannot decide, say, that a note is an eighth; by the time
    it sees one, it already is.
    """
    return {
        "kind": "score",
        "stem": stem,
        "title": title,
        "start": round(start, 3),
        "end": round(end, 3),
        "clefs": score.clefs,
        "key": score.key,
        "sig": score.sig,
        "time": score.time,
        "tempo": round(score.tempo, 2),
        "t0": round(score.t0, 4),
        "subdiv": score.subdiv,
        "beats_per_bar": score.beats_per_bar,
        "first_bar": score.first_bar,
        "measures": [{
            "number": m.number,
            "start": round(m.start, 4),
            "end": round(m.end, 4),
            "chord": m.chord,
            "voices": [[_score_note(n) for n in v] for v in m.voices],
        } for m in score.measures],
    }


def session_json(session) -> dict:
    """A DAW session on its way in: every track, and what it will become.

    Carries `why` for each row because the mapping is a *guess* the user is
    being asked to check, and "matched 'gtr'" is what makes an odd row
    correctable rather than merely wrong.
    """
    return {
        "source": str(session.source),
        "name": session.source.stem,
        "kind": session.kind,
        "mixdown": session.mixdown.name if session.mixdown else "",
        "warnings": session.warnings,
        "tracks": [{"name": t.name, "file": t.path.name, "stem": t.stem,
                    "why": t.why, "ignored": [p.name for p in t.extra]}
                   for t in session.tracks],
    }
