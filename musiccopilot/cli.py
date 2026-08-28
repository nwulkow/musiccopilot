"""Command line interface.

  python -m musiccopilot analyze  song.mp3 --llm
  python -m musiccopilot parts    song.mp3
  python -m musiccopilot chart    song.mp3
  python -m musiccopilot tab      song.mp3 --part "guitar solo"
  python -m musiccopilot tab      song.mp3 --stem guitar --start 1:02 --end 1:18
  python -m musiccopilot tab      song.mp3 --part "guitar solo" --follow
  python -m musiccopilot record   --instrument guitar
  python -m musiccopilot solo     song.mp3 --prompt "slow bluesy" --play
  python -m musiccopilot models

Positions (--start/--end) are seconds, mm:ss, or a bar number: `--start 62`,
`--start 1:02` and `--start bar17` are all valid. `--bars 17-24` is shorthand,
and `--part chorus2` uses the form the analysis found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

from . import chart, notes as nt, report, synth
from .config import SR
from .form import bar_edges, bar_start
from .pipeline import Song
from .tabs import TabLayout, fret_notes, pick_instrument, render_tab

_BAR = re.compile(r"^(?:bars?\s*(\d+)|(\d+)\s*bars?)$")


def _bar_times(song):
    """Bar grid to resolve positions against: the form's if there is one, else raw downbeats."""
    return song.form.bar_times if song.form else bar_edges(song.analysis)


def position(value, song) -> float | None:
    """A point in the song: seconds, mm:ss, or a bar number.

    Bars are what a tab prints, seconds are what a player reads off a media
    player, so both have to work.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if (m := _BAR.match(text)):
        return bar_start(_bar_times(song), int(m.group(1) or m.group(2)))
    if ":" in text:
        out = 0.0
        for chunk in text.split(":"):
            out = out * 60 + float(chunk)
        return out
    return float(text)


def _window(args, song) -> tuple[float, float, str]:
    """Resolve --part / --bars / --start / --end into one time range."""
    a = song.analysis
    start, end, title = None, None, ""

    if getattr(args, "part", None):
        part = song.part(args.part)
        if part is None:
            have = ", ".join(p.name for p in song.form.parts) if song.form else "none"
            raise SystemExit(f"no part called {args.part!r} (have: {have})")
        start, end, title = part.start, part.end, part.name
    if getattr(args, "bars", None):
        first, _, last = args.bars.partition("-")
        start = position(f"bar {first}", song)
        end = bar_start(_bar_times(song), int(last or first) + 1)   # through that bar
    if args.start is not None:
        start = position(args.start, song)
    if args.end is not None:
        end = position(args.end, song)

    start = 0.0 if start is None else start
    end = end if end is not None else min(start + 20, a.duration)
    return start, min(end, a.duration), title


def _grid_cols(song, start: float, end: float, a, subdiv: int) -> int:
    """How many grid columns a passage occupies, counted in bars.

    Part boundaries are rounded to 2dp and land a little after the downbeat
    they belong to, so `(end - start) * tempo` reliably overshoots by a
    fraction of a bar - which the grid then rounds up into an empty one.
    Counting bar lines instead keeps the tab exactly as long as the passage.
    """
    bars = _bar_times(song)
    n = sum(1 for t in bars if start - 0.05 <= t < end - 0.05)
    if n:
        return n * a.beats_per_bar * subdiv
    return int((end - start) * a.tempo / 60.0 * subdiv)


def _load(path, need_form: bool = True) -> Song:
    """Reload a song, running whatever stages are missing (which can be slow)."""
    song = Song.open(path)
    if song.analysis is None or (need_form and song.form is None):
        report.console.print("[yellow]Not analysed yet - running analyze first.[/]")
        song.run(log=report.log)
    return song


# --- commands ----------------------------------------------------------------

def cmd_analyze(args) -> None:
    """Run the full pipeline (stems, chords, notes, lyrics, form) and write the chart."""
    song = Song.open(args.file).run(
        separate=not args.no_separate, do_lyrics=not args.no_lyrics,
        do_notes=not args.no_notes, do_snippets=not args.no_snippets,
        llm=args.llm, force=args.force,
        whisper_size=args.whisper, device=args.device, log=report.log)
    if args.stem_snippets:
        song.write_snippets(stems=True, force=args.force)
    path = chart.write(song)
    report.console.print(f"[green]done[/] → everything in {song.work}")
    report.full(song)
    report.console.print(f"\nrecreate sheet → [bold]{path}[/]")


def cmd_parts(args) -> None:
    """Print the song's form: parts, bars, timestamps, chords."""
    song = _load(args.file)
    report.form(song)


