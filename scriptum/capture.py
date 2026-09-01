"""Recording an input device into the library, driven from the browser.

The mic is the server's (CLAUDE.md, "The mic is the server's"), and so is the
loopback device: what Scriptum can record is whatever the machine it runs on
can hear. So this is `musiccopilot capture` with a browser in front of it, and
the capture itself happens in the server process exactly as the live panes do.

Two deliberate shapes:

**One session at a time.** There is one sound card, and two recordings of the
same device are the same recording twice. The session is module state rather
than a per-request object, which is also what lets a browser that was closed
mid-take come back and find the recording still running - the same reason
`JOBS.active()` exists for analyses.

**The meter is polled, not streamed.** A level is a *gauge*, not a transcript:
a client that reconnects wants the current value, not every value since it
started. That is the opposite of the job log over SSE, so it would be the
wrong machinery, and a few hundred bytes at 4Hz is not worth a WebSocket.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from musiccopilot import record

# Silence is the failure this setup actually has, and it has one cause: the
# audio went into the loopback device and nowhere else, or never into it. The
# client shows the fix once a take has been quiet for this long.
SILENT_DB = -80.0


def _db(v: float) -> float:
    """Amplitude to dBFS, floored so a digital-silent buffer is a number."""
    return float(20.0 * np.log10(max(float(v), 1e-7)))


class Capture:
    """One recording in progress, and the file it becomes."""

    def __init__(self, name: str = "", device=None, mono: bool = False,
                 max_minutes: float = 12.0):
        """Open the device at *its* sample rate rather than resampling on the
        way in - `audio.load` resamples to `SR` when the pipeline reads the
        wav, so one conversion happens instead of two."""
        import sounddevice as sd

        info = sd.query_devices(device, "input")
        self.device = device
        self.device_name = str(info["name"])
        self.sr = int(info["default_samplerate"])
        self.channels = 1 if mono else min(2, int(info["max_input_channels"]))
        self.name = name
        self.started = time.time()
        self.quiet_since: float | None = self.started
        self.rec = record.Recorder(self.sr, channels=self.channels, device=device,
                                   max_seconds=max_minutes * 60)
        self.max_seconds = max_minutes * 60
        self.rec.start()

    def status(self) -> dict:
        """What the meter needs, and enough for the client to explain silence."""
        peak = _db(self.rec.peak)
        now = time.time()
        if peak > SILENT_DB:
            self.quiet_since = None
        elif self.quiet_since is None:
            self.quiet_since = now
        return {
            "active": True,
            "name": self.name,
            "device": self.device_name,
            "samplerate": self.sr,
            "channels": self.channels,
            "seconds": round(self.rec.seconds, 2),
            "peak_db": round(peak, 1),
            "dropouts": self.rec.overflows,
            # How long it has heard nothing. The client turns this into the
            # BlackHole hint rather than the server guessing when to nag.
            "quiet_seconds": round(now - self.quiet_since, 1) if self.quiet_since else 0.0,
            "full": self.rec.seconds >= self.max_seconds - 0.5,
            "max_seconds": self.max_seconds,
        }

    def discard(self) -> None:
        """Close the device and throw the audio away."""
        self.rec.stop()

    def write(self, root: Path) -> dict:
        """Close the device and file the take as a song in `root`.

        Returns the same numbers `status` reports plus where it landed, so the
        client can say "4:09 captured, peak -6 dB" without a second request.
        """
        from musiccopilot import audio

        from . import library

        self.rec.stop()
        y = self.rec.frames()
        seconds, peak = self.rec.seconds, float(np.abs(y).max()) if y.size else 0.0
        if not y.size:
            return {"written": False, "seconds": 0.0, "peak_db": _db(0.0)}

        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        stem = library.slugify(self.name or time.strftime("capture-%Y%m%d-%H%M%S"))
        path, song_id = library.unique_path(root, stem, ".wav")
        audio.save(path, y, self.sr)
        return {"written": True, "song": song_id, "filename": path.name,
                "seconds": round(seconds, 2), "peak_db": round(_db(peak), 1),
                "dropouts": self.rec.overflows, "silent": peak < 1e-3}


class Captures:
    """The one live capture, guarded so two clients cannot start two."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Capture | None = None

    def start(self, **kw) -> dict:
        """Open a capture, refusing if one is already running."""
        with self._lock:
            if self._current is not None:
                raise RuntimeError("a capture is already running")
            self._current = Capture(**kw)
            return self._current.status()

    def status(self) -> dict:
        """The running capture's meter, or that there is none."""
        cur = self._current
        return cur.status() if cur is not None else {"active": False}

    def stop(self, root: Path, discard: bool = False) -> dict:
        """Finish the capture: write it as a song, or throw it away."""
        with self._lock:
            cur, self._current = self._current, None
        if cur is None:
            raise RuntimeError("no capture is running")
        if discard:
            cur.discard()
            return {"written": False, "discarded": True}
        return cur.write(root)


CAPTURES = Captures()
