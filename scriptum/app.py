"""Scriptum's HTTP + WebSocket API.

Thin on purpose. Every musical decision - how a window is resolved, which
columns a tab has, which stem leads a solo - is made by `musiccopilot`, and
several handlers call straight into `cli`'s helpers (`position`, `_window`,
`_grid_cols`) so that "bars 17-24" means the same thing in the browser as it
does in the terminal. What is left here is uploads, jobs, JSON and files.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import soundfile as sf

from fastapi import (Body, FastAPI, File, Form, HTTPException, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from musiccopilot import audio as au, cli, notes as nt, report
from musiccopilot.config import (LLM_CLEAN_MAX_NOTES, LLM_CLEAN_MAX_SECONDS, SR,
                                 STEM_NAMES, base_stem, fretboard_for, workdir_for)
from musiccopilot.pipeline import TRANSCRIBE_STEMS, Song

from . import capture, library, live, serialize
from .jobs import JOBS

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

# How long a Gemini-backed job may run before it is reported as failed.
# The calls themselves have no timeout, and their duration varies widely
# on identical input, so this is a reporting deadline rather than a
# prediction - see `Jobs.start`.
GEMINI_DEADLINE = float(os.getenv("SCRIPTUM_LLM_TIMEOUT", "300"))


# --- helpers -----------------------------------------------------------------

def _song(song_id: str, *, need: str = "analysis") -> Song:
    """Load a song's cache, refusing rather than silently starting the pipeline.

    `cli._load` runs the whole slow pipeline when the cache is cold, which is
    right for a terminal but wrong for a request: the browser has an explicit
    Analyse button and a progress stream, so an un-analysed song is a 409 with
    a machine-readable reason, not a five-minute hang.
    """
    path = library.find(song_id)
    if path is None:
        raise HTTPException(404, f"no song {song_id!r}")
    song = Song.open(path)
    if need in ("analysis", "form") and song.analysis is None:
        raise HTTPException(409, {"error": "not_analyzed", "song": song_id})
    if need == "form" and song.form is None:
        raise HTTPException(409, {"error": "no_form", "song": song_id})
    return song


def _safe(work: Path, *parts: str) -> Path:
    """A path inside a song's cache folder, or 404 - never outside it."""
    p = (work.joinpath(*parts)).resolve()
    if not str(p).startswith(str(work.resolve())) or not p.is_file():
        raise HTTPException(404, "no such file")
    return p


