"""Live mic modes, streamed to the browser over a WebSocket.

Both panes in the UI - "what is he playing" (live tab) and "what key are we
in" (live key) - are the same machinery as `musiccopilot record`: a
`Recorder` appending to a preallocated buffer on the audio thread, and
`analysis_worker` re-transcribing the tail off it. Nothing about that changes
here; this module only turns `LiveState` into JSON frames on a timer and adds
the rolling key estimate the key pane needs.

The mic is opened by the server process, so the machine running Scriptum is
the one in the practice room. That keeps the whole torchcrepe path untouched
and the latency down to what the CLI already achieves.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from musiccopilot import notes as nt
from musiccopilot.config import SR

_KEY_WINDOW = 20.0          # seconds of tail the key estimate looks at
_KEY_EVERY = 3.0            # ... and how often it is recomputed (HPSS is not cheap)
_TAB_HISTORY = 12.0         # seconds of notes kept on screen in the live tab


def devices() -> list[dict]:
    """Input devices the server can record from, for the UI's picker."""
    try:
        import sounddevice as sd
    except Exception as exc:                        # noqa: BLE001
        raise RuntimeError(f"sounddevice unavailable: {exc}") from exc
    out = []
    try:
        default_in = sd.default.device[0]
    except Exception:                               # noqa: BLE001
        default_in = None
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append({"index": i, "name": d["name"],
                        "channels": d["max_input_channels"],
                        "default": i == default_in,
                        "samplerate": int(d.get("default_samplerate") or SR)})
    return out


class KeyTracker:
    """A rolling key estimate over the last `_KEY_WINDOW` seconds.

    Separate from `analysis_worker`'s chord pass for the same reason pitch and
    chords are separate there: `detect_key` runs HPSS, which costs far more
    than one analysis period, and a key does not change fast enough to be
    worth that on every pass.
    """

    def __init__(self) -> None:
        self.key = ""
        self.scale: list[str] = []
        self._last = 0.0
        self._lock = threading.Lock()

    def maybe_update(self, rec, chords) -> None:
        """Recompute the key if enough time has passed and there is audio."""
        now = time.monotonic()
        if now - self._last < _KEY_EVERY or rec.seconds < 4.0:
            return
        self._last = now
        y, _ = rec.tail(_KEY_WINDOW)
        if len(y) < SR:
            return
        try:
            from musiccopilot.analysis import detect_key
            from musiccopilot.tabs import scale_notes
            key = detect_key(y, rec.sr, chords or None)
            with self._lock:
                self.key, self.scale = key, scale_notes(key)
        except Exception:                           # noqa: BLE001 - keep listening
            pass

    def snapshot(self) -> tuple[str, list[str]]:
        """The current estimate."""
        with self._lock:
            return self.key, list(self.scale)