def cmd_chart(args) -> None:
    """Write and print the recreate sheet (chart.md)."""
    song = _load(args.file)
    path = chart.write(song, tabs=args.tabs, max_tab_bars=args.tab_bars)
    report.markdown(path.read_text())
    report.console.print(f"\nsaved → [bold]{path}[/]")


def cmd_snippets(args) -> None:
    """Cut every part out as its own wav (and per-stem wavs with --stems)."""
    song = _load(args.file)
    written = song.write_snippets(stems=args.stems, force=args.force)
    report.console.print(f"[green]{len(written)} written[/] → {song.work / 'snippets'}"
                         if written else "[dim]snippets already there (--force to redo)[/]")
    report.form(song)


def cmd_show(args) -> None:
    """Print one section of the cached analysis, chosen by --what."""
    song = _load(args.file)
    if args.what == "riffs":
        report.riffs(song, args.stem)
        return
    {"all": report.full, "overview": report.overview, "chords": report.chords,
     "form": report.form, "structure": report.structure, "patterns": report.patterns,
     "instruments": report.instruments, "lyrics": report.lyrics}[args.what](song)


def cmd_tab(args) -> None:
    """Print (or play/follow) tablature for a part, a bar range or a time range."""
    song = _load(args.file)
    a = song.analysis
    ns = song.notes.get(args.stem)
    if not ns:
        report.console.print(f"[red]no notes for stem '{args.stem}'[/] "
                             f"(have: {', '.join(song.notes) or 'none'})")
        return
    start, end, title = _window(args, song)
    window = nt.in_window(ns, start, end)
    instrument = ("bass" if args.stem == "bass" else
                  "guitar" if args.stem == "guitar" else pick_instrument(window))
    layout = TabLayout(
        fret_notes(window, instrument), instrument, tempo=a.tempo, t0=start,
        beats_per_bar=a.beats_per_bar, subdiv=args.subdiv,
        first_bar=report.bar_number(song, start),
        chords=[c for c in a.chords if c.end > start and c.start < end],
        # The passage may end on a rest; the grid still has to reach the end of
        # it or the cursor runs off the last column while audio plays on. The
        # length comes from the *bar grid*, not from duration x tempo: a part
        # boundary sits a little past its last bar line, and the arithmetic
        # version turns that overhang into a whole extra empty bar.
        min_cols=_grid_cols(song, start, end, a, args.subdiv))
    heading = report.window_title(song, start, end,
                                  f"{title} · " if title else "") + f" · {args.stem}"
    if args.follow:
        return _follow(args, song, window, layout, start, end, heading)
    report.console.print(report.Panel(layout.render(), expand=False, title=heading))

    snippet = None
    part = song.part(args.part) if args.part else None
    if part and (others := [p.name for p in song.form.matching(args.part) if p is not part]):
        report.console.print(f"[dim]{part.name} of {len(others) + 1}; "
                             f"also: {', '.join(others)}[/]")
    if part and part.snippet and (wav := song.work / "snippets" / part.snippet).exists():
        snippet = wav                       # --play prefers the real recording
        report.console.print(f"[dim]recording → {wav}[/]")
    if args.audio:
        y = synth.render(window, SR, "bass" if instrument == "bass" else "clean", t0=start)
        out = synth.write(song.work / f"tab_{args.stem}_{int(start)}.wav", y)
        report.console.print(f"transcription audio → {out}")
        snippet = out
    if args.play and snippet and snippet.exists():
        synth.play(snippet)