def _wav_response(y, sr: int) -> FileResponse:
    """Serve a computed (mono or stereo) signal as a wav, without keeping it.

    A temp file rather than an in-memory `StreamingResponse`: Starlette's
    `FileResponse` answers HTTP range requests, which `<audio>` elements use
    to seek and to probe duration before the whole file has arrived, and a
    streamed `BytesIO` cannot. The file is unlinked once the response has been
    sent - this is for a mix nobody wants kept, not a cache.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    sf.write(tmp.name, y.T if y.ndim > 1 else y, sr)
    return FileResponse(tmp.name, media_type="audio/wav",
                        background=BackgroundTask(lambda: os.unlink(tmp.name)))


def _window(song, part=None, bars=None, start=None, end=None):
    """Resolve a passage exactly as the CLI does, via `cli._window`."""
    args = SimpleNamespace(part=part, bars=bars, start=start, end=end)
    try:
        return cli._window(args, song)
    except SystemExit as exc:                       # cli raises this for a bad part
        raise HTTPException(400, str(exc)) from exc
    except (ValueError, IndexError) as exc:
        raise HTTPException(400, f"bad window: {exc}") from exc


def _layout_for(song, stem: str, start: float, end: float, *, subdiv: int,
                title: str, window: list):
    """Build the right layout for a stem: a fretboard only where there is one.

    Mirrors `cli.cmd_tab` - `piano`, `vocals` and demucs' `other` have no
    strings, and printing frets for them is a lie (CLAUDE.md: "Stems without a
    fretboard get a staff").
    """
    from musiccopilot.tabs import (StaffLayout, TabLayout, fret_notes,
                                   pick_instrument)

    a = song.analysis
    fretted_stem = fretboard_for(stem)
    instrument = fretted_stem or pick_instrument(window)
    common = dict(
        tempo=a.tempo, t0=start, beats_per_bar=a.beats_per_bar, subdiv=subdiv,
        first_bar=report.bar_number(song, start),
        chords=[c for c in a.chords if c.end > start and c.start < end],
        min_cols=cli._grid_cols(song, start, end, a, subdiv),
        max_width=200)          # the browser scrolls; do not wrap for a terminal
    layout = (TabLayout(fret_notes(window, instrument), instrument, **common)
              if fretted_stem else StaffLayout(window, **common))
    return serialize.layout_json(layout, title=title, stem=stem,
                                 start=start, end=end)


class _SPAFiles(StaticFiles):
    """Static files that fall back to `index.html` for unknown paths.

    `StaticFiles(html=True)` only serves `index.html` for a *directory*; any
    other miss is a 404. The client is a single-page app whose routes
    (`/song/crystallize/tabs`, `/live/key`) exist only in the browser, so
    reloading one of them - or opening a bookmark - has to reach the app
    rather than the 404 handler. Anything under /api or /ws is a real miss
    and is left alone, so a mistyped endpoint still fails as an endpoint
    instead of quietly returning HTML.

    A built asset is never a client route either, and that exclusion is not
    cosmetic. Vite fingerprints every lazy view into its own chunk, so a tab
    left open across a rebuild asks for a chunk name that no longer exists -
    and answering it with the shell hands the browser HTML where it asked for
    a JavaScript module. The import rejects, vue-router abandons the
    navigation, and the link is dead for the life of that tab with nothing in
    the network log but a 200. A real 404 is what lets the client notice it is
    out of date and reload (`main.js`, `router.onError`).
    """

    #: Suffixes that only ever name a built file, so a miss there is a miss.
    ASSETS = (".js", ".mjs", ".css", ".map", ".woff", ".woff2", ".ttf", ".ico")

    async def get_response(self, path: str, scope):
        """Serve the file, or the SPA shell when the path is a client route."""
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if (exc.status_code != 404
                    or path.startswith(("api", "ws", "assets/"))
                    or path.endswith(self.ASSETS)):
                raise
            return await super().get_response("index.html", scope)


def create_app() -> FastAPI:
    """Build the ASGI app: API routes, live sockets, and the built client."""
    app = FastAPI(title="Scriptum", version="1.0",
                  description="Web front end for MusicCopilot")

    # ---------------------------------------------------------------- library
    @app.get("/api/health")
    def health() -> dict:
        """Liveness plus what optional features this install can offer."""
        try:
            live.devices()
            mic = True
        except Exception:                           # noqa: BLE001
            mic = False
        from musiccopilot.config import gemini_api_key
        try:
            has_key = bool(gemini_api_key())
        except Exception:                           # noqa: BLE001
            has_key = False
        return {"ok": True, "mic": mic, "gemini": has_key,
                "library": str(library.library_root()),
                "stems": TRANSCRIBE_STEMS,
                # The cleanup cap, so the settings pane can state it without
                # keeping its own copy of a number that lives in Python.
                "clean_limit": {"max_notes": LLM_CLEAN_MAX_NOTES,
                                "max_seconds": LLM_CLEAN_MAX_SECONDS}}

    @app.get("/api/transcribers")
    def get_transcribers() -> dict:
        """The note transcribers this install can run, for the settings pane.

        Availability is reported rather than assumed: every backend but pYIN
        is an optional dependency, and an engine that cannot import should be
        explained in the settings list instead of failing when a song is
        analysed with it.
        """
        return {"backends": nt.backend_status(), "default": nt.DEFAULT_BACKEND}

    @app.get("/api/library")
    def get_library() -> list[dict]:
        """Every song in the library, newest first."""
        return library.listing()

    @app.post("/api/library")
    async def upload(file: UploadFile) -> dict:
        """Store an uploaded audio file under a slug derived from its name."""
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in library.AUDIO_SUFFIXES:
            raise HTTPException(400, f"unsupported audio type {suffix!r}")
        root = library.library_root()
        dest, _ = library.unique_path(root, library.slugify(file.filename or "song"),
                                      suffix)
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        return library.entry(dest)

    # -------------------------------------------------------------------- daw
    #
    # Scriptum runs on the machine GarageBand is open on, so importing a
    # project is a question the server answers about its own filesystem - the
    # browser never uploads a `.band`, which it could not do anyway: a project
    # is a *package*, and a file input hands back either nothing or an
    # unordered pile of its insides.
    @app.get("/api/daw/garageband")
    def garageband() -> dict:
        """What GarageBand has open, what else is lying around, and whether
        this process is actually allowed to read any of it."""
        from musiccopilot import daw

        def row(p) -> dict:
            ok, hint = daw.readable(p)
            return {"path": str(p), "name": p.stem, "readable": ok, "hint": hint}

        open_now = [row(p) for p in daw.open_projects()]
        recent = [row(p) for p in daw.recent_projects()
                  if not any(r["path"] == str(p) for r in open_now)]
        rows = open_now + recent
        return {
            "open": open_now,
            "recent": recent,
            # One blocked project and every project is blocked - it is a folder
            # permission, not a per-file one - so the client can show the fix
            # once at the top instead of on every row.
            "blocked": bool(rows) and all(not r["readable"] for r in rows),
            "hint": next((r["hint"] for r in rows if r["hint"]), ""),
            # Whose permission it is. macOS files the toggle under the app that
            # launched Scriptum, so a panel that says "grant Scriptum access"
            # is sending someone to look for a row that cannot exist.
            "app": daw.responsible_app(),
        }

    @app.post("/api/daw/reveal")
    def daw_reveal(body: dict = Body(...)) -> dict:
        """Show a project in Finder, blocked or not.

        The way out of a TCC block is to drag the project somewhere unprotected,
        and that drag is the one thing Scriptum cannot do on anyone's behalf -
        it may not copy what it may not read. Opening a Finder window on it is
        the most the app can contribute, and `open` needs no permission of its
        own because Finder does the opening.
        """
        from musiccopilot import daw
        src = Path((body.get("path") or "").strip()).expanduser()
        if not src.exists() or not (src.suffix.lower() == ".band" or src.is_dir()):
            raise HTTPException(400, "not a project or folder on this Mac")
        daw.reveal(src)
        return {"revealed": str(src)}

    @app.post("/api/daw/upload")
    async def daw_upload(files: list[UploadFile] = File(...),
                         name: str = Form("")) -> dict:
        """Take exported tracks from the browser and stage them as a folder.

        The other doors are about the *server's* filesystem, which is right when
        Scriptum runs on the machine the session is on. BandLab is the case
        where it is not: the tracks come out of a browser one download at a
        time, on whatever machine that browser was on. So they are uploaded, and
        land in a staging folder that `read_session` then reads exactly as if
        someone had pointed at it - the import path does not learn a new shape.

        A single zip is passed through as a zip rather than unpacked here:
        `read_session` already knows how to, and doing it twice would mean two
        guesses at the same wrapper folders.
        """
        from musiccopilot import daw
        allowed = daw.AUDIO_SUFFIXES | {".zip"}
        keep = []
        for f in files:
            base = Path(f.filename or "").name
            # A folder upload sends everything else in the folder too; a stems
            # folder with a PDF and a .DS_Store in it is not an error.
            if base and not base.startswith(".") and Path(base).suffix.lower() in allowed:
                keep.append((base, f))
        if not keep:
            raise HTTPException(400, "no audio files in that - BandLab's "
                                     "Download > Tracks gives WAV or M4A")

        root = library.library_root() / ".imports"
        root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 24 * 3600
        for old in root.iterdir():      # staging is scaffolding, not storage
            if old.is_dir() and old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)

        # mkdtemp rather than a name of our own: two uploads a second apart
        # would otherwise collide, and merging them would import the tracks of
        # whichever attempt was abandoned along with the ones that were not.
        dest = Path(tempfile.mkdtemp(prefix=f"{daw.slug(name or keep[0][0])}-",
                                     dir=root))
        for base, f in keep:
            with (dest / base).open("wb") as fh:
                shutil.copyfileobj(f.file, fh)
        one_zip = len(keep) == 1 and keep[0][0].lower().endswith(".zip")
        return {"path": str(dest / keep[0][0] if one_zip else dest),
                "files": len(keep)}

    @app.post("/api/daw/browse")
    def daw_browse(body: dict = Body(default={})) -> dict:
        """Open a native macOS file picker - on the server's own screen.

        A `.band` is a *package*: a browser file input either refuses it or
        hands back a pile of its insides with no folder structure, so "open
        from file" cannot be an upload. It can be a real Finder dialog,
        because the machine running Scriptum is the machine with the project
        on it. Useless when Scriptum is being driven from a phone, which is
        why the picker is an *alternative* to the path field rather than the
        only way in - the client keeps both.
        """
        import subprocess
        kind = body.get("kind") or "band"
        prompt = ("Choose a GarageBand project" if kind == "band"
                  else "Choose the folder of exported stems")
        script = (f'choose file with prompt "{prompt}" of type {{"band"}}'
                  if kind == "band" else f'choose folder with prompt "{prompt}"')
        try:
            p = subprocess.run(["osascript", "-e", f'POSIX path of ({script})'],
                               capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise HTTPException(408, "the file picker was left open too long")
        except OSError as exc:
            raise HTTPException(500, f"cannot open a file picker here: {exc}")
        if p.returncode != 0:
            err = p.stderr.strip()
            if "-128" in err:                       # the user pressed Cancel
                return {"path": ""}
            raise HTTPException(500, err or "the file picker failed")
        return {"path": p.stdout.strip().rstrip("/")}

    @app.post("/api/daw/preview")
    def daw_preview(body: dict = Body(...)) -> dict:
        """The track -> stem mapping for a session, computed but not written.

        The browser's half of `--dry-run`: the guesses are good, not perfect,
        and the moment to fix "which of these two is the rhythm guitar" is
        before a five-minute analysis rather than after it.
        """
        from musiccopilot import daw
        src = (body.get("path") or "").strip()
        if not src:
            raise HTTPException(400, "no path")
        ok, hint = daw.readable(src)
        if not ok:
            raise HTTPException(403, hint)
        try:
            session = daw.assign(daw.read_session(src), body.get("map") or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return serialize.session_json(session)

    @app.post("/api/daw/import")
    def daw_import(body: dict = Body(...)) -> dict:
        """Import a session, then analyse it - as one job, since importing
        without analysing leaves a song the rest of the app cannot open."""
        from musiccopilot import daw
        src = (body.get("path") or "").strip()
        if not src:
            raise HTTPException(400, "no path")
        ok, hint = daw.readable(src)
        if not ok:
            raise HTTPException(403, hint)
        try:
            session = daw.assign(daw.read_session(src), body.get("map") or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        name = body.get("name") or session.source.stem
        song_id = daw.slug(name)
        backend = body.get("backend")

        def run(job) -> dict:
            for w in session.warnings:
                job.log(f"! {w}")
            path = daw.import_session(session, name=name,
                                      out=library.library_root(), log=job.log)
            job.log(f"imported {len(session.tracks)} tracks - no separation needed")
            from musiccopilot import chart as chart_mod
            chart_mod.write(Song.open(path).run(backend=backend, log=job.log))
            return {"song": path.stem}

        return JOBS.start("import", song_id, run).snapshot()

    @app.delete("/api/library/{song_id}")
    def delete_song(song_id: str, cache: bool = True) -> dict:
        """Remove a song and (by default) its analysis cache."""
        if not library.remove(song_id, drop_cache=cache):
            raise HTTPException(404, f"no song {song_id!r}")
        return {"deleted": song_id}

    # ---------------------------------------------------------------- capture
    #
    # Recording an input device into the library. The device is usually a
    # loopback driver (BlackHole, Loopback), which is how a song you can only
    # stream becomes a file the pipeline can read. Like the live panes, the
    # device belongs to the *server* - Scriptum records on the machine it runs
    # on, and the browser is only the control surface.
    #
    # There is no `analyze` option here on purpose. Stopping a capture creates
    # a library song exactly as an upload does, and the client already knows
    # how to analyse one of those - see `LibraryView.upload`. Wiring a second
    # path to the same job would be two things to keep in step.
    @app.get("/api/capture")
    def capture_status() -> dict:
        """The running capture's meter, or that there is none.

        Polled a few times a second while recording, and once on page load: the
        session lives in the server, so a browser closed mid-take comes back to
        find the recording still running rather than to a lost take.
        """
        return capture.CAPTURES.status()

    @app.post("/api/capture/start")
    def capture_start(body: dict = Body(default={})) -> dict:
        """Open an input device and start recording."""
        try:
            device = cli._input_device(body.get("device"))
        except SystemExit as exc:                   # cli raises this for a bad name
            raise HTTPException(400, str(exc)) from exc
        try:
            return capture.CAPTURES.start(
                name=str(body.get("name") or "").strip(),
                device=device,
                mono=bool(body.get("mono")),
                max_minutes=float(body.get("max_minutes") or 12.0))
        except RuntimeError as exc:                 # one sound card, one capture
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:                    # noqa: BLE001 - device refused
            raise HTTPException(400, f"cannot record from that device: {exc}") from exc

    @app.post("/api/capture/stop")
    def capture_stop(body: dict = Body(default={})) -> dict:
        """Finish the capture: file it as a song, or throw it away."""
        try:
            return capture.CAPTURES.stop(library.library_root(),
                                         discard=bool(body.get("discard")))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    # ------------------------------------------------------------------ songs
    @app.get("/api/songs/{song_id}")
    def get_song(song_id: str) -> dict:
        """Everything cached for a song: analysis, form, lyrics, stems, notes.

        Note *counts* rather than the notes themselves - a full stem is tens of
        thousands of notes and the client only ever draws a window of them.
        """
        path = library.find(song_id)
        if path is None:
            raise HTTPException(404, f"no song {song_id!r}")
        song = Song.open(path)
        work = song.work
        job = JOBS.for_song(song_id)
        return {
            **library.entry(path),
            "work": str(work),
            "job": job.snapshot() if job else None,
            "analysis": serialize.analysis_json(song.analysis) if song.analysis else None,
            "form": serialize.form_json(song.form) if song.form else None,
            "lyrics": [serialize.line_json(l) for l in song.lyrics],
            "stems": sorted(song.stems),
            # Present only for an imported multitrack. The client uses it to
            # stop promising a stem-separation pass that will not run, and to
            # say which of the band's tracks each stem actually is.
            "sources": song.sources or None,
            # Which stems demucs handed over as one file that turned out to
            # hold more than one player - and which ones were checked and
            # were not. Both are worth showing.
            "voices": serialize.voices_json(song.voices) if song.voices else None,
            "note_stems": {s: len(ns) for s, ns in sorted(song.notes.items())},
            "note_backends": dict(sorted(song.note_backends.items())),
            "llm_notes": song.llm_notes,
            "chart": (work / "chart.md").read_text() if (work / "chart.md").exists() else "",
        }

    @app.post("/api/songs/{song_id}/analyze")
    def analyze(song_id: str, opts: dict = Body(default={})) -> dict:
        """Kick off the pipeline on a worker thread; returns a job to follow."""
        path = library.find(song_id)
        if path is None:
            raise HTTPException(404, f"no song {song_id!r}")

        def work(job) -> dict:
            """Run the pipeline, logging each stage into the job transcript."""
            song = Song.open(path).run(
                separate=opts.get("separate", True),
                do_lyrics=opts.get("lyrics", True),
                do_notes=opts.get("notes", True),
                do_snippets=opts.get("snippets", False),
                do_voices=opts.get("voices", True),
                voice_count=opts.get("voice_count") or None,
                llm=opts.get("llm", False),
                force=opts.get("force", False),
                whisper_size=opts.get("whisper", "base"),
                device=opts.get("device") or None,
                backend=opts.get("backend") or None,
                log=job.log)
            if opts.get("stem_snippets"):
                job.log("• cutting per-instrument snippets…")
                song.write_snippets(stems=True, force=opts.get("force", False))
            from musiccopilot import chart as chart_mod
            job.log("• writing the recreate sheet…")
            chart_mod.write(song)
            job.log("done")
            return {"song": song_id}

        return JOBS.start("analyze", song_id, work).snapshot()

    @app.post("/api/songs/{song_id}/transcribe")
    def retranscribe(song_id: str, body: dict = Body(default={})) -> dict:
        """Re-read a song's notes with a different transcriber.

        Separate from /analyze because it is the cheap half of the pipeline:
        the stems, chords, lyrics and form are already right and are left
        alone, so changing engine costs one transcription pass instead of the
        whole slow run. Like analysis it is a job - a stem is tens of seconds,
        not a request - and the one-per-song rule keeps it from racing an
        analysis over the same cache files.
        """
        song = _song(song_id)
        backend = body.get("backend") or nt.DEFAULT_BACKEND
        if backend not in nt.BACKENDS:
            raise HTTPException(400, f"unknown transcriber {backend!r}")
        stems, force, path = body.get("stems") or None, bool(body.get("force")), song.path

        def work(job) -> dict:
            """Re-transcribe on a worker thread, logging each stem."""
            fresh = Song.open(path)
            try:
                changed = fresh.retranscribe(backend, stems=stems, force=force,
                                             log=job.log)
            except ValueError as exc:              # an unknown stem name
                raise HTTPException(400, str(exc)) from exc
            job.log("done" if changed else "already read with that engine")
            return {"song": song_id, "backend": backend, "stems": sorted(changed)}

        return JOBS.start("transcribe", song_id, work).snapshot()

    @app.post("/api/songs/{song_id}/voices")
    def split_voices(song_id: str, body: dict = Body(default={})) -> dict:
        """Look inside a stem for more than one player, or put one back together.

        A job rather than a request for the usual two reasons: it re-reads the
        stem and re-detects the form, which is tens of seconds, and `JOBS`'
        one-per-song rule is what stops it renaming stems out from under a
        running analysis.

        `count` insists on a number of players instead of letting the split
        decide, and implies a redo - otherwise asking for three on an
        already-split song would find the stem accounted for and do nothing.
        """
        song = _song(song_id)          # the split clusters notes, so it needs them
        if song.sources and not body.get("undo"):
            raise HTTPException(400, {
                "error": "imported",
                "detail": "these stems are the band's own tracks, one player each"})
        count = body.get("count") or None
        if count is not None and not (isinstance(count, int) and 1 <= count <= 4):
            raise HTTPException(400, f"count must be 1-4, got {count!r}")
        stems, undo = body.get("stems") or None, bool(body.get("undo"))
        force, path = bool(body.get("force")) or bool(count), song.path

        def work(job) -> dict:
            """Split (or merge) on a worker thread, then rebuild what depended on it."""
            fresh = Song.open(path)
            changed: set[str] = set()
            if undo:
                for source in list(fresh.voices):
                    changed |= fresh.merge_voices(source, log=job.log)
            else:
                changed = fresh.split_voices(stems=stems, count=count, force=force,
                                             log=job.log)
            if changed:
                # The form reads which stem leads a solo, and that question has
                # a different answer once a guitar has become two; the chart
                # and the part snippets are cut from the form.
                fresh = Song.open(path).run(log=job.log)
                from musiccopilot import chart as chart_mod
                chart_mod.write(fresh)
            job.log("done" if changed else "nothing changed")
            return {"song": song_id, "stems": sorted(changed)}

        return JOBS.start("voices", song_id, work).snapshot()

    @app.post("/api/songs/{song_id}/tracks")
    def reassign_tracks(song_id: str, body: dict = Body(...)) -> dict:
        """Point an imported song's tracks at different instruments.

        The mapping is checked before the import, but a wrong row is not always
        visible until the analysis comes back - a vocal track labelled `guitar`
        shows up as an empty Lyrics tab, not as an error. Re-importing to fix
        one row would mean handing over the whole multitrack again, so the
        stems are relabelled in place and only what the labels were
        load-bearing for is read again (`daw.reassign`).
        """
        from musiccopilot import daw
        path = library.find(song_id)
        if path is None:
            raise HTTPException(404, f"no song {song_id!r}")
        mapping = body.get("map") or {}
        if not mapping:
            raise HTTPException(400, "no tracks to reassign")
        # Checked here as well as inside `reassign`, because a bad row is an
        # argument error and belongs in the response - the job is for the
        # minutes of work that follow, and a 400 should not have to be dug out
        # of a failed job's transcript. The work itself stays in the job: it
        # renames files the pipeline reads, and `JOBS.start` is what keeps it
        # from doing that underneath a running analysis.
        sources = Song.open(path).sources
        if not sources:
            raise HTTPException(400, f"{path.name} was separated, not imported - "
                                     "there are no tracks to reassign")
        known = {t["name"].lower() for t in sources.get("tracks") or []}
        for name, stem in mapping.items():
            if str(name).lower() not in known:
                raise HTTPException(400, f"no track named {name!r}")
            if base_stem(stem) not in STEM_NAMES:
                raise HTTPException(400, f"unknown stem {stem!r}")
        backend = body.get("backend")

        def work(job) -> dict:
            job.log("• relabelling the imported tracks…")
            done = daw.reassign(workdir_for(path), mapping, log=job.log)
            if not done["moves"]:
                job.log("nothing to change")
                return {"song": song_id, **done}
            job.log(f"• re-reading {', '.join(done['recompute'])}…")
            song = Song.open(path).run(backend=backend, log=job.log)
            from musiccopilot import chart as chart_mod
            chart_mod.write(song)
            job.log("done")
            return {"song": song_id, **done}

        return JOBS.start("reassign", song_id, work).snapshot()

    @app.get("/api/songs/{song_id}/chart")
    def get_chart(song_id: str) -> dict:
        """The recreate sheet as markdown, written if it is not cached yet."""
        song = _song(song_id, need="form")
        from musiccopilot import chart as chart_mod
        p = song.work / "chart.md"
        if not p.exists():
            p = chart_mod.write(song)
        return {"markdown": p.read_text()}

    @app.get("/api/songs/{song_id}/notes")
    def get_notes(song_id: str, stem: str, start: float = 0.0,
                  end: float | None = None) -> dict:
        """Raw transcribed notes for a stem, optionally windowed."""
        song = _song(song_id)
        ns = song.notes.get(stem)
        if ns is None:
            raise HTTPException(404, f"no notes for {stem!r}")
        end = song.analysis.duration if end is None else end
        window = nt.in_window(ns, start, end)
        return {"stem": stem, "start": start, "end": end, "total": len(ns),
                "notes": [serialize.note_json(n) for n in window]}

    # -------------------------------------------------------------------- tab
    def _tab_payload(song, song_id, stem, part, bars, start, end, subdiv,
                     clean=False, voice="all") -> dict:
        """Everything the client needs to draw one passage of one stem.

        Shared by the plain tab request and the cleanup job so both return
        the same shape - the cleaned tab is the same view with a different
        note list behind it.
        """
        ns = song.notes.get(stem)
        if not ns:
            raise HTTPException(404, {"error": "no_notes", "stem": stem,
                                      "have": sorted(song.notes)})
        t_start, t_end, title = _window(song, part, bars, start, end)
        # `cli._voice` for the same reason `_window` is `cli._window`: which
        # notes "melody" means must not differ between the browser and the
        # terminal.
        try:
            window = cli._voice(nt.in_window(ns, t_start, t_end), voice)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        from musiccopilot.gemini import clean_window_cost
        # Whether the cleanup button applies to *this* window is a fact about
        # the limit in `musiccopilot.config`, so it is answered here and sent
        # along - the client greys the button out rather than keeping its own
        # copy of the numbers and drifting from them.
        can_clean, n_win, secs_win = clean_window_cost(window, t_start, t_end)
        cleaned = None
        if clean and window:
            from musiccopilot.gemini import TooLongToClean, clean_solo, solo_to_notes
            before = len(window)
            try:
                res = clean_solo(window, song.analysis, t_start, t_end)
            except TooLongToClean as exc:
                raise HTTPException(400, {"error": "window_too_long",
                                          "detail": str(exc), "notes": n_win,
                                          "seconds": round(secs_win, 1)}) from exc
            window = solo_to_notes(res, song.analysis.tempo, t0=t_start)
            cleaned = {"before": before, "after": len(window),
                       "changes": res.changes}

        out = _layout_for(song, stem, t_start, t_end, subdiv=subdiv,
                          title=title, window=window)
        out["heading"] = report.window_title(song, t_start, t_end,
                                             f"{title} · " if title else "")
        out["llm_clean"] = cleaned
        out["voice"] = voice
        out["clean_ok"] = can_clean
        out["clean_size"] = {"notes": n_win, "seconds": round(secs_win, 1),
                             "max_notes": LLM_CLEAN_MAX_NOTES,
                             "max_seconds": LLM_CLEAN_MAX_SECONDS}
        out["notes"] = [serialize.note_json(n) for n in window]
        if part and song.form:
            p = song.part(part)
            out["part"] = serialize.part_json(p) if p else None
            out["siblings"] = [q.name for q in song.form.matching(part)] if p else []
        return out

    @app.get("/api/songs/{song_id}/tab")
    def get_tab(song_id: str, stem: str = "guitar", part: str | None = None,
                bars: str | None = None, start: str | None = None,
                end: str | None = None, subdiv: int = 4,
                voice: str = "all") -> dict:
        """A drawable tab or staff for a passage.

        The window is resolved by the CLI's own `_window`, so `part`, `bars`,
        `start` and `end` behave identically to the command line (including
        `1:02` and `bar17` forms). `voice` is the same choice `--voice` makes:
        the whole stem, the line being played, or what is under it.
        """
        return _tab_payload(_song(song_id), song_id, stem, part, bars, start,
                            end, subdiv, voice=voice)

    # ------------------------------------------------------------------ score
    @app.get("/api/songs/{song_id}/score")
    def get_score(song_id: str, stem: str = "piano", part: str | None = None,
                  bars: str | None = None, start: str | None = None,
                  end: str | None = None, subdiv: int = 4,
                  voice: str = "all") -> dict:
        """A passage as engraved notation rather than a grid.

        Same window vocabulary as `/tab` (it goes through the same
        `cli._window`), and the same rule about where decisions live: this
        only calls `score.build_score` and serialises what comes back. The
        client engraves it; it does not decide any of it.
        """
        song = _song(song_id)
        ns = song.notes.get(stem)
        if not ns:
            raise HTTPException(404, {"error": "no_notes", "stem": stem,
                                      "have": sorted(song.notes)})
        from musiccopilot.score import build_score

        a = song.analysis
        t_start, t_end, title = _window(song, part, bars, start, end)
        try:
            window = cli._voice(nt.in_window(ns, t_start, t_end), voice)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        built = build_score(
            window, tempo=a.tempo, t0=t_start, beats_per_bar=a.beats_per_bar,
            subdiv=subdiv, first_bar=report.bar_number(song, t_start),
            key=a.key, chords=[c for c in a.chords if c.end > t_start and c.start < t_end],
            min_cols=cli._grid_cols(song, t_start, t_end, a, subdiv))
        out = serialize.score_json(built, title=title, stem=stem,
                                   start=t_start, end=t_end)
        out["heading"] = report.window_title(song, t_start, t_end,
                                             f"{title} · " if title else "")
        out["voice"] = voice
        out["notes"] = [serialize.note_json(n) for n in window]
        return out

    @app.post("/api/songs/{song_id}/tab/clean")
    def clean_tab(song_id: str, body: dict = Body(default={})) -> dict:
        """Ask Gemini to declutter a transcribed passage, as a job.

        This is a job rather than a query flag because the call is slow - a
        75-note solo window measures around a minute, past what a browser will
        reliably hold a GET open for. Like the CLI's `--llm-clean` it is
        display-time only: nothing is written back to `notes/<stem>.json`, so a
        bad cleanup costs one re-request.

        It is also a **snippet** operation. The default window on the Tabs page
        is the whole song (`windowParams` has to say so; see CLAUDE.md), and on
        crystallize's guitar stem that is 1438 notes - which the model is asked
        to read *and* write back, for something like a hundred times the cost of
        the guitar solo the button exists for. `clean_window_cost` is checked
        here so that request is a 400 the page can explain.
        """
        song = _song(song_id)
        stem = body.get("stem", "guitar")

        # Check the window before spending a job on it. `clean_solo` enforces
        # the same limit itself, but a request that is going to be refused
        # should be refused now, as a 400 the button can explain, rather than
        # as a job the user watches and then finds failed.
        from musiccopilot.gemini import clean_window_cost
        ns = song.notes.get(stem) or []
        t0, t1, _ = _window(song, body.get("part"), body.get("bars"),
                            body.get("start"), body.get("end"))
        ok, n_win, secs_win = clean_window_cost(nt.in_window(ns, t0, t1), t0, t1)
        if not ok:
            raise HTTPException(400, {
                "error": "window_too_long", "notes": n_win,
                "seconds": round(secs_win, 1), "max_notes": LLM_CLEAN_MAX_NOTES,
                "max_seconds": LLM_CLEAN_MAX_SECONDS,
                "detail": (f"{n_win} notes over {secs_win:.0f}s is past what cleanup "
                           f"is allowed to cost. Pick a part or a bar range "
                           f"(up to {LLM_CLEAN_MAX_NOTES} notes / "
                           f"{LLM_CLEAN_MAX_SECONDS:.0f}s) and clean that.")})

        def work(job) -> dict:
            """Run the cleanup and lay the result out."""
            job.log(f"• asking Gemini to clean up {stem}…")
            out = _tab_payload(song, song_id, stem, body.get("part"),
                               body.get("bars"), body.get("start"),
                               body.get("end"), int(body.get("subdiv", 4)),
                               clean=True, voice=body.get("voice", "all"))
            c = out["llm_clean"]
            job.log(f"• {c['before']} → {c['after']} notes" if c else "• nothing to clean")
            job.log("done")
            return out

        return JOBS.start("clean", f"{song_id}:clean:{stem}", work,
                          timeout=GEMINI_DEADLINE).snapshot()

    @app.get("/api/songs/{song_id}/chords")
    def get_chord_shapes(song_id: str) -> dict:
        """Fingerings for the chords this song actually uses."""
        from musiccopilot.tabs import chord_frets, chord_shape
        song = _song(song_id)
        seen, out = set(), []
        for c in song.analysis.chords:
            if c.name in seen or c.name == "N.C." or c.root < 0:
                continue
            seen.add(c.name)
            out.append({"name": c.name, "root": c.root, "quality": c.quality,
                        "frets": chord_frets(c.name, c.root, c.quality),
                        "shape": chord_shape(c.name, c.root, c.quality)})
        return {"chords": out}

    # ------------------------------------------------------------------- solo
    @app.post("/api/songs/{song_id}/solo")
    def make_solo(song_id: str, body: dict = Body(...)) -> dict:
        """Ask Gemini for a solo over a passage. Runs as a job - it is slow."""
        song = _song(song_id)
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "a prompt is required")

        if not any(body.get(k) for k in ("part", "bars", "start", "end")):
            t_start, t_end = song.solo_section()
            title = ""
        else:
            t_start, t_end, title = _window(song, body.get("part"), body.get("bars"),
                                            body.get("start"), body.get("end"))

        def work(job) -> dict:
            """Generate, render to audio, and lay the result out as a tab."""
            from musiccopilot import synth
            from musiccopilot.gemini import solo_to_notes, suggest_solo

            a = song.analysis
            instrument = body.get("instrument", "guitar")
            job.log(f"• asking Gemini for a {instrument} solo…")
            extra = {"style_notes": song.llm_notes[:1200]} if song.llm_notes else None
            solo = suggest_solo(prompt, a, t_start, t_end, extra=extra,
                                temperature=float(body.get("temperature", 1.0)))
            solo_notes = solo_to_notes(solo, a.tempo, t0=t_start)

            job.log(f"• {len(solo_notes)} notes — rendering audio…")
            voice = {"bass": "bass", "piano": "clean"}.get(instrument, "lead")
            lead = synth.render(solo_notes, SR, voice,
                                duration=t_end - t_start + 2.0, t0=t_start)
            over = body.get("over", "backing")
            bed = None
            if over == "backing" and song.stems:
                bed = song.backing(exclude=(instrument,))[
                    int(t_start * SR):int((t_end + 2) * SR)]
            elif over == "chords":
                bed = synth.render_chords(
                    [c for c in a.chords if c.end > t_start and c.start < t_end],
                    SR, t0=t_start, duration=t_end - t_start + 2.0)
            y = (synth.mix(lead, bed, gains=[1.0, float(body.get("bed_gain", 0.55))])
                 if bed is not None else synth.mix(lead))

            slug = "".join(ch for ch in prompt[:24]
                           if ch.isalnum() or ch == " ").strip().replace(" ", "_")
            wav = synth.write(song.work / f"solo_{slug or 'take'}.wav", y)
            midi = nt.write_midi(solo_notes, song.work / f"solo_{slug or 'take'}.mid",
                                 a.tempo)
            layout = _layout_for(song, instrument, t_start, t_end,
                                 subdiv=int(body.get("subdiv", 4)),
                                 title=title or "Generated solo", window=solo_notes)
            job.log("done")
            return {
                "title": solo.title, "scale": solo.scale,
                "explanation": solo.explanation,
                "start": t_start, "end": t_end,
                "instrument": instrument,
                "notes": [serialize.note_json(n) for n in solo_notes],
                "layout": layout,
                "audio": f"/api/songs/{song_id}/media/file/{wav.name}",
                "midi": f"/api/songs/{song_id}/media/file/{midi.name}",
            }

        return JOBS.start("solo", f"{song_id}:solo", work,
                          timeout=GEMINI_DEADLINE).snapshot()

    # ------------------------------------------------------------------ media
    @app.get("/api/songs/{song_id}/media/mix")
    def media_mix(song_id: str):
        """The original uploaded file."""
        path = library.find(song_id)
        if path is None:
            raise HTTPException(404, "no such song")
        return FileResponse(path)

    @app.get("/api/songs/{song_id}/media/stem/{stem}")
    def media_stem(song_id: str, stem: str):
        """One separated stem - `.m4a` for a song analysed since stem
        compression, `.wav` for one cached before it."""
        song = _song(song_id, need="none")
        path = song.stems.get(stem)
        if path is None or not path.is_file():
            raise HTTPException(404, "no such stem")
        return FileResponse(path)

    @app.get("/api/songs/{song_id}/media/snippet/{slug}")
    def media_snippet(song_id: str, slug: str):
        """One part's audio excerpt, cut on the fly from the mixdown.

        Parts used to each get a pre-rendered wav under `snippets/` - 90 MB on
        a four-minute song for a feature that is only ever a few seconds of
        playback at a time. `Part.start`/`end` already say exactly what to
        cut, so this slices the source audio per request instead; a browser
        never asks for the same ten seconds often enough for that to matter.
        """
        song = _song(song_id, need="form")
        part = next((p for p in song.form.parts if p.slug == slug), None)
        if part is None:
            raise HTTPException(404, "no such part")
        y = au.excerpt(song.audio(mono=False), part.start, part.end, SR)
        return _wav_response(y, SR)

    @app.get("/api/songs/{song_id}/media/file/{name}")
    def media_file(song_id: str, name: str):
        """A generated file in the song's cache (solo takes, rendered tabs)."""
        song = _song(song_id, need="none")
        return FileResponse(_safe(song.work, name))

    @app.get("/api/songs/{song_id}/media/backing")
    def media_backing(song_id: str, exclude: str = "guitar"):
        """The song minus one or more stems - the play-along bed.

        Mixed fresh on every request rather than cached to disk. It used to
        write a `_backing_minus_<stems>.wav` per exclusion set and never clean
        any of them up - one song's worth reached 14 files and 312 MB, because
        the set of possible exclusions is the power set of its stems. `load()`
        (`useTransport.js`) only calls this once per stem toggle, not once per
        seek, so decoding a handful of stems here is a one-off per toggle, not
        a hot path - the cost this used to be cached against.
        """
        song = _song(song_id, need="none")
        drop = tuple(sorted(s for s in exclude.split(",") if s))
        if not song.stems:
            return FileResponse(song.path)
        return _wav_response(song.backing(exclude=drop), SR)

    # ------------------------------------------------------------------- jobs
    @app.get("/api/jobs")
    def get_jobs() -> list[dict]:
        """Jobs still running, so a reloaded page can re-attach to them."""
        JOBS.prune()
        return JOBS.active()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        """One job's status and full transcript."""
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/stream")
    async def stream_job(job_id: str):
        """Server-sent events: every progress line, then the final state.

        The job logs from a worker thread, so the wait is pushed to a thread
        too - blocking the event loop here would stall every other request
        while a five-minute separation runs.
        """
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "no such job")

        async def events():
            """Yield new transcript lines as they appear, then a done event."""
            sent = 0
            while True:
                snap = job.snapshot()
                for line in snap["lines"][sent:]:
                    yield f"event: log\ndata: {json.dumps({'line': line})}\n\n"
                sent = len(snap["lines"])
                if snap["state"] != "running":
                    yield f"event: end\ndata: {json.dumps(snap)}\n\n"
                    return
                await asyncio.to_thread(job.wait, 15.0)
                yield ": keepalive\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------- live
    @app.get("/api/devices")
    def get_devices() -> dict:
        """Input devices the server can hear."""
        try:
            return {"devices": live.devices()}
        except RuntimeError as exc:
            return JSONResponse({"devices": [], "error": str(exc)}, status_code=503)

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket) -> None:
        """Stream live analysis frames while the mic is open.

        The client picks the mode: `tab` for "what is he playing", `key` for
        "what are we jamming in". Frames are produced on a thread because
        every one of them reads state the analysis worker writes under a lock.
        """
        await ws.accept()
        params = ws.query_params
        mode = params.get("mode", "tab")
        instrument = params.get("instrument", "guitar")
        device = params.get("device") or None
        if device is not None and device.lstrip("-").isdigit():
            device = int(device)
        try:
            fps = max(1.0, min(20.0, float(params.get("fps", 8))))
        except ValueError:
            fps = 8.0

        session = None
        try:
            session = live.LiveSession(
                mode=mode, instrument=instrument, device=device,
                tempo=float(params.get("tempo", 0) or 0),
                subdiv=int(params.get("subdiv", 4)))
            session.__enter__()
        except Exception as exc:                    # noqa: BLE001
            await ws.send_json({"type": "error", "message": str(exc)})
            await ws.close()
            return

        await ws.send_json({"type": "started", "mode": mode,
                            "instrument": instrument, "sr": SR})
        stopped = asyncio.Event()

        async def reader() -> None:
            """Handle client commands (currently just save/stop)."""
            try:
                while True:
                    msg = await ws.receive_json()
                    if msg.get("cmd") == "save":
                        res = await asyncio.to_thread(session.save,
                                                      msg.get("name", ""))
                        await ws.send_json({"type": "saved", **res})
                    elif msg.get("cmd") == "stop":
                        break
            except (WebSocketDisconnect, RuntimeError, ValueError):
                pass
            finally:
                stopped.set()

        task = asyncio.create_task(reader())
        try:
            while not stopped.is_set():
                frame = await asyncio.to_thread(session.frame)
                await ws.send_json(frame)
                await asyncio.sleep(1.0 / fps)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            stopped.set()
            task.cancel()
            session.__exit__(None, None, None)
            try:
                await ws.close()
            except RuntimeError:
                pass

    # ----------------------------------------------------------------- client
    if WEB_DIST.is_dir():
        app.mount("/", _SPAFiles(directory=WEB_DIST, html=True), name="web")
    else:
        @app.get("/")
        def no_client() -> dict:
            """Explain the missing build rather than 404ing on the root."""
            return {"scriptum": "api only",
                    "hint": "cd web && npm install && npm run build",
                    "docs": "/docs"}

    return app


app = create_app()
