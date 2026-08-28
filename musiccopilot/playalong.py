"""Play along: the tab (or the notes) scrolling under a moving cursor.

`musiccopilot tab --follow` plays a passage and draws a vertical bar at the
moment the audio is actually at, so the part can be practised against the
recording instead of read off a static page.

Two things make this honest rather than decorative:

* The cursor position comes from the *audio callback's* frame counter, not
  from a wall clock started next to it. `afplay` in a subprocess gives no
  position at all, and a `time.monotonic()` estimate drifts against the device
  clock - by the end of a 30s solo that is enough to sit a cursor a full beat
  away from what you are hearing, which is worse than no cursor.
* Columns come from the same `TabLayout` the printed tab uses, so what the bar
  points at is exactly the cell you would have read.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from .config import SR


# --- audio transport ---------------------------------------------------------

class Transport:
    """Plays a buffer and reports where the *device* is, not where a clock is.

    `position()` is the number of frames the callback has handed to the sound
    card, so it stays locked to what is audible even when the UI thread stalls
    or the stream underruns.
    """

    def __init__(self, y: np.ndarray, sr: int = SR, t0: float = 0.0,
                 loop: bool = False):
        """`t0` is the song-time the buffer's first frame corresponds to, so
        `position()` reports seconds into the song rather than into `y`."""
        self._t_start = 0.0
        self.y = np.ascontiguousarray(
            y if y.ndim == 2 else y.reshape(-1, 1), dtype=np.float32)
        self.sr, self.t0, self.loop = sr, t0, loop
        self.frame = 0
        self.done = threading.Event()
        self._stream = None

    def _callback(self, out, frames, _time, status):   # noqa: ANN001
        """Hand the sound card its next block and advance `self.frame` - the
        counter `position()` reads. Runs on the audio thread, so this is the
        only place `frame` is allowed to move."""
        import sounddevice as sd
        n = len(self.y)
        i = self.frame
        chunk = self.y[i:i + frames]
        if len(chunk) < frames:
            if self.loop:
                wrap = self.y[: frames - len(chunk)]
                out[:] = np.vstack([chunk, wrap])
                self.frame = len(wrap)
                return
            out[: len(chunk)] = chunk
            out[len(chunk):] = 0
            self.frame = n
            self.done.set()
            raise sd.CallbackStop
        out[:] = chunk
        self.frame = i + frames

    def start(self) -> "Transport":
        """Open the output stream and start the callback running."""
        import sounddevice as sd
        self._stream = sd.OutputStream(
            samplerate=self.sr, channels=self.y.shape[1], dtype="float32",
            blocksize=1024, callback=self._callback,
            finished_callback=self.done.set)
        self._stream.start()
        self._t_start = time.monotonic()
        return self

    @property
    def duration(self) -> float:
        """Length of the buffer in seconds."""
        return len(self.y) / self.sr

    def stalled(self) -> bool:
        """True if the device is not actually consuming audio.

        Some environments hand out an output stream that never calls back (no
        audio device, a sandbox, a disconnected default output). Without this
        check the follow loop waits on a `done` that can never be set and hangs
        with a frozen cursor, which looks like the program crashed. Compare the
        wall clock against the frames the device has taken: a real stream stays
        close to real time, a dead one never moves at all.
        """
        if self._stream is None:
            return False
        elapsed = time.monotonic() - self._t_start
        return elapsed > max(1.0, self.duration + 1.0) and self.frame == 0

    def position(self) -> float:
        """Seconds into the song (not into the buffer)."""
        return self.t0 + self.frame / self.sr

    def stop(self) -> None:
        """Tear down the stream and mark `done`, so a follow loop waiting on
        it exits instead of hanging."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.done.set()

    def __enter__(self):
        """Start as a context manager."""
        return self.start()

    def __exit__(self, *exc):
        """Stop as a context manager."""
        self.stop()


# --- the live view -----------------------------------------------------------

def _cursor_row(layout, col: int, width: int) -> str:
    """A caret under the column the audio is currently on."""
    x = layout.x_of(col)
    return " " * min(x, max(0, width - 1)) + "^"


