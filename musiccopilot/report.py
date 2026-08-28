"""Terminal rendering of everything the analysis found."""
from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import notes as nt
from .analysis import common_progressions, find_riffs
from .form import bar_edges, bar_index, reference_part
from .tabs import chord_chart, pick_instrument, scale_notes, tab_for

console = Console()


def log(msg) -> None:
    """Progress line; markup off so exception text can't be eaten as a tag."""
    console.print(str(msg), markup=False, highlight=False)


def _mmss(t: float) -> str:
    """Seconds as m:ss."""
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def markdown(text: str) -> None:
    """Render a markdown string (e.g. chart.md) to the terminal."""
    console.print(Markdown(text))


def _bars(song):
    """Bar grid to resolve positions against: the form's if there is one, else raw downbeats."""
    return song.form.bar_times if song.form else bar_edges(song.analysis)


def bar_number(song, t: float) -> int:
    """Which bar a time falls in, 1-based."""
    return bar_index(_bars(song), t)


def window_title(song, start: float, end: float, prefix: str = "") -> str:
    """`Verse 2 · bars 61-80 · 1:56-2:35` - so a passage can be found either way."""
    first, last = bar_number(song, start), bar_number(song, end - 0.1)
    return f"{prefix}bars {first}–{max(first, last)} · {_mmss(start)}–{_mmss(end)}"


def form(song) -> None:
    """The song's shape: what repeats, where, and on which chords."""
    if not song.form:
        console.print("[dim]no form detected yet - run analyze[/]")
        return structure(song)
    f = song.form
    console.print(Panel(f.outline().replace("->", "→"),
                        title=f"form · {song.path.stem}", expand=False))

    t = Table(header_style="bold")
    for c in ("part", "bars", "time", "len", "key", "chords"):
        t.add_column(c)
    for p in f.parts:
        mark = (f"  [yellow](transposed {p.transpose:+d})[/]" if p.transpose else
                "  [yellow](varies)[/]" if p.varies else "")
        t.add_row(f"[bold]{p.name}[/]", f"{p.bar}–{p.bar + p.bars - 1}",
                  f"{_mmss(p.start)}–{_mmss(p.end)}", f"{p.bars}b", p.key,
                  p.loop_text() + mark)
    console.print(t)

    rows = []
    for role, parts in f.roles().items():
        ref = reference_part(parts)
        rows.append(f"[bold]{role}[/] ×{len(parts)}  {ref.loop_text()}")
    console.print(Panel("\n".join(rows), title="one line per part", expand=False))
    snips = song.work / "snippets"
    if snips.exists():
        pick = next((p for p in f.parts if p.kind == "instrumental"), f.parts[-1])
        console.print(f"[dim]snippets → {snips}[/]\n[dim]tab one with:[/] "
                      f"python -m musiccopilot tab {song.path.name} "
                      f'--part "{pick.name.lower()}" --stem guitar')


def overview(song) -> None:
    """Print length, tempo, key, time signature and which stems are available."""
    a = song.analysis
    body = (f"[bold]{song.path.name}[/]\n"
            f"length {_mmss(a.duration)}   tempo {a.tempo:.1f} BPM   "
            f"key [bold]{a.key}[/]   {a.beats_per_bar}/4\n"
            f"pentatonic pool: {' '.join(scale_notes(a.key))}\n"
            f"stems: {', '.join(sorted(song.stems)) or 'none (run with separation)'}")
    console.print(Panel(body, title="overview", expand=False))


def structure(song) -> None:
    """Print the raw detected sections (no form/roles yet), each with its chord progression."""
    t = Table(title="structure", header_style="bold")
    for c in ("part", "start", "end", "type", "chords"):
        t.add_column(c)
    for s in song.analysis.sections:
        prog = " ".join(song.analysis.progression(s.start, s.end)[:8])
        t.add_row(s.label, _mmss(s.start), _mmss(s.end), s.kind, prog)
    console.print(t)


