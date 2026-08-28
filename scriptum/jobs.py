"""Background jobs: the slow pipeline, run off the request thread.

`analyze` on a fresh song is minutes of demucs plus transcription, so it
cannot happen inside an HTTP request. A job runs on a worker thread and
appends every line the pipeline logs to a transcript the client tails over
SSE; the pipeline's own `log=` hook is the progress feed, so the browser sees
exactly what the CLI prints and no progress reporting had to be invented.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field


@dataclass
class Job:
    """One background run. `lines` is append-only so a late subscriber can be
    handed the whole transcript and then follow along from the end."""
    id: str
    kind: str
    song: str
    state: str = "running"            # running | done | error
    lines: list[str] = field(default_factory=list)
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    result: dict | None = None
    _event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, text: str) -> None:
        """Record a progress line and wake everyone tailing the job."""
        with self._lock:
            self.lines.append(str(text))
        self._wake()

    def _wake(self) -> None:
        """Release every waiter, then re-arm for the next update."""
        self._event.set()
        self._event = threading.Event()

    def finish(self, state: str, error: str = "", result: dict | None = None) -> None:
        """Mark the job finished (or failed) and wake its followers."""
        self.state, self.error, self.result = state, error, result
        self.finished = time.time()
        self._wake()

    def snapshot(self) -> dict:
        """A consistent view for a status poll."""
        with self._lock:
            lines = list(self.lines)
        return {"id": self.id, "kind": self.kind, "song": self.song,
                "state": self.state, "lines": lines, "error": self.error,
                "started": self.started, "finished": self.finished,
                "result": self.result}

    def wait(self, timeout: float) -> None:
        """Block until the next log line or state change (or `timeout`)."""
        self._event.wait(timeout)


class Jobs:
    """The job registry, and the one-at-a-time rule that protects the cache.

    Two analyses of the same song at once would race on the same cache files -
    both writing `notes/<stem>.json`, one clobbering the other's form - so a
    song already running is returned its existing job instead of starting a
    second one.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._by_song: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        """A job by id."""
        return self._jobs.get(job_id)

    def for_song(self, song: str) -> Job | None:
        """The running job for a song, if there is one."""
        with self._lock:
            job = self._jobs.get(self._by_song.get(song, ""))
        return job if job and job.state == "running" else None

    def active(self) -> list[dict]:
        """Every job still running, for the client to re-attach to on reload."""
        return [j.snapshot() for j in self._jobs.values() if j.state == "running"]

    def start(self, kind: str, song: str, target, timeout: float = 0.0) -> Job:
        """Run `target(job)` on a worker thread, unless this song is busy.

        `target` gets the job so it can log through it; whatever it returns
        becomes `job.result`.

        `timeout`, when set, is a deadline for reporting - not a kill. Python
        cannot interrupt a thread blocked in a socket read, and the Gemini
        calls behind the solo and cleanup jobs have no timeout of their own
        (a 75-note cleanup measured 48s once and over 110s the next time on
        the identical input). Without a deadline a hung call leaves the
        client watching a spinner with no way to tell waiting from broken, so
        the job is marked failed and the orphaned thread - a daemon - is left
        to die with the process. A late result is then discarded rather than
        overwriting the reported failure.
        """
        if (running := self.for_song(song)):
            return running
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, song=song)
        with self._lock:
            self._jobs[job.id] = job
            self._by_song[song] = job.id

        def run() -> None:
            """Execute the job body, reporting either result or traceback."""
            try:
                result = target(job) or {}
            except Exception as exc:                       # noqa: BLE001
                if job.state == "running":
                    job.log(f"error: {exc}")
                    job.finish("error", error=f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
                return
            if job.state == "running":                 # not already timed out
                job.finish("done", result=result)

        def watchdog() -> None:
            """Fail the job if it outlives its deadline."""
            time.sleep(timeout)
            if job.state == "running":
                job.log(f"gave up after {timeout:.0f}s")
                job.finish("error",
                           error=f"timed out after {timeout:.0f}s - the model "
                                 f"did not answer; try again or use a shorter passage")

        threading.Thread(target=run, daemon=True, name=f"job-{job.id}").start()
        if timeout:
            threading.Thread(target=watchdog, daemon=True,
                             name=f"watch-{job.id}").start()
        return job

    def prune(self, keep: float = 3600.0) -> None:
        """Forget jobs that finished more than `keep` seconds ago."""
        now = time.time()
        with self._lock:
            for jid, j in list(self._jobs.items()):
                if j.state != "running" and now - j.finished > keep:
                    self._jobs.pop(jid, None)
                    if self._by_song.get(j.song) == jid:
                        self._by_song.pop(j.song, None)


JOBS = Jobs()
