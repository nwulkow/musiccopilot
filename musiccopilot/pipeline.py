"""Orchestration: run the full analysis once, cache it, reload it instantly."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import (audio, clean as cln, lyrics as lyr, notes as nt, texture as tex,
               voices as vc)
from .analysis import Analysis, analyze
from .config import SR, base_stem, workdir_for
from .form import Form, Part, detect_form

TRANSCRIBE_STEMS = ["guitar", "bass", "vocals", "piano", "other"]


def _shaped(notes: list[nt.Note], source, spectrum=None) -> list[nt.Note]:
    """Turn a raw transcription into the one worth caching.

    Two passes that have to run in this order and stay separate modules.
    `clean` checks the notes against the audio and only ever removes; `texture`
    then groups what is left into the strums it was struck as and pulls each
    one onto a single onset, which only ever moves. Between them they are the
    difference between a note list and a tab: without the first, a stem the
    band never played still gets a page of notes off the noise floor, and
    without the second every strummed chord is printed as an arpeggio.
    """
    return tex.align(cln.clean(notes, source, spectrum=spectrum))


def _shaping() -> list[int]:
    """Which revision of each of those passes a cached stem went through.

    A pair rather than one number because they are separate modules with
    separate reasons to change, and a mismatch in either is a cache miss in
    `transcribe_notes` exactly as a different backend is. A cache written
    before `texture` existed reads back as `[n, 0]` (see `Song.open`) and is
    re-read once, which is what a revision bump has always cost here.
    """
    return [cln.REVISION, tex.REVISION]


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
    notes_clean: dict[str, list[int]] = field(default_factory=dict)
    lyrics: list[lyr.Line] = field(default_factory=list)
    llm_notes: str = ""
    sources: dict = field(default_factory=dict)   # set when stems were imported
    voices: dict[str, vc.Split] = field(default_factory=dict)   # stems split again

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
        # Matches both `STEM_EXT` and the `.wav` a cache from before stem
        # compression still holds - a song is not re-separated just because
        # the default format changed.
        song.stems = ({p.stem: p for p in stem_dir.glob("*.wav")}
                      if stem_dir.exists() else {})
        if stem_dir.exists():
            song.stems.update({p.stem: p for p in stem_dir.glob(f"*{audio.STEM_EXT}")})
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
        # Which stems hold more than one player, and which stems those became.
        # Its presence is what stops `split_voices` looking inside a stem it
        # has already been through - including the ones it decided *not* to
        # split, which is a finding worth caching rather than re-deriving.
        song.voices = {name: vc.from_dict(row)
                       for name, row in (song._read("voices.json") or {}).items()}
        song.note_backends = song._read("note_backends.json") or {}
        # Which revision of each note-shaping pass (`clean.py`, then
        # `texture.py`) every stem's notes went through. Unlike
        # `note_backends.json` there is nothing sensible to backfill a missing
        # entry with: a cache written before the checks existed holds whatever
        # the tracker said, residue and all, so it is left unrecorded and
        # re-read on the next run. A bare int is what this file held before
        # `texture` joined `clean`, and means exactly that: cleaned at that
        # revision, never grouped into strums.
        song.notes_clean = {stem: rev if isinstance(rev, list) else [rev, 0]
                            for stem, rev in (song._read("notes_clean.json")
                                              or {}).items()}
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
            do_notes: bool = True, do_form: bool = True, do_snippets: bool = False,
            do_voices: bool = True, voice_count: int | None = None,
            llm: bool = False, force: bool = False, whisper_size: str = "base",
            device: str | None = None, backend: str | None = None,
            log=print) -> "Song":
        """Run whichever stages are missing, in cache-dependency order, and write each one.

        Each `do_*` flag only gates whether that stage is *eligible* to run;
        whether it actually runs still depends on what's already cached (or
        `force`, which redoes everything). The order matters because later
        stages consume earlier ones: notes and lyrics feed the form, and the
        form in turn triggers a monophonic re-transcription of each solo's
        lead stem (`_refine_lead_notes`). `fresh` and `form_fresh` track
        whether analysis/form were *just* computed this call (as opposed to
        loaded from cache) so those downstream stages know whether they need
        to redo their work too.

        `do_snippets` defaults to off: cutting a wav per part (times per-stem
        with `--stems`) used to happen on every analysis, and cost 90 MB on a
        four-minute song for files that are only ever a convenience - a
        browser or terminal can cut the same excerpt from a stem on demand
        for the cost of a `librosa.load` slice. `musiccopilot snippets` still
        writes them on request, for someone who actually wants files on disk
        (to drag a chorus into another tool, say); this only stops doing it
        automatically for everyone who does not.

        `backend` picks the note transcriber (`notes.BACKENDS`). Changing it is
        a cache miss for the stems it changes, so switching engine re-does the
        notes - and only the notes: stems, chords and lyrics are untouched.

        `do_voices` gates looking for more than one guitarist inside the guitar
        stem; `voice_count` overrides how many to find rather than letting
        `voices.py` decide.
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

        # Separation gives one `guitar` file however many guitarists played.
        # This looks inside it and, when there really is more than one, splits
        # it into `guitar` and `guitar-2` - which everything downstream then
        # treats as two instruments, because a suffixed stem already is one.
        # It runs *after* the notes because it is the notes it clusters (see
        # voices.py), and before the form because which stem leads a solo is
        # a different question once there are two guitars to choose between.
        if do_voices:
            retranscribed |= self.split_voices(count=voice_count, force=force, log=log)

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
            # opt-in only (see `run`'s docstring) - new part boundaries mean
            # any snippets already on disk are cut in the wrong places
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

        Every transcription is then checked against the audio it describes
        (`clean.py`) before it is cached, and a stem that turns out to be
        nothing but separation residue is cached as *no* notes rather than as
        a page of them. `notes_clean.json` records the revision of the checker
        that ran, and a mismatch there is a cache miss exactly as a backend
        mismatch is - otherwise a change to the checking would never reach a
        song that had already been analysed.
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

        todo = {s: src for s, src in sources.items()
                if force or s not in self.notes
                or self.note_backends.get(s) != nt.resolve_backend(backend, s)
                or self.notes_clean.get(s) != _shaping()}
        spectra = {s: cln.Spectrum.of(src) for s, src in todo.items()}
        # "Is this instrument in the song at all" is a question about the set
        # of stems, not about one of them: a stem holding only residue is
        # perfectly self-consistent. An imported multitrack is exempt - those
        # stems are the band's own tracks, and a quiet one is a quiet player.
        absent = set() if self.sources else cln.absent(spectra)

        changed: set[str] = set()
        for stem, src in todo.items():
            want = nt.resolve_backend(backend, stem)
            if stem in absent:
                log(f"• {stem}: separation residue, not an instrument — no notes")
                self.notes[stem] = []
            else:
                log(f"• transcribing notes: {stem} ({want})…")
                raw = nt.transcribe(src, stem, backend=want)
                self.notes[stem] = _shaped(raw, src, spectra[stem])
                if (gone := len(raw) - len(self.notes[stem])):
                    log(f"  ({gone} of {len(raw)} notes were not in the audio)")
            (self.work / "notes" / f"{stem}.json").write_text(
                json.dumps(nt.to_dicts(self.notes[stem])))
            self.note_backends[stem] = want
            self.notes_clean[stem] = _shaping()
            changed.add(stem)
        if changed:
            self._write("note_backends.json", self.note_backends)
            self._write("notes_clean.json", self.notes_clean)
        return changed

    # --- more than one player in a stem -------------------------------------
    def split_voices(self, stems: list[str] | None = None,
                     count: int | None = None, *, force: bool = False,
                     log=print) -> set[str]:
        """Split any stem that holds more than one player, and return what changed.

        Demucs emits one `guitar` whatever the band did, so a rhythm part, a
        lead and an acoustic arrive summed into a single file - and a single
        tab. `voices.split` clusters that stem's own notes into players and
        masks the audio to match; this writes the result into the cache as
        ordinary stems, because a suffixed stem already *is* a second
        instrument everywhere else in the pipeline (see CLAUDE.md, "Stem names
        are load-bearing"). Nothing downstream has to learn anything new.

        Three things it is careful about:

        - **An imported multitrack is never touched.** Its stems are the
          band's own tracks, one player each by construction; looking for a
          second guitarist inside one guitarist's DI take can only invent one.
        - **The finding is cached either way.** A stem it decided *not* to
          split is recorded in `voices.json` just as a split one is, so the
          question is asked once per stem rather than on every analysis.
        - **The chord track survives.** The parts are a partition of the file
          they came from, so `harmonic_bed` sums to exactly what it summed to
          before and `analysis.json` still describes this song. The *form*
          does not survive: which stem leads a solo is a different question
          once there are two guitars to choose between.
        """
        if self.sources:
            return set()                  # a real multitrack: one player a track
        want = tuple(stems) if stems else vc.SPLIT_STEMS

        # Which stems' cached notes are no longer a plain polyphonic read of
        # the whole stem: `_refine_lead_notes` splices a *monophonic*
        # transcription over each solo, which honestly reports far fewer notes
        # there. Clustering those would weight a solo at a tenth of its real
        # size and give a different answer depending only on whether the song
        # had been analysed before - this stem asked twice came back as two
        # guitarists once and three the next time. It is the same feedback
        # trap CLAUDE.md documents for lead detection, one stage along, and
        # the fix is the same: read the stem again rather than read back what
        # a later stage wrote. Read before the merge below, which drops the
        # form the answer is in.
        refined = {p.lead for p in self.form.parts if p.lead} if self.form else set()

        changed, merged = set(), set()
        if force:
            # `remember=False`: an undo asked for by hand is sticky, so that
            # the next analysis does not put the split straight back - but a
            # redo is that same undo followed immediately by a fresh look, and
            # a sticky record would make it skip the looking.
            for source in [s for s in self.voices if base_stem(s) in want]:
                if (back := self.merge_voices(source, remember=False, log=log)):
                    changed |= back
                    merged.add(source)

        seen = {part for split in self.voices.values() for part in split.parts}
        for source in sorted(s for s in self.stems
                             if base_stem(s) in want and s not in seen):
            if source not in merged and not self.notes.get(source):
                continue                  # nothing transcribed to cluster yet
            log(f"• looking for more than one player in {source}…")
            if source in refined or source in merged:
                # Merged stems have no notes at all any more (their old ones
                # were read off the masked audio); refined ones have notes
                # that are not a plain read of the whole stem. Both need one.
                log(f"  · reading {source} whole…")
                heard = _shaped(nt.transcribe(self.stems[source], source,
                                              backend=self.note_backends.get(source)),
                                self.stems[source])
                if source in merged:
                    # A merged stem has no cached notes left, so this read is
                    # the cache now. A *refined* stem's is emphatically not:
                    # writing it back would overwrite the monophonic solo
                    # splice with a polyphonic read and take every bend in the
                    # tab with it - silently, because a stem that turns out to
                    # hold one player changes nothing else that would notice.
                    self.notes[source] = heard
                    (self.work / "notes" / f"{source}.json").write_text(
                        json.dumps(nt.to_dicts(heard)))
                    self.note_backends.setdefault(
                        source, nt.resolve_backend(None, source))
                    self.notes_clean[source] = _shaping()
            else:
                heard = self.notes[source]
            # `parts` (each player's own audio, from the pitch-informed mask)
            # is deliberately not kept: it would have to be written lossy to
            # hold the line on storage, and two independent lossy encodes do
            # not sum back to the stem they came from, which is what made
            # `merge_voices` exact and let `_forget_form` leave `analysis.json`
            # standing. The notes are the deliverable regardless (CLAUDE.md,
            # "the audio follows the notes, and it is the weaker half") - a
            # split voice beyond the first has a tab but no isolated stem to
            # play back; `--minus-stem`/`--audio` fall back to the mix for it.
            split, _parts, groups = vc.split(self.stems[source], source,
                                             heard, count=count, log=log)
            self.voices[source] = split
            if not split.split:
                continue
            for voice, group in zip(split.voices, groups):
                self.notes[voice.stem] = group
                (self.work / "notes" / f"{voice.stem}.json").write_text(
                    json.dumps(nt.to_dicts(group)))
                self.note_backends[voice.stem] = self.note_backends.get(
                    source, nt.resolve_backend(None, voice.stem))
                # a player's notes are a subset of the source stem's, so they
                # have been through whatever check the source's went through
                self.notes_clean[voice.stem] = self.notes_clean.get(
                    source, _shaping())
            changed.update(split.parts)

        self._save_voices()
        if changed:
            self._write("note_backends.json", self.note_backends)
            self._write("notes_clean.json", self.notes_clean)
            self._forget_form(log)
        return changed

    def _save_voices(self) -> None:
        """Write `voices.json`, or remove it once nothing is recorded there."""
        if self.voices:
            self._write("voices.json", {n: sp.to_dict() for n, sp in self.voices.items()})
        else:
            (self.work / "voices.json").unlink(missing_ok=True)

    def merge_voices(self, source: str, *, remember: bool = True,
                     log=print) -> set[str]:
        """Drop a stem's extra players and go back to reading it as one.

        The undo half of `split_voices`. `source`'s own file was never
        touched by splitting it - a player beyond the first has no stem audio
        of its own (see `split_voices`), only notes read off a mask - so
        there is nothing to sum back together and no approximation to worry
        about. What actually needs undoing is the *notes*: they were read off
        the masked source, not off `source` whole, and mean nothing once the
        players are being treated as one again.

        What it leaves behind is a *one-player* record rather than no record,
        so the next analysis does not helpfully split the stem straight back
        apart again - unless `remember` is off, which is what a *redo* is.
        """
        split = self.voices.pop(source, None)
        if split is None or not split.split:
            if split is not None and remember:
                self.voices[source] = split   # already one player; leave the record
            return set()
        log(f"• putting {len(split.parts)} players back into {source}…")
        for name in split.parts:
            if name != source:
                (self.work / "stems" / f"{name}{audio.STEM_EXT}").unlink(missing_ok=True)
                (self.work / "stems" / f"{name}.wav").unlink(missing_ok=True)
                self.stems.pop(name, None)
            (self.work / "notes" / f"{name}.json").unlink(missing_ok=True)
            self.notes.pop(name, None)
            self.note_backends.pop(name, None)
        # A one-player record rather than no record, so the next analysis does
        # not helpfully split the stem straight back apart. Putting it back
        # together is a correction, and a correction the next run silently
        # reverses is not one. `remember=False` is the redo path, which wants
        # the stem looked at again rather than left alone.
        if remember:
            self.voices[source] = vc.Split(
                source, [vc.Voice(stem=source)], 0.0, "put back together by hand")
        self._save_voices()
        self._write("note_backends.json", self.note_backends)
        self._forget_form(log)
        return set(split.parts)

    def _forget_form(self, log=print) -> None:
        """Drop everything that was derived from which stem is which player.

        The form reads stem *names* - which one leads a solo - so it has to be
        worked out again once a guitar has become two, and the chart and the
        part snippets are cut from the form. The chord track deliberately
        stays: `split_voices` never rewrites the source stem's own file, only
        adds notes read off a mask of it, so the audio chords were detected
        over is unchanged (the same argument `daw.reassign` makes for a
        renumber).
        """
        self.form = None
        (self.work / "form.json").unlink(missing_ok=True)
        (self.work / "chart.md").unlink(missing_ok=True)
        shutil.rmtree(self.work / "snippets", ignore_errors=True)

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
