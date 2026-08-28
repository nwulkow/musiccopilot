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
from pathlib import Path
from types import SimpleNamespace

from fastapi import (Body, FastAPI, HTTPException, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from musiccopilot import cli, notes as nt, report
from musiccopilot.config import SR, TUNINGS
from musiccopilot.pipeline import TRANSCRIBE_STEMS, Song

from . import library, live, serialize
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
    fretted_stem = stem in TUNINGS or stem == "guitar"
    instrument = ("bass" if stem == "bass" else
                  "guitar" if stem == "guitar" else pick_instrument(window))
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
    """

    async def get_response(self, path: str, scope):
        """Serve the file, or the SPA shell when the path is a client route."""
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith(("api", "ws")):
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
                "stems": TRANSCRIBE_STEMS}

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

    @app.delete("/api/library/{song_id}")
    def delete_song(song_id: str, cache: bool = True) -> dict:
        """Remove a song and (by default) its analysis cache."""
        if not library.remove(song_id, drop_cache=cache):
            raise HTTPException(404, f"no song {song_id!r}")
        return {"deleted": song_id}

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
            "note_stems": {s: len(ns) for s, ns in sorted(song.notes.items())},
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
                do_snippets=opts.get("snippets", True),
                llm=opts.get("llm", False),
                force=opts.get("force", False),
                whisper_size=opts.get("whisper", "base"),
                device=opts.get("device") or None,
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
                     clean=False) -> dict:
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
        window = nt.in_window(ns, t_start, t_end)
        cleaned = None
        if clean and window:
            from musiccopilot.gemini import clean_solo, solo_to_notes
            before = len(window)
            res = clean_solo(window, song.analysis, t_start, t_end)
            window = solo_to_notes(res, song.analysis.tempo, t0=t_start)
            cleaned = {"before": before, "after": len(window),
                       "changes": res.changes}

        out = _layout_for(song, stem, t_start, t_end, subdiv=subdiv,
                          title=title, window=window)
        out["heading"] = report.window_title(song, t_start, t_end,
                                             f"{title} · " if title else "")
        out["llm_clean"] = cleaned
        out["notes"] = [serialize.note_json(n) for n in window]
        if part and song.form:
            p = song.part(part)
            out["part"] = serialize.part_json(p) if p else None
            out["siblings"] = [q.name for q in song.form.matching(part)] if p else []
        return out

    @app.get("/api/songs/{song_id}/tab")
    def get_tab(song_id: str, stem: str = "guitar", part: str | None = None,
                bars: str | None = None, start: str | None = None,
                end: str | None = None, subdiv: int = 4) -> dict:
        """A drawable tab or staff for a passage.

        The window is resolved by the CLI's own `_window`, so `part`, `bars`,
        `start` and `end` behave identically to the command line (including
        `1:02` and `bar17` forms).
        """
        return _tab_payload(_song(song_id), song_id, stem, part, bars, start,
                            end, subdiv)

    # ------------------------------------------------------------------ score
    @app.get("/api/songs/{song_id}/score")
    def get_score(song_id: str, stem: str = "piano", part: str | None = None,
                  bars: str | None = None, start: str | None = None,
                  end: str | None = None, subdiv: int = 4) -> dict:
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
        window = nt.in_window(ns, t_start, t_end)
        built = build_score(
            window, tempo=a.tempo, t0=t_start, beats_per_bar=a.beats_per_bar,
            subdiv=subdiv, first_bar=report.bar_number(song, t_start),
            key=a.key, chords=[c for c in a.chords if c.end > t_start and c.start < t_end],
            min_cols=cli._grid_cols(song, t_start, t_end, a, subdiv))
        out = serialize.score_json(built, title=title, stem=stem,
                                   start=t_start, end=t_end)
        out["heading"] = report.window_title(song, t_start, t_end,
                                             f"{title} · " if title else "")
        out["notes"] = [serialize.note_json(n) for n in window]
        return out

    @app.post("/api/songs/{song_id}/tab/clean")
    def clean_tab(song_id: str, body: dict = Body(default={})) -> dict:
        """Ask Gemini to declutter a transcribed passage, as a job.

        This is a job rather than a query flag because the call is slow -
        a 75-note solo window measures around 50s, past what a browser will
        reliably hold a GET open for, and longer passages are worse. Like the CLI's
        `--llm-clean` it is display-time only: nothing is written back to
        `notes/<stem>.json`, so a bad cleanup costs one re-request.
        """
        song = _song(song_id)
        stem = body.get("stem", "guitar")

        def work(job) -> dict:
            """Run the cleanup and lay the result out."""
            job.log(f"• asking Gemini to clean up {stem}…")
            out = _tab_payload(song, song_id, stem, body.get("part"),
                               body.get("bars"), body.get("start"),
                               body.get("end"), int(body.get("subdiv", 4)),
                               clean=True)
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
        """One separated stem."""
        song = _song(song_id, need="none")
        return FileResponse(_safe(song.work, "stems", f"{stem}.wav"))

    @app.get("/api/songs/{song_id}/media/snippet/{name}")
    def media_snippet(song_id: str, name: str):
        """One part's audio excerpt."""
        song = _song(song_id, need="none")
        return FileResponse(_safe(song.work, "snippets", name))

    @app.get("/api/songs/{song_id}/media/file/{name}")
    def media_file(song_id: str, name: str):
        """A generated file in the song's cache (solo takes, rendered tabs)."""
        song = _song(song_id, need="none")
        return FileResponse(_safe(song.work, name))

    @app.get("/api/songs/{song_id}/media/backing")
    def media_backing(song_id: str, exclude: str = "guitar"):
        """The song minus one or more stems - the play-along bed.

        Cached per exclusion set: mixing stems means decoding several wavs, and
        a play-along that re-does that on every seek is unusable.
        """
        from musiccopilot import audio
        song = _song(song_id, need="none")
        drop = tuple(sorted(s for s in exclude.split(",") if s))
        if not song.stems:
            return FileResponse(song.path)
        out = song.work / f"_backing_minus_{'-'.join(drop) or 'none'}.wav"
        if not out.exists():
            audio.save(out, song.backing(exclude=drop), SR)
        return FileResponse(out)

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