class LiveSession:
    """One open mic: the recorder, the analysis thread, and the JSON frames.

    Used as a context manager so the input stream is always closed - a
    WebSocket that drops must not leave the device open, or the next session
    cannot start.
    """

    def __init__(self, *, mode: str = "tab", instrument: str = "guitar",
                 device=None, tempo: float = 0.0, subdiv: int = 4,
                 max_seconds: float = 3600.0) -> None:
        from musiccopilot import record

        self.mode = mode                    # "tab" | "key"
        self.instrument = instrument
        self.tempo = tempo
        self.subdiv = subdiv
        self.rec = record.Recorder(SR, device=device, max_seconds=max_seconds)
        self.state = record.LiveState()
        self.stop = threading.Event()
        self.keys = KeyTracker()
        self._worker: threading.Thread | None = None
        self._record = record

    def __enter__(self) -> "LiveSession":
        """Open the mic and start the analysis thread."""
        self.rec.start()
        # The key pane needs the chord track; the tab pane can skip it, which
        # roughly halves the work per pass and keeps the note display prompt.
        self._worker = threading.Thread(
            target=self._record.analysis_worker, daemon=True,
            args=(self.rec, self.state, self.stop),
            kwargs={"instrument": self.instrument,
                    "do_chords": self.mode == "key" or self.instrument != "vocals"})
        self._worker.start()
        return self

    def __exit__(self, *exc) -> None:
        """Stop analysis and close the device."""
        self.stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
        self.rec.stop()

    # --- frames -------------------------------------------------------------
    def frame(self) -> dict:
        """One JSON frame: what has been played, and what is sounding now.

        `pitch` is deliberately not taken from `notes` - no committed note
        covers the present moment (see `record._live_notes`), so the
        now-playing readout comes off the contour's last voiced frames.
        """
        notes, chords, pitch = self.state.snapshot()
        now = self.rec.seconds
        tempo = self.tempo or self.state.tempo or 120.0
        chords = self._record.collapse_chords(list(chords))

        out = {
            "type": "frame",
            "t": round(now, 3),
            "level": round(float(self.rec.peak), 4),
            "tempo": round(float(tempo), 1),
            "overflows": self.rec.overflows,
            "pitch": round(float(pitch), 2) if pitch else None,
            "pitch_name": nt.pitch_name(int(round(pitch))) if pitch else None,
            "chords": [{"start": round(c.start, 2), "end": round(c.end, 2),
                        "name": c.name, "root": c.root, "quality": c.quality}
                       for c in chords[-16:]],
        }

        if self.mode == "key":
            self.keys.maybe_update(self.rec, chords)
            key, scale = self.keys.snapshot()
            out["key"] = key
            out["scale"] = scale
            out["chord"] = chords[-1].name if chords else None
            return out

        recent = [n for n in notes if n.end > now - _TAB_HISTORY]
        out["notes"] = [{"start": round(n.start, 3), "end": round(n.end, 3),
                         "pitch": n.pitch, "name": n.name,
                         "technique": n.technique, "bend": round(float(n.bend), 2)}
                        for n in recent[-64:]]
        out["layout"] = self._layout(recent, tempo, now)
        return out

    def _layout(self, recent: list, tempo: float, now: float) -> dict | None:
        """Lay the recent notes out as a tab (or a staff, for a fretless
        instrument) using the same layout objects the offline path uses."""
        from musiccopilot.config import TUNINGS
        from musiccopilot.tabs import StaffLayout, TabLayout, fret_notes

        from .serialize import layout_json

        if not recent:
            return None
        t0 = max(0.0, now - _TAB_HISTORY)
        common = dict(tempo=tempo, t0=t0, subdiv=self.subdiv, first_bar=1,
                      chords=[], max_width=120)
        try:
            if self.instrument in TUNINGS:
                layout = TabLayout(fret_notes(recent, self.instrument),
                                   self.instrument, **common)
            else:
                layout = StaffLayout(recent, **common)
        except Exception:                           # noqa: BLE001
            return None
        return layout_json(layout, stem=self.instrument, start=t0, end=now)

    # --- saving -------------------------------------------------------------
    def save(self, name: str = "") -> dict:
        """Write the take the way `cli.cmd_record` does, and describe it.

        The whole take is re-transcribed rather than saving what was shown:
        the offline segmenter can look forward, which is what tells a bend
        from two notes, so the file beats the live display.
        """
        import json
        from pathlib import Path

        from musiccopilot import audio
        from musiccopilot.config import PITCH_RANGE

        y = self.rec.audio()
        if not len(y):
            return {"saved": False, "reason": "nothing captured"}
        name = name or time.strftime("take-%Y%m%d-%H%M%S")
        out = Path.cwd() / "recordings" / name
        out.mkdir(parents=True, exist_ok=True)
        wav = audio.save(out / "take.wav", y, SR)

        fmin, fmax = PITCH_RANGE.get(self.instrument, PITCH_RANGE["other"])
        ns = nt._crepe_notes_from_audio(y, SR, fmin, fmax)
        (out / "notes").mkdir(exist_ok=True)
        (out / "notes" / f"{self.instrument}.json").write_text(
            json.dumps(nt.to_dicts(ns)))
        tempo = self.tempo or self.state.tempo or 120.0
        nt.write_midi(ns, out / "take.mid", tempo)
        return {"saved": True, "name": name, "dir": str(out), "wav": str(wav),
                "notes": len(ns), "seconds": round(self.rec.seconds, 2)}
