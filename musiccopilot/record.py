"""Live capture: play into the mic and get notes, tabs and chords back.

`musiccopilot record` opens the input device, shows what it is hearing as it
hears it, and on ctrl-c writes the take to `analyzed_songs/<name>/` as a wav
plus the same `Note` list the rest of the pipeline uses - so a riff you just
played can be tabbed, charted and re-synthesised exactly like one lifted off a
record.

The design constraint that shapes everything here: transcription has to keep
up with the player. The offline path (`notes._crepe_notes`) sees a whole clip
and can afford to look forwards; a live one gets audio in 40ms blocks and must
decide "is that still the same note" with no future to look at. So capture and
analysis are split - the audio thread only ever appends to a ring buffer, and a
worker transcribes the *settled* tail behind the write head. Nothing that can
block or allocate runs in the callback: an underrun there is a hole in the
recording, and the recording is the one thing that cannot be recomputed.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .config import SR
from .notes import Note


# --- capture -----------------------------------------------------------------

class Recorder:
    """Mic -> a growing float32 buffer, with a lock-free-enough read window.

    The callback appends and nothing else. `tail()` hands the analysis thread a
    copy of the most recent audio, so the two never touch the same memory while
    the device is writing to it.
    """

    def __init__(self, sr: int = SR, channels: int = 1, device=None,
                 max_seconds: float = 60 * 30):
        """Preallocate the whole buffer up front - growing it inside the
        callback would mean an allocation on the audio thread, which is
        exactly the kind of thing that can turn into an underrun."""
        self.sr, self.channels, self.device = sr, channels, device
        self._buf = np.zeros((int(sr * max_seconds), channels), dtype=np.float32)
        self._n = 0
        self._lock = threading.Lock()
        self._stream = None
        self.overflows = 0
        self.peak = 0.0

    def _callback(self, indata, frames, _time, status):     # noqa: ANN001
        """Append to the buffer and nothing else - no transcription, no
        allocation. Analysis happens off-thread in `analysis_worker`; an
        underrun here is a hole in the take, and the take cannot be redone."""
        if status:
            self.overflows += 1
        n = self._n
        end = n + frames
        if end > len(self._buf):
            return                                  # buffer full: stop growing
        self._buf[n:end] = indata
        self._n = end
        # a cheap level meter for the UI - abs().max() on 40ms is nothing
        p = float(np.abs(indata).max())
        self.peak = max(self.peak * 0.85, p)

    def start(self) -> "Recorder":
        """Open the input stream and start the callback running."""
        import sounddevice as sd
        self._stream = sd.InputStream(
            samplerate=self.sr, channels=self.channels, dtype="float32",
            blocksize=1024, device=self.device, callback=self._callback)
        self._stream.start()
        return self

    def stop(self) -> None:
        """Tear down the input stream."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    @property
    def seconds(self) -> float:
        """How much audio has been captured so far."""
        return self._n / self.sr

    def audio(self) -> np.ndarray:
        """Everything captured so far, mono."""
        y = self._buf[: self._n]
        return y.mean(axis=1) if y.shape[1] > 1 else y[:, 0]

    def tail(self, seconds: float) -> tuple[np.ndarray, float]:
        """The last `seconds` of audio, and the time it starts at."""
        n = int(seconds * self.sr)
        end = self._n
        start = max(0, end - n)
        y = self._buf[start:end]
        y = y.mean(axis=1) if y.shape[1] > 1 else y[:, 0]
        return np.ascontiguousarray(y), start / self.sr

    def __enter__(self):
        """Start as a context manager."""
        return self.start()

    def __exit__(self, *exc):
        """Stop as a context manager."""
        self.stop()


# --- live pitch -> notes -----------------------------------------------------

@dataclass
class LiveState:
    """What the analysis thread has worked out so far."""
    notes: list[Note] = field(default_factory=list)
    chords: list = field(default_factory=list)
    pitch: float | None = None          # what is sounding right now, in MIDI
    tempo: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> tuple[list[Note], list, float | None]:
        """A consistent copy of notes/chords/pitch for the view thread to
        read, taken under the lock so it never sees a half-written update."""
        with self.lock:
            return list(self.notes), list(self.chords), self.pitch


