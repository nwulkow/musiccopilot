"""The recreate sheet: the least you need to know to play the song.

One section per *role* rather than per occurrence - a song with three choruses
gets one "Chorus" heading with its chord loop, and the repeats are listed
underneath with only what makes them different (usually nothing, sometimes a
transposition). Written to `chart.md` next to the rest of the analysis.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import notes as nt
from .form import parse_chord, reference_part, transpose_loop
from .tabs import chord_shape, pick_instrument, tab_for

SEMITONE_WORDS = {1: "a half step", 2: "a whole step", 3: "three half steps",
                  4: "two whole steps", 5: "a fourth", 7: "a fifth"}


def _mmss(t: float) -> str:
    """Seconds as m:ss."""
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def _cell(text: str) -> str:
    """A chord loop inside a markdown table - its pipes are not column breaks."""
    return text.replace("|", "\\|")


def _shift_text(semitones: int) -> str:
    """'a whole step higher' etc, for describing a transposed repeat of a loop."""
    word = SEMITONE_WORDS.get(abs(semitones), f"{abs(semitones)} half steps")
    return f"{word} {'higher' if semitones > 0 else 'lower'}"


def _shapes(loop: list[str]) -> str:
    """Fingerings for the distinct chords of a loop, in the order they arrive."""
    out, seen = [], set()
    for name in loop:
        root, quality = parse_chord(name)
        if root is None or name in seen:
            continue
        seen.add(name)
        if (shape := chord_shape(name, root, quality)):
            out.append(re.sub(r"\s+", " ", shape.strip()))
    return " · ".join(out)


def _lyrics_of(song, part) -> list[str]:
    """Words sung *in* this part - by the line's midpoint, so a line that spills
    over the boundary does not put lyrics under an instrumental."""
    if part.kind != "vocal":
        return []
    return [l.text for l in song.lyrics
            if part.start <= (l.start + l.end) / 2 < part.end]


def _tab_of(song, part, max_bars: int, subdiv: int = 4) -> str:
    """Tab the head of a part, from whichever stem is carrying it."""
    a = song.analysis
    stem = part.lead or ("guitar" if song.notes.get("guitar") else
                         max(song.notes, key=lambda s: len(song.notes[s]), default=""))
    ns = song.notes.get(stem)
    if not ns:
        return ""
    end = min(part.end, part.start + max_bars * 60.0 / a.tempo * a.beats_per_bar)
    window = nt.in_window(ns, part.start, end)
    if not window:
        return ""
    instrument = "bass" if stem == "bass" else "guitar" if stem == "guitar" else pick_instrument(window)
    body = tab_for(window, instrument, tempo=a.tempo, t0=part.start,
                   beats_per_bar=a.beats_per_bar, subdiv=subdiv, max_width=84,
                   first_bar=part.bar,
                   chords=[c for c in a.chords if c.end > part.start and c.start < end])
    where = (f"bars {part.bar}-{part.bar + max_bars - 1}" if end < part.end
             else f"bars {part.bar}-{part.bar + part.bars - 1}")
    source = stem if stem == instrument else f"{stem} stem, on {instrument}"
    return f"**Tab** ({source}, {where}):\n\n```\n{body}\n```\n"


def build(song, tabs: str = "instrumental", max_tab_bars: int = 8) -> str:
    """`tabs`: which parts get a tab printed - all | instrumental | none."""
    form, a = song.form, song.analysis
    out: list[str] = [
        f"# {song.path.stem} - how to play it", "",
        f"**{form.key}** · {a.tempo:.0f} BPM · {a.beats_per_bar}/4 · "
        f"{_mmss(a.duration)} · {len(form.bar_times) - 1} bars", "",
        "## Form", "", form.outline().replace("->", "→"), "",
        "| bars | time | part | chords |", "|---|---|---|---|",
    ]
    for p in form.parts:
        out.append(f"| {p.bar}-{p.bar + p.bars - 1} | {_mmss(p.start)}-{_mmss(p.end)} "
                   f"| {p.name} | {_cell(p.loop_text())} |")

    out += ["", "## The parts", ""]
    for role, parts in form.roles().items():
        ref = reference_part(parts)
        keys = {p.key for p in parts}
        where = (f"{len(parts)}x" if len(parts) > 1 else
                 f"bar {ref.bar} · {_mmss(ref.start)}-{_mmss(ref.end)}")
        out += [f"### {role}", "",
                f"{where} · {ref.bars} bars · {' / '.join(sorted(keys))}", "",
                f"**Chords:**  {ref.loop_text()}"]
        if (shapes := _shapes(ref.loop)):
            out += ["", f"**Shapes:**  {shapes}"]

        out += [""]
        for p in (parts if len(parts) > 1 else []):    # a single one is in the heading
            where = f"bar {p.bar}, {_mmss(p.start)}-{_mmss(p.end)}, {p.bars} bars"
            note = ""
            if p.transpose:
                shifted = " | ".join(transpose_loop(ref.loop, p.transpose))
                note = (f" - **same loop, {_shift_text(p.transpose)}**: | {shifted} |"
                        f"  ({_shapes(transpose_loop(ref.loop, p.transpose))})")
            elif p.varies:
                note = f" - **different chords**: {p.loop_text()}"
            elif p.bars != ref.bars:
                note = f" - same chords, {p.bars // max(1, len(ref.loop))} times round"
            out.append(f"- **{p.name}** ({where}){note}")

        if (lines := _lyrics_of(song, ref)):
            out += ["", f"**Words ({ref.name}):**", ""]
            out += [f"> {l}" for l in lines]
        want = tabs == "all" or (tabs == "instrumental" and ref.kind == "instrumental")
        if want and (tab := _tab_of(song, ref, max_tab_bars)):
            out += ["", tab]
        out += [""]

    if any(p.snippet for p in form.parts):
        out += ["## Snippets", "",
                "Every part is also a wav in `snippets/`, and any of them can be "
                "tabbed directly with `tab --part <name>`.", "",
                "| part | time | file |", "|---|---|---|"]
        out += [f"| {p.name} | {_mmss(p.start)}-{_mmss(p.end)} | `{p.snippet}` |"
                for p in form.parts if p.snippet]
    return "\n".join(out).rstrip() + "\n"


def write(song, **kw) -> Path:
    """Build the recreate sheet and save it to chart.md."""
    path = song.work / "chart.md"
    path.write_text(build(song, **kw))
    return path