def _follow(args, song, window, layout, start, end, heading) -> None:
    """Play the passage and scroll the tab under a cursor (`tab --follow`).

    What you hear is by default the *recording*, not the transcription: the
    point is to play along with the record. `--follow-source synth` swaps in
    the synthesised transcription, which is the useful one when you want to
    hear what the tab actually claims.
    """
    from . import audio, playalong
    from .config import SR as _SR

    a = song.analysis
    src = args.follow_source
    if src == "synth":
        y = synth.render(window, _SR, "bass" if layout.instrument == "bass" else "clean",
                         duration=end - start, t0=start)
    elif src == "stem" and args.stem in song.stems:
        y = audio.excerpt(audio.load(song.stems[args.stem], _SR, mono=True),
                          start, end, _SR, fade=0.01)
    else:
        if src == "stem":
            report.console.print(f"[yellow]no '{args.stem}' stem; using the mix[/]")
        y = audio.excerpt(song.audio(), start, end, _SR, fade=0.01)

    if args.minus_stem and song.stems:
        # practice bed: the song with your instrument taken out
        bed = song.backing(exclude=(args.stem,))
        y = audio.excerpt(bed, start, end, _SR, fade=0.01)

    speed = max(0.25, min(4.0, args.speed))
    if speed != 1.0:
        # Slowing down for practice must not drop the pitch - you are playing
        # along with it. Stretching also changes the clock the cursor runs on:
        # at half speed a bar takes twice as long, so the layout's tempo is
        # scaled to match and the grid still lines up with what is audible.
        import librosa
        y = librosa.effects.time_stretch(np.asarray(y, dtype=np.float32), rate=speed)
        layout.tempo = a.tempo * speed

    # The count-in is generated *after* the stretch, at the tempo you will
    # actually hear: clicking four beats and then stretching them counts you
    # in at the wrong speed, which is the one thing a count-in must not do.
    t0 = start
    if args.count_in:
        clicks = synth.click_track(args.count_in, layout.tempo, _SR, a.beats_per_bar)
        y = np.concatenate([clicks.astype(np.float32), np.asarray(y, dtype=np.float32)])
        t0 = start - len(clicks) / _SR

    report.console.print(f"[dim]{heading} — space of {report._mmss(end - start)}, "
                         f"{src}{' minus ' + args.stem if args.minus_stem else ''}"
                         f"{f', {speed:g}x' if speed != 1 else ''}"
                         f" · ctrl-c to stop[/]")
    transport = playalong.Transport(y, _SR, t0=t0, loop=args.loop)
    try:
        with transport:
            if args.follow_view == "notes":
                playalong.follow_notes(window, transport, title=heading)
            else:
                playalong.follow_tab(layout, transport, title=heading,
                                     count_in=args.count_in)
    except KeyboardInterrupt:
        pass


