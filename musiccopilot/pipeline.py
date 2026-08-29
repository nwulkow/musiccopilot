"""Orchestration: run the full analysis once, cache it, reload it instantly."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import audio, lyrics as lyr, notes as nt
from .analysis import Analysis, analyze
from .config import SR, base_stem, workdir_for
from .form import Form, Part, detect_form

TRANSCRIBE_STEMS = ["guitar", "bass", "vocals", "piano", "other"]


@dataclass
class Song:
    """The cache-backed handle every command works through.

    `open()` reloads whatever stages already exist on disk under `work`;
    `run()` computes only the stages that are still missing (or, with
    `force`, everything). Fields left at their defaults just mean that
    stage hasn't been cached yet.
    """

    path: Path
    work: Path
    stems: dict[str, Path] = field(default_factory=dict)
    analysis: Analysis | None = None
    form: Form | None = None
    notes: dict[str, list[nt.Note]] = field(default_factory=dict)
    note_backends: dict[str, str] = field(default_factory=dict)
    lyrics: list[lyr.Line] = field(default_factory=list)
    llm_notes: str = ""
    sources: dict = field(default_factory=dict)   # set when stems were imported

    # --- cache helpers ------------------------------------------------------
    def _read(self, name: str):
        """Load a JSON cache file from `work`, or None if it hasn't been written yet."""
        p = self.work / name
        return json.loads(p.read_text()) if p.exists() else None

    def _write(self, name: str, data) -> None:
        """Write a JSON cache file under `work`, creating/overwriting it."""
        (self.work / name).write_text(json.dumps(data, indent=1))

    def audio(self, mono: bool = True):
        """Load the original source audio (not a stem) at the pipeline's sample rate."""
        return audio.load(self.path, SR, mono)

    def backing(self, exclude: tuple[str, ...] = ("guitar",)):
        """The song minus the lead instrument - to solo over.

        `exclude` accepts either - an instrument ("guitar" drops both of an
        imported multitrack's guitarists, which is what you want to solo over)
        or one exact stem ("guitar-2" drops only the second, which is what the
        second guitarist wants from `--minus-stem`).
        """
        keep = [s for s in self.stems
                if s not in exclude and base_stem(s) not in exclude]
        return audio.mix(self.stems, keep, SR) if keep else self.audio()

    # --- build --------------------------------------------------------------
    @classmethod
    def open(cls, path: str | Path) -> "Song":
        """Build a Song from whatever is already cached in `workdir_for(path)`.

        Never computes anything - each stage is loaded if its file exists and
        left at its default otherwise. Call `run()` to fill in the rest.
        """
        path = Path(path).expanduser().resolve()
        song = cls(path=path, work=workdir_for(path))
        stem_dir = song.work / "stems"
        song.stems = {p.stem: p for p in stem_dir.glob("*.wav")} if stem_dir.exists() else {}
        if (a := song._read("analysis.json")):
            song.analysis = Analysis.from_dict(a)
        if (f := song._read("form.json")):
            song.form = Form.from_dict(f)
        if (l := song._read("lyrics.json")) is not None:
            song.lyrics = lyr.from_dicts(l)
        for p in (song.work / "notes").glob("*.json"):
            song.notes[p.stem] = nt.from_dicts(json.loads(p.read_text()))
        # Which transcriber each stem's notes came from. Kept at the work root
        # rather than inside `notes/`, which is globbed by stem name - a
        # `notes/backends.json` would read back as a stem called "backends".
        # Written by `daw.import_session`; its presence is what tells `run()`
        # these stems came off a multitrack rather than out of demucs.
        song.sources = song._read("sources.json") or {}
        song.note_backends = song._read("note_backends.json") or {}
        # Notes cached before the engine was selectable were produced by the
        # old per-stem split, which is exactly `auto`. Assume that rather than
        # treating every existing cache as unknown and re-transcribing minutes
        # of audio the first time someone opens a song.
        for stem in song.notes:
            song.note_backends.setdefault(stem, nt.resolve_backend("auto", stem))
        note_file = song.work / "llm_notes.txt"
        song.llm_notes = note_file.read_text() if note_file.exists() else ""
        return song

    def run(self, *, separate: bool = True, do_lyrics: bool = True,
            do_notes: bool = True, do_form: bool = True, do_snippets: bool = True,
            llm: bool = False, force: bool = False, whisper_size: str = "base",
            device: str | None = None, backend: str | None = None,
            log=print) -> "Song":
        """Run whichever stages are missing, in cache-dependency order, and write each one.

        Each `do_*` flag only gates whether that stage is *eligible* to run;
        whether it actually runs still depends on what's already cached (or
        `force`, which redoes everything). The order matters because later
        stages consume earlier ones: notes and lyrics feed the form, and the
        form in turn triggers a monophonic re-transcription of each solo's
        lead stem (`_refine_lead_notes`) and retriggers snippet writing, since
        newly-found part boundaries make the old snippet cuts wrong. `fresh`
        and `form_fresh` track whether analysis/form were *just* computed this
        call (as opposed to loaded from cache) so those downstream stages know
        whether they need to redo their work too.

        `backend` picks the note transcriber (`notes.BACKENDS`). Changing it is
        a cache miss for the stems it changes, so switching engine re-does the
        notes - and only the notes: stems, chords and lyrics are untouched.
        """
        # Imported stems are never re-separated, `--force` included. They are
        # the real multitrack - the thing demucs spends minutes *estimating* -
        # so running separation over them would replace the recording with a
        # guess at the recording, and there is no way back from that.
        if separate and not self.sources and (force or not self.stems):
            log("• separating stems (this is the slow part)…")
            self.stems = audio.separate(self.path, device=device, force=force)

        fresh = force or self.analysis is None
        if fresh:
            log("• tempo, key, chords, structure…")
            y = self.audio()
            bed = audio.harmonic_bed(self.stems) if self.stems else None
            voc = audio.load(self.stems["vocals"]) if "vocals" in self.stems else None
            self.analysis = analyze(y, SR, harmonic=bed, vocals=voc)
            self._write("analysis.json", self.analysis.to_dict())

        retranscribed: set[str] = set()
        if do_notes:
            retranscribed = self.transcribe_notes(backend=backend, force=force, log=log)

        if do_lyrics and "vocals" in self.stems and (force or not self.lyrics):
            log("• transcribing lyrics…")
            self.lyrics = lyr.transcribe(self.stems["vocals"], whisper_size)
            self._write("lyrics.json", lyr.to_dicts(self.lyrics))

        form_fresh = do_form and (fresh or self.form is None)
        if form_fresh:                    # chord loops come from the chord track
            log("• working out the song form…")
            voc = audio.load(self.stems["vocals"]) if "vocals" in self.stems else None
            self.form = detect_form(self.analysis, self.audio(), SR, vocals=voc,
                                    notes=self.notes, lyrics=self.lyrics, stems=self.stems)
            self._write("form.json", self.form.to_dict())

        # `retranscribed` matters as much as `form_fresh` here: a fresh pass
        # over a stem overwrites the monophonic splice this stage put in it,
        # so a change of engine has to put the solos back or every bend in the
        # tab quietly disappears.
        if do_notes and (form_fresh or force or retranscribed) and self.form:
            self._refine_lead_notes(backend=backend, log=log)

        if do_snippets and self.form:
            # new part boundaries mean the old wavs are cut in the wrong places
            new = self.write_snippets(force=force or form_fresh)
            if new:
                log(f"• wrote {len(new)} part snippets → {self.work / 'snippets'}")

        if llm and (force or not self.llm_notes):
            from .gemini import listening_notes
            log("• asking Gemini for listening notes…")
            try:
                self.llm_notes = listening_notes(self.path, self.analysis)
                (self.work / "llm_notes.txt").write_text(self.llm_notes)
            except Exception as exc:                      # noqa: BLE001
                log(f"  (skipped: {exc})")
        return self

    # --- notes --------------------------------------------------------------
    def transcribe_notes(self, sources: dict[str, Path] | None = None, *,
                         backend: str | None = None, force: bool = False,
                         log=print) -> set[str]:
        """Transcribe every stem whose notes are missing - or whose cached
        notes came from a *different* backend - and return the stems that
        changed. Does not touch the solo splices; see `retranscribe`.

        The backend each stem was read with is recorded in
        `note_backends.json`, and that record is what makes the engine a real
        setting: without it, asking for CREPE on a song already analysed with
        Basic Pitch would hit the `stem in self.notes` cache check, reload the
        old notes, and look exactly like the setting did nothing.
        """
        if sources is None:
            # Keyed by instrument, not by name: an imported multitrack's
            # `guitar-2` is as transcribable as its `guitar`, and drums are
            # skipped whichever kit mic they came off.
            sources = {s: p for s, p in self.stems.items()
                       if base_stem(s) in TRANSCRIBE_STEMS}
            # with no stems at all, transcribe the mix so tabs still work
            sources = sources or {"mix": self.path}
        (self.work / "notes").mkdir(parents=True, exist_ok=True)

        changed: set[str] = set()
        for stem, src in sources.items():
            want = nt.resolve_backend(backend, stem)
            if stem in self.notes and not force and self.note_backends.get(stem) == want:
                continue
            log(f"• transcribing notes: {stem} ({want})…")
            self.notes[stem] = nt.transcribe(src, stem, backend=want)
            (self.work / "notes" / f"{stem}.json").write_text(
                json.dumps(nt.to_dicts(self.notes[stem])))
            self.note_backends[stem] = want
            changed.add(stem)
        if changed:
            self._write("note_backends.json", self.note_backends)
        return changed

    def retranscribe(self, backend: str | None = None, *,
                     stems: list[str] | None = None, force: bool = False,
                     log=print) -> set[str]:
        """Re-read the notes with a different engine, solos included.

        The cheap half of `run()`: stems, chords, lyrics and the form all stay
        as they are, so changing your mind about the transcriber costs one
        transcription pass rather than the whole slow pipeline. Only the lead
        windows of stems that actually changed are refined, because a fresh
        pass over a stem wipes the monophonic solo notes spliced into it.
        """
        sources = None
        if stems is not None:
            sources = {s: self.stems[s] for s in stems if s in self.stems}
            if not sources:
                raise ValueError(f"no such stems: {', '.join(stems)}")
        changed = self.transcribe_notes(sources, backend=backend, force=force, log=log)
        if changed and self.form:
            self._refine_lead_notes(backend=backend, only=changed, log=log)
        return changed

    def _refine_lead_notes(self, backend: str | None = None,
                           only: set[str] | None = None, log=print) -> None:
        """Re-transcribe each solo's lead stem monophonically over just that
        part's window, and splice the result into the cached notes.

        Basic Pitch is polyphonic, so on a single-line solo it both invents
        extra simultaneous pitches and chops bends into note staircases - the
        wrong model for one string playing one note at a time. It stays the
        transcriber for the rest of each stem (rhythm parts are genuinely
        polyphonic); only the identified solo window gets replaced.
        """
        touched: set[str] = set()
        for part in self.form.parts:
            stem = part.lead
            if not stem or stem not in self.stems or stem not in self.notes:
                continue
            if only is not None and stem not in only:
                continue
            log(f"• re-transcribing {part.name} ({stem}, monophonic)…")
            clip = self.work / "_lead_tmp.wav"
            try:
                y = audio.excerpt(audio.load(self.stems[stem], SR, mono=True),
                                  part.start, part.end, SR, fade=0.0)
                audio.save(clip, y, SR)
                lead_notes = nt.transcribe_lead(clip, stem, backend=backend)
            finally:
                clip.unlink(missing_ok=True)
            for n in lead_notes:                # clip-local time -> song time
                n.start += part.start
                n.end += part.start
            self.notes[stem] = nt.replace_window(self.notes[stem], part.start, part.end,
                                                 lead_notes)
            touched.add(stem)
        for stem in touched:
            (self.work / "notes" / f"{stem}.json").write_text(
                json.dumps(nt.to_dicts(self.notes[stem])))

    # --- part snippets ------------------------------------------------------
    def write_snippets(self, stems: bool = False, force: bool = False) -> list[Path]:
        """One wav per part - `snippets/03_chorus-1.wav` - plus optional stems.

        Also stamps the file name onto each Part, so form.json stays the index
        of what is on disk.
        """
        if not self.form:
            return []
        out = self.work / "snippets"
        out.mkdir(parents=True, exist_ok=True)
        wanted = {f"{i:02d}_{p.slug}": p for i, p in enumerate(self.form.parts, 1)}
        todo = [n for n, p in wanted.items()
                if force or not (out / f"{n}.wav").exists()
                or (stems and self.stems and not (out / n).is_dir())]

        written: list[Path] = []
        if todo:
            y = audio.load(self.path, SR, mono=False)
            for name in todo:
                part = wanted[name]
                written.append(audio.save(out / f"{name}.wav",
                                          audio.excerpt(y, part.start, part.end)))
            if stems:
                for stem, path in self.stems.items():
                    ys = audio.load(path, SR, mono=False)
                    for name in todo:
                        part = wanted[name]
                        audio.save(out / name / f"{stem}.wav",
                                   audio.excerpt(ys, part.start, part.end))
        for name, part in wanted.items():
            part.snippet = f"{name}.wav"
        self._write("form.json", self.form.to_dict())
        return written

    # --- convenience --------------------------------------------------------
    def part(self, query: str) -> Part | None:
        """Look a part up by name: 'chorus', 'verse 2', 'guitar solo', '#4'."""
        return self.form.find(query) if self.form else None

    def solo_section(self) -> tuple[float, float]:
        """Best guess at a section to solo over: the solo, else the longest
        instrumental part, else the longest section of any kind."""
        if self.form:
            cands = [p for p in self.form.parts if "solo" in p.role.lower()]
            cands = cands or [p for p in self.form.parts if p.kind == "instrumental"]
            if cands:
                p = max(cands, key=lambda p: p.end - p.start)
                return p.start, p.end
        a = self.analysis
        cands = [s for s in a.sections if s.kind == "instrumental"] or a.sections
        s = max(cands, key=lambda s: s.end - s.start)
        return s.start, s.end