def follow_tab(layout, transport: Transport, *, console=None,
               title: str = "", count_in: float = 0.0,
               refresh: int = 24) -> None:
    """Draw `layout` with a cursor tracking `transport`, until the audio ends.

    Only the system (line-block) the cursor is on is drawn, plus the next one,
    so the view stays put instead of scrolling a whole solo past the eye. The
    line flips when the cursor crosses into the next system.
    """
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    from . import report
    console = console or report.console

    def frame() -> Panel:
        """Render the current system with the cursor at `transport`'s position."""
        t = transport.position()
        col = layout.col_of(t)
        col = int(np.clip(col, 0, layout.n_cols - 1))
        line = layout.line_of(col)

        body = Text()
        if count_in and t < layout.t0:
            beats = int(np.ceil((layout.t0 - t) * layout.tempo / 60.0))
            body.append(f"  count in… {beats}\n\n", style="bold yellow")

        rows = layout.line_rows(line)
        width = max(len(r) for r in rows)
        for i, row in enumerate(rows):
            if i == 0 or i == len(rows) - 1:          # chord / bar-number rows
                body.append(row + "\n", style="dim")
            else:
                # highlight the character cell the cursor sits on so the note
                # you are meant to be playing reads at a glance
                x = layout.x_of(col)
                w = layout.widths[col] if col < len(layout.widths) else 1
                body.append(row[:x])
                body.append(row[x:x + w], style="bold black on yellow")
                body.append(row[x + w:] + "\n")
        body.append(_cursor_row(layout, col, width) + "\n", style="bold yellow")

        bar = layout.first_bar + col // layout.per_bar
        beat = (col % layout.per_bar) // layout.subdiv + 1
        head = f"bar {bar} · beat {beat} · {report._mmss(t)}"
        nxt = f"   (line {line + 1}/{layout.n_lines})"
        body.append("\n" + head + nxt, style="dim")
        return Panel(body, title=title or "play along", expand=False,
                     border_style="yellow")

    with Live(frame(), console=console, refresh_per_second=refresh,
              transient=False) as live:
        try:
            while not transport.done.is_set():
                if transport.stalled():
                    console.print("[yellow]no audio output on this device — "
                                  "showing the tab without playback[/]")
                    break
                live.update(frame())
                time.sleep(1.0 / refresh)
            live.update(frame())
        except KeyboardInterrupt:
            transport.stop()


def follow_notes(notes, transport: Transport, *, console=None, title: str = "",
                 window: float = 6.0, refresh: int = 24) -> None:
    """The same idea without a fretboard: a time ruler of note names scrolling
    past a fixed cursor. This is the view for a stem that is not a guitar - a
    vocal line, a piano part - where fret numbers would be a lie.
    """
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    from . import report
    console = console or report.console
    notes = sorted(notes, key=lambda n: n.start)

    def frame() -> Panel:
        """One redraw: the note-name ruler around the transport's current time."""
        t = transport.position()
        lo, hi = t - window / 3, t + window * 2 / 3
        body = Text()
        live_now = [n for n in notes if n.start <= t < n.end]
        body.append("  now: ", style="dim")
        body.append(" ".join(n.name for n in live_now) or "—",
                    style="bold yellow" if live_now else "dim")
        body.append("\n\n")
        for n in notes:
            if n.end < lo or n.start > hi:
                continue
            pos = int((n.start - lo) / (hi - lo) * 60)
            row = " " * max(0, pos) + n.name
            if n.technique != "normal":
                row += f" ({n.technique})"
            style = ("bold yellow" if n in live_now else
                     "dim" if n.end < t else "")
            body.append(row + "\n", style=style)
        body.append("\n" + " " * 20 + "^ now  " + report._mmss(t), style="bold yellow")
        return Panel(body, title=title or "play along", expand=False,
                     border_style="yellow")

    with Live(frame(), console=console, refresh_per_second=refresh) as live:
        try:
            while not transport.done.is_set():
                if transport.stalled():
                    console.print("[yellow]no audio output on this device[/]")
                    break
                live.update(frame())
                time.sleep(1.0 / refresh)
        except KeyboardInterrupt:
            transport.stop()