def cmd_solo(args) -> None:
    """Ask Gemini for a solo over a part (or the detected solo section) and render it to audio."""
    from .gemini import solo_to_notes, suggest_solo

    song = _load(args.file)
    a = song.analysis
    if args.start is None and args.end is None and not args.part and not args.bars:
        start, end = song.solo_section()
    else:
        start, end, _ = _window(args, song)
    report.console.print(f"[dim]soloing over {report.window_title(song, start, end)} "
                         f"({' '.join(a.progression(start, end)[:8])})[/]")

    extra = {"style_notes": song.llm_notes[:1200]} if song.llm_notes else None
    solo = suggest_solo(args.prompt, a, start, end, extra=extra,
                        temperature=args.temperature)
    solo_notes = solo_to_notes(solo, a.tempo, t0=start)

    tab = render_tab(fret_notes(solo_notes, "guitar"), "guitar", tempo=a.tempo, t0=start,
                     beats_per_bar=a.beats_per_bar, subdiv=args.subdiv,
                     chords=[c for c in a.chords if c.end > start and c.start < end])

    lead = synth.render(solo_notes, SR, "lead", duration=end - start + 2.0, t0=start)
    if args.over == "backing" and song.stems:
        bed = song.backing()[int(start * SR):int((end + 2) * SR)]
    elif args.over == "chords":
        bed = synth.render_chords([c for c in a.chords if c.end > start and c.start < end],
                                  SR, t0=start, duration=end - start + 2.0)
    else:
        bed = None
    y = synth.mix(lead, bed, gains=[1.0, args.bed_gain]) if bed is not None else synth.mix(lead)

    slug = "".join(ch for ch in args.prompt[:24] if ch.isalnum() or ch == " ").strip().replace(" ", "_")
    wav = synth.write(song.work / f"solo_{slug or 'take'}.wav", y)
    midi = nt.write_midi(solo_notes, song.work / f"solo_{slug or 'take'}.mid", a.tempo)
    report.solo(solo, tab, wav, midi)
    if args.play:
        synth.play(wav)


def cmd_record(args) -> None:
    """Capture playing from the mic; write the take as audio, notes and a tab.

    The take lands in its own `analyzed_songs/<name>/` folder with the same
    file names the offline pipeline uses (`notes/<stem>.json`, `chart.md`), so
    everything downstream - `tab`, `chart`, `show` - works on something you
    played exactly as it does on something you analysed.
    """
    import threading

    from . import audio, record
    from .tabs import fret_notes

    name = args.name or time.strftime("take-%Y%m%d-%H%M%S")
    out = Path(args.into).expanduser().resolve() if args.into else \
        Path.cwd() / "recordings" / name
    out.mkdir(parents=True, exist_ok=True)

    rec = record.Recorder(SR, device=args.device_in, max_seconds=args.max_seconds)
    state = record.LiveState()
    stop = threading.Event()

    report.console.print(f"[dim]recording to {out} — ctrl-c to stop[/]")
    if args.count_in:
        clicks = synth.click_track(args.count_in, args.tempo or 120.0, SR)
        synth.play(synth.write(out / "_countin.wav", clicks))

    worker = threading.Thread(
        target=record.analysis_worker, daemon=True,
        args=(rec, state, stop),
        kwargs={"instrument": args.instrument, "do_chords": not args.no_chords})
    try:
        with rec:
            worker.start()
            record.live_view(rec, state, stop, instrument=args.instrument,
                             view=args.view, tempo=args.tempo,
                             subdiv=args.subdiv)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=2.0)

    y = rec.audio()
    if not len(y):
        report.console.print("[yellow]nothing captured[/]")
        return
    wav = audio.save(out / "take.wav", y, SR)

    # The live pass only ever saw a few seconds at a time. Now the take is
    # complete, transcribe it whole: the offline segmenter can look forward,
    # which is what tells a bend from two notes, so the saved transcription is
    # better than anything shown while playing.
    report.console.print("[dim]transcribing the take…[/]")
    from .config import PITCH_RANGE
    fmin, fmax = PITCH_RANGE.get(args.instrument, PITCH_RANGE["other"])
    ns = nt._crepe_notes_from_audio(y, SR, fmin, fmax)

    tempo = args.tempo
    chords = []
    if not args.no_chords:
        import librosa
        est, beats = librosa.beat.beat_track(y=y, sr=SR, units="time")
        tempo = tempo or float(np.atleast_1d(est)[0])
        from .analysis import detect_chords
        chords = record.collapse_chords(detect_chords(y, SR, beats))
    tempo = tempo or 120.0

    (out / "notes").mkdir(exist_ok=True)
    (out / "notes" / f"{args.instrument}.json").write_text(
        json.dumps(nt.to_dicts(ns)))
    nt.write_midi(ns, out / "take.mid", tempo)

    instrument = "bass" if args.instrument == "bass" else "guitar"
    # fit the terminal: a tab wider than the console gets wrapped by the panel,
    # which interleaves the strings and makes it unreadable
    tab = render_tab(fret_notes(ns, instrument), instrument, tempo=tempo, t0=0.0,
                     subdiv=args.subdiv, chords=chords,
                     max_width=max(40, report.console.width - 6))
    lines = [f"# {name}", "", f"{len(ns)} notes · {rec.seconds:.1f}s · {tempo:.0f} bpm", ""]
    if chords:
        lines += ["## chords", "", "  " + " ".join(c.name for c in chords), ""]
    lines += ["## tab", "", "```", tab, "```", ""]
    (out / "chart.md").write_text("\n".join(lines))

    report.console.print(report.Panel(tab, expand=False,
                                      title=f"{name} · {tempo:.0f} bpm"))
    if chords:
        report.console.print("chords: [cyan]" + " ".join(c.name for c in chords) + "[/]")
    report.console.print(f"[green]saved[/] → {wav}  ·  {out / 'chart.md'}  ·  "
                         f"{out / 'take.mid'}")
    if rec.overflows:
        report.console.print(f"[yellow]{rec.overflows} input overruns "
                             f"(the take may have gaps)[/]")