_ANALYSIS_WINDOW = 4.0      # seconds of tail handed to the tracker each pass
_SETTLE = 0.35              # ... of which the newest is too fresh to commit


def _live_notes(y: np.ndarray, sr: int, t0: float, fmin: float, fmax: float,
                settle: float = _SETTLE) -> tuple[list[Note], float | None]:
    """Transcribe one analysis window: the settled notes, and what is sounding.

    A note is committed once it is far enough behind the write head that a
    later block cannot change it. Without that cut every pass would re-emit the
    note currently being played, with a slightly different end each time, and
    the display would flicker between guesses.

    That same cut means no *committed* note ever covers the present moment, so
    "what am I playing right now" cannot come from them - it is read straight
    off the last voiced frames of the contour, which is the freshest thing
    available and does not need to be stable to be useful.
    """
    from .notes import _segment_contour, _torchcrepe_contour_audio

    times, midi, per = _torchcrepe_contour_audio(y, sr, fmin, fmax)
    notes = _segment_contour(times, midi, per)

    tail = midi[-int(round(settle / max(times[1] - times[0], 1e-6))):] if len(times) > 1 else midi
    tail = tail[np.isfinite(tail)]
    sounding = float(np.median(tail)) if len(tail) >= 3 else None

    cutoff = len(y) / sr - settle
    out = []
    for n in notes:
        if n.start >= cutoff:
            continue
        n.start += t0
        n.end = min(n.end, cutoff) + t0
        out.append(n)
    return out, sounding


def _merge(existing: list[Note], fresh: list[Note], overlap_from: float) -> list[Note]:
    """Replace the re-analysed tail with the new pass, keep what is older.

    Each pass re-examines the last few seconds, so its verdict on that span is
    better informed than the previous pass's; anything starting before the
    window began is settled and left alone.
    """
    kept = [n for n in existing if n.start < overlap_from]
    return kept + [n for n in fresh if n.start >= overlap_from]


def _live_chords(y: np.ndarray, sr: int, t0: float, beat_times: np.ndarray):
    """Chords over one window, using the same templates as the offline path."""
    from .analysis import detect_chords

    if len(beat_times) < 2:
        return []
    chords = detect_chords(y, sr, beat_times)
    for c in chords:
        c.start += t0
        c.end += t0
    return chords


_CHORD_EVERY = 3            # chord passes are this many times rarer than pitch


def analysis_worker(rec: Recorder, state: LiveState, stop: threading.Event, *,
                    instrument: str = "guitar", do_chords: bool = True,
                    period: float = 0.4) -> None:
    """Background pass: re-transcribe the tail every `period` seconds.

    Runs off the audio thread on purpose - a pass over a 4s window costs a
    good fraction of a second, which is an eternity inside a callback that has
    23ms to return a block.

    Pitch and chords run on different clocks. Together they cost more than the
    period, so a single loop falls behind and the "what am I playing now"
    readout - the one thing that has to feel immediate - inherits the latency
    of the slowest thing in the loop. Chords change on the scale of a bar, so
    they can afford to run every few passes; pitch cannot.
    """
    import librosa

    from .config import PITCH_RANGE
    fmin, fmax = PITCH_RANGE.get(instrument, PITCH_RANGE["other"])
    turn = 0

    while not stop.is_set():
        t_wake = time.monotonic()
        # CREPE needs about a second of context to be worth trusting, but
        # waiting for the *full* window before showing anything leaves the
        # display empty for the first few seconds of a take - long enough that
        # you assume the mic is dead and stop playing. So analyse whatever
        # exists once there is enough of it.
        if rec.seconds < 1.0:
            time.sleep(period)
            continue
        y, t0 = rec.tail(_ANALYSIS_WINDOW)
        try:
            fresh, sounding = _live_notes(y, rec.sr, t0, fmin, fmax)
        except Exception:                      # noqa: BLE001 - keep recording
            fresh, sounding = [], None

        with state.lock:
            state.notes = _merge(state.notes, fresh, t0)
            state.pitch = sounding

        if do_chords and turn % _CHORD_EVERY == 0 and len(y) > 2 * rec.sr:
            try:
                est, beats = librosa.beat.beat_track(y=y, sr=rec.sr, units="time")
                chords = _live_chords(y, rec.sr, t0, beats)
                with state.lock:
                    state.tempo = float(np.atleast_1d(est)[0])
                    if chords:
                        state.chords = _merge_chords(state.chords, chords, t0)
            except Exception:                  # noqa: BLE001
                pass
        turn += 1

        time.sleep(max(0.0, period - (time.monotonic() - t_wake)))