def chords(song, limit: int = 60) -> None:
    """Print the beat-synced chord track plus fretboard shapes for each chord seen."""
    t = Table(title="chord track", header_style="bold")
    for c in ("time", "bars", "chord"):
        t.add_column(c)
    spb = 60.0 / song.analysis.tempo * song.analysis.beats_per_bar
    for ch in song.analysis.chords[:limit]:
        t.add_row(_mmss(ch.start), f"{(ch.end - ch.start) / spb:.1f}", ch.name)
    console.print(t)
    console.print(Panel(chord_chart(song.analysis.chords),
                        title="guitar shapes (low E → high e)", expand=False))


def patterns(song) -> None:
    """Print the most-repeated chord loops, and Gemini's listening notes if there are any."""
    lines = []
    for n in (2, 4, 8):
        for prog, count in common_progressions(song.analysis.chords, n, top=2):
            lines.append(f"×{count:<3} {prog}")
    console.print(Panel("\n".join(lines) or "(no repeated loops found)",
                        title="repeated progressions", expand=False))
    if song.llm_notes:
        console.print(Panel(song.llm_notes, title="Gemini listening notes", expand=False))


def instruments(song) -> None:
    """Print per-stem note counts, pitch range and the most-used pitch classes."""
    t = Table(title="instruments", header_style="bold")
    for c in ("stem", "notes", "range", "notes/sec", "most used"):
        t.add_column(c)
    for stem, ns in song.notes.items():
        s = nt.summarise(ns)
        if not s.get("count"):
            continue
        t.add_row(stem, str(s["count"]), s["range"], str(s["notes_per_second"]),
                  " ".join(s["pitch_classes"][:6]))
    console.print(t)


def riffs(song, stem: str = "guitar", count: int = 3) -> None:
    """Auto-detect the busiest passages of a stem and print them as tab."""
    ns = song.notes.get(stem)
    if not ns:
        console.print(f"[dim]no transcribed notes for '{stem}'[/]")
        return
    a = song.analysis
    inst = "bass" if stem == "bass" else ("guitar" if stem == "guitar" else pick_instrument(ns))
    found = find_riffs(ns, top=count)
    if not found:
        console.print(f"[dim]no dense passages found in '{stem}'[/]")
    for start, end, n in found:
        window = nt.in_window(ns, start, end)
        tab = tab_for(window, inst, tempo=a.tempo, t0=start,
                      beats_per_bar=a.beats_per_bar, first_bar=bar_number(song, start),
                      chords=[c for c in a.chords if c.end > start and c.start < end])
        console.print(Panel(tab, title=f"{stem} riff @ {_mmss(start)} · {inst} ({n} notes)",
                            expand=False))


def lyrics(song, limit: int = 40) -> None:
    """Print the transcribed lyric lines with their timestamps."""
    if not song.lyrics:
        console.print("[dim]no lyrics (no vocal stem, or instrumental)[/]")
        return
    body = "\n".join(f"[dim]{_mmss(l.start)}[/] {l.text}" for l in song.lyrics[:limit])
    console.print(Panel(body, title="lyrics", expand=False))


def full(song) -> None:
    """Print everything: overview, form/structure, chords, patterns, instruments, riffs, lyrics."""
    overview(song)
    form(song) if song.form else structure(song)
    chords(song)
    patterns(song)
    instruments(song)
    lead = "guitar" if "guitar" in song.notes else next(iter(song.notes), None)
    if lead:
        riffs(song, lead)
    if "bass" in song.notes:
        riffs(song, "bass", count=1)
    lyrics(song)


def solo(solo_obj, tab: str, wav_path, midi_path) -> None:
    """Print a generated solo: Gemini's explanation, the tab, and where the audio/midi landed."""
    console.print(Panel(f"[bold]{solo_obj.title}[/]\nscale: {solo_obj.scale}\n\n"
                        f"{solo_obj.explanation}", title="Gemini solo", expand=False))
    console.print(Panel(tab, title="tab", expand=False))
    console.print(f"audio → [bold]{wav_path}[/]\nmidi  → {midi_path}")