def cmd_models(args) -> None:
    """Which Gemini models this key can actually use - set GEMINI_MODEL to one."""
    from google import genai

    from .config import GEMINI_MODEL, gemini_api_key
    for m in genai.Client(api_key=gemini_api_key()).models.list():
        if "generateContent" in (getattr(m, "supported_actions", None) or []):
            mark = " [green]← current[/]" if m.name.endswith(GEMINI_MODEL) else ""
            report.console.print(f"{m.name}{mark}")


# --- argument parsing ---------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Wire up the `musiccopilot` subcommands."""
    p = argparse.ArgumentParser("musiccopilot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="separate stems, transcribe, detect chords")
    a.add_argument("file")
    a.add_argument("--no-separate", action="store_true")
    a.add_argument("--no-lyrics", action="store_true")
    a.add_argument("--no-notes", action="store_true")
    a.add_argument("--no-snippets", action="store_true",
                   help="skip cutting each part out as a wav")
    a.add_argument("--llm", action="store_true", help="ask Gemini to listen to the track")
    a.add_argument("--force", action="store_true", help="ignore cache")
    a.add_argument("--whisper", default="base", help="tiny|base|small|medium|large-v3")
    a.add_argument("--device", default=None, help="cpu|cuda|mps")
    a.add_argument("--stem-snippets", action="store_true",
                   help="also cut every part into per-instrument snippets")
    a.set_defaults(func=cmd_analyze)

    f = sub.add_parser("parts", help="the song's form: parts, bars, timestamps, chords")
    f.add_argument("file")
    f.set_defaults(func=cmd_parts)

    c = sub.add_parser("chart", help="the minimal sheet needed to recreate the song")
    c.add_argument("file")
    c.add_argument("--tabs", default="instrumental",
                   choices=["all", "instrumental", "none"], help="which parts to tab")
    c.add_argument("--tab-bars", type=int, default=8, help="bars of tab per part")
    c.set_defaults(func=cmd_chart)

    n = sub.add_parser("snippets", help="cut every part out as its own wav")
    n.add_argument("file")
    n.add_argument("--stems", action="store_true", help="also one wav per instrument")
    n.add_argument("--force", action="store_true")
    n.set_defaults(func=cmd_snippets)

    s = sub.add_parser("show", help="print the cached analysis")
    s.add_argument("file")
    s.add_argument("--what", default="all",
                   choices=["all", "overview", "form", "chords", "structure",
                            "patterns", "instruments", "riffs", "lyrics"])
    s.add_argument("--stem", default="guitar")
    s.set_defaults(func=cmd_show)

    t = sub.add_parser("tab", help="tablature for a part, a bar range or a time range")
    t.add_argument("file")
    t.add_argument("--stem", default="guitar")
    t.add_argument("--part", default=None,
                   help="a part from `parts`: chorus, 'verse 2', 'guitar solo', #4")
    t.add_argument("--bars", default=None, help="bar range, e.g. 17-24")
    t.add_argument("--start", default=None, help="seconds, mm:ss, or bar N")
    t.add_argument("--end", default=None, help="seconds, mm:ss, or bar N")
    t.add_argument("--subdiv", type=int, default=4, help="grid steps per beat")
    t.add_argument("--audio", action="store_true", help="also synthesise the passage")
    t.add_argument("--play", action="store_true",
                   help="play the part's recording, or the synthesised tab with --audio")
    t.add_argument("--follow", action="store_true",
                   help="play along: scroll the tab under a live cursor")
    t.add_argument("--follow-view", default="tab", choices=["tab", "notes"],
                   help="fretboard tab, or plain note names (for non-guitar stems)")
    t.add_argument("--follow-source", default="mix", choices=["mix", "stem", "synth"],
                   help="what to hear while following: the record, one stem, or the tab")
    t.add_argument("--minus-stem", action="store_true",
                   help="play the song with --stem removed, so you play that part")
    t.add_argument("--count-in", type=int, default=0, metavar="BEATS",
                   help="click for this many beats before the passage starts")
    t.add_argument("--speed", type=float, default=1.0,
                   help="playback rate, pitch preserved (0.5 = half speed)")
    t.add_argument("--loop", action="store_true", help="repeat the passage until ctrl-c")
    t.set_defaults(func=cmd_tab)

    g = sub.add_parser("solo", help="generate a solo with Gemini and hear it")
    g.add_argument("file")
    g.add_argument("--prompt", required=True)
    g.add_argument("--part", default=None, help="solo over a named part")
    g.add_argument("--bars", default=None, help="bar range, e.g. 97-112")
    g.add_argument("--start", default=None, help="seconds, mm:ss, or bar N")
    g.add_argument("--end", default=None, help="seconds, mm:ss, or bar N")
    g.add_argument("--over", default="backing", choices=["backing", "chords", "none"])
    g.add_argument("--bed-gain", type=float, default=0.55)
    g.add_argument("--temperature", type=float, default=1.0)
    g.add_argument("--subdiv", type=int, default=4)
    g.add_argument("--play", action="store_true")
    g.set_defaults(func=cmd_solo)

    r = sub.add_parser("record", help="play into the mic: live notes, tabs and chords")
    r.add_argument("--name", default=None, help="folder name for the take")
    r.add_argument("--into", default=None, help="write the take here instead")
    r.add_argument("--instrument", default="guitar",
                   choices=["guitar", "bass", "vocals", "piano", "other"])
    r.add_argument("--view", default="tab", choices=["tab", "notes"],
                   help="live display: fretboard tab or a list of note names")
    r.add_argument("--tempo", type=float, default=0.0,
                   help="fix the tempo instead of tracking it (bpm)")
    r.add_argument("--subdiv", type=int, default=4, help="grid steps per beat")
    r.add_argument("--count-in", type=int, default=0, metavar="BEATS")
    r.add_argument("--no-chords", action="store_true",
                   help="skip chord detection (lighter; right for single notes)")
    r.add_argument("--device-in", default=None, help="input device name or index")
    r.add_argument("--max-seconds", type=float, default=1800.0)
    r.set_defaults(func=cmd_record)

    sub.add_parser("models", help="list Gemini models your key can use").set_defaults(
        func=cmd_models)
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args, dispatch to the matching cmd_*, and report errors tersely."""
    # --debug is not a subcommand flag: it is stripped before parsing so it can
    # be passed anywhere in the line, next to whichever argument is misbehaving.
    argv = list(sys.argv[1:] if argv is None else argv)
    debug = "--debug" in argv
    args = build_parser().parse_args([a for a in argv if a != "--debug"])
    np.random.seed(0)
    try:
        args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:                       # noqa: BLE001
        report.console.print(f"[red]error:[/] {exc}")
        if debug:
            raise
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