def _merge_chords(existing: list, fresh: list, overlap_from: float) -> list:
    """Same idea as `_merge`, for the chord track: keep chords settled before
    the re-analysed window, replace everything from `overlap_from` on."""
    kept = [c for c in existing if c.start < overlap_from]
    return kept + [c for c in fresh if c.start >= overlap_from]


def collapse_chords(chords: list, min_len: float = 0.5) -> list:
    """Consecutive identical chords into one span, dropping N.C. flickers."""
    out = []
    for c in chords:
        if out and out[-1].name == c.name:
            out[-1].end = c.end
        else:
            out.append(c)
    return [c for c in out if c.end - c.start >= min_len or c.name != "N.C."]


# --- the live view -----------------------------------------------------------

def live_view(rec: Recorder, state: LiveState, stop: threading.Event, *,
              instrument: str = "guitar", view: str = "tab", console=None,
              tempo: float = 0.0, beats_per_bar: int = 4, subdiv: int = 4,
              history: float = 8.0, refresh: int = 12) -> None:
    """Draw what is being played: level, current note, recent notes/tab/chords.

    The tab is re-fretted from the last few seconds each frame rather than
    appended to. Fretting is a whole-phrase decision (see `tabs.fret_notes` -
    the hand position depends on what comes next), so a cell chosen when a note
    arrived is not necessarily the one the finished phrase wants.
    """
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    from . import report
    from .config import pitch_name
    from .tabs import TabLayout, fret_notes
    console = console or report.console

    def meter(level: float, width: int = 28) -> Text:
        """A bar-graph level meter; sqrt-scaled so quiet input still moves it."""
        n = int(np.clip(level, 0, 1) ** 0.5 * width)
        t = Text()
        t.append("█" * n, style="red" if level > 0.9 else
                 "yellow" if level > 0.6 else "green")
        t.append("·" * (width - n), style="dim")
        return t

    def frame():
        """Render one frame of the live view from the latest state snapshot."""
        notes, chords, pitch = state.snapshot()
        now = rec.seconds
        recent = [n for n in notes if n.end > now - history]

        head = Table.grid(padding=(0, 2))
        head.add_row("rec", meter(rec.peak),
                     f"[bold]{report._mmss(now)}[/]",
                     f"[yellow]{pitch_name(int(pitch))}[/]" if pitch else "[dim]—[/]",
                     f"[dim]{state.tempo:.0f} bpm[/]" if state.tempo else "")

        body = Text()
        bpm = tempo or state.tempo or 120.0
        if view == "tab" and recent:
            t0 = max(0.0, now - history)
            lay = TabLayout(fret_notes(recent, instrument), instrument,
                            tempo=bpm, t0=t0, beats_per_bar=beats_per_bar,
                            subdiv=subdiv, first_bar=1,
                            max_width=max(40, console.width - 8),
                            chords=[c for c in chords if c.end > t0])
            for line in range(lay.n_lines):
                for row in lay.line_rows(line):
                    body.append(row + "\n")
        elif recent:
            for n in recent[-14:]:
                tech = "" if n.technique == "normal" else f"  {n.technique}"
                body.append(f"  {report._mmss(n.start)}  {n.name:<4} "
                            f"{n.duration:4.2f}s{tech}\n")
        else:
            body.append("  listening…\n", style="dim")

        heard = collapse_chords([c for c in chords if c.end > now - history])
        if heard:
            body.append("\n  chords: ", style="dim")
            body.append(" ".join(c.name for c in heard[-10:]), style="cyan")

        grid = Table.grid()
        grid.add_row(head)
        grid.add_row(body)
        return Panel(grid, title=f"recording · {instrument} · ctrl-c to stop",
                     border_style="red", expand=False)

    with Live(frame(), console=console, refresh_per_second=refresh) as live:
        try:
            while not stop.is_set():
                live.update(frame())
                time.sleep(1.0 / refresh)
        except KeyboardInterrupt:
            stop.set()
