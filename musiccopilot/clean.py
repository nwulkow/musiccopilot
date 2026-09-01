"""Checking a transcription against the audio it claims to describe.

Every note tracker in `notes.py` answers "what pitch is most likely here?" and
none of them ever answers "is anything here at all?". On a demucs stem that is
the wrong question to leave unasked, because separation always produces all six
files: a song with no piano still gets a `piano` stem, holding the residue of
whatever did not cancel, and Basic Pitch reads that residue as music. Measured
across the songs in this repo, a stem the band never played gets its notes from
50-60 dB below its own peak, while a stem someone actually played gets them
from about 10 dB below it. That gap is the whole of this module: a transcriber
looking only at pitch cannot see it, and a tab of the noise floor is worse than
no tab, because it looks exactly like a tab.

Three passes, in the order they have to run:

* `_merge_repeats` joins the fragments a polyphonic model makes of one held
  note. It is first because the other two measure a note's level over its own
  span, and a note chopped into three measures three attacks and no sustain.
* `_drop_quiet` is the silence gate above - a note whose own pitch band never
  rises far enough above the stem's loud level was read off the noise floor.
* `_drop_overtones` is the stray-high-note gate. A struck chord puts real
  energy an octave, a twelfth and two octaves above each note it contains, and
  a polyphonic model duly transcribes some of those as notes. They are the
  ones that end up as a lone `e15` over a low chord shape, and they cannot be
  found by level alone - a partial of a loud note is louder than the whole of
  a quiet one. What finds them is the *ratio*: measured on isolated notes in
  this repo's own material, a guitar's octave partial sits ~10 dB under its
  fundamental, its twelfth ~17 dB. A note no stronger than that above a note
  sounding underneath it has not been shown to exist separately.

The passes only ever remove notes, never invent or move one. That is a
deliberate limit: everything here is evidence that something is *not* there,
and nothing here is evidence about what should have been there instead.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from .config import CLEAN
from .notes import Note

# Bumped whenever a pass changes what it keeps. `pipeline.transcribe_notes`
# treats a stem whose cached notes were checked by an older revision as a cache
# miss, the same way it treats a stem read by a different backend - without
# that, an improvement here would only ever reach songs nobody had analysed yet.
REVISION = 1

# The CQT this module measures on. Three bins per semitone is enough to catch
# a note whose pitch sits a little off the grid (a bend, a detuned string)
# without letting a neighbouring semitone leak in; the 5.8ms hop resolves the
# attack of a note from the decay of the one before it.
_SR = 22050
_HOP = 256
_BINS_PER_SEMITONE = 3
_FMIN_MIDI = 24                  # C1 - below the lowest string of a 5-string bass
_N_BINS = 84 * _BINS_PER_SEMITONE


class Spectrum:
    """A stem's log-magnitude CQT, with the two questions this module asks it.

    Built once per stem and passed to every pass, because a CQT of a
    four-minute stem costs a couple of seconds and each pass would otherwise
    pay it again.
    """

    def __init__(self, mag_db: np.ndarray):
        self.db = mag_db
        # The stem's own loud level, not its peak: a single click would set a
        # peak, and every note in the stem would then be measured against it.
        self.ref = float(np.percentile(mag_db.max(axis=0), 99)) if mag_db.size else -200.0

    @classmethod
    def of(cls, path, sr: int | None = None) -> "Spectrum":
        """The CQT of an audio file, or of an array already in memory - which
        has to say what rate it is at, since nothing else here can tell."""
        import librosa

        if isinstance(path, np.ndarray):
            if sr is None:
                raise ValueError("Spectrum.of(array) needs the sample rate")
            y = np.asarray(path, dtype=np.float32)
            if y.ndim > 1:
                y = y.mean(axis=0)
            if sr != _SR:
                y = librosa.resample(y, orig_sr=sr, target_sr=_SR)
        else:
            y, _ = librosa.load(str(path), sr=_SR, mono=True)
        if not len(y):
            return cls(np.zeros((_N_BINS, 0)))
        c = np.abs(librosa.cqt(y, sr=_SR, hop_length=_HOP,
                               fmin=librosa.midi_to_hz(_FMIN_MIDI),
                               n_bins=_N_BINS, bins_per_octave=12 * _BINS_PER_SEMITONE))
        return cls(20.0 * np.log10(np.maximum(c, 1e-10)))

    @property
    def silent(self) -> bool:
        """Whether there is nothing here to check anything against."""
        return self.db.shape[1] == 0

    def level(self, pitch: int, start: float, end: float) -> float:
        """Loudest dB at `pitch` over [start, end).

        The loudest, not the mean: a note is present if its band ever rises,
        and a long note's own decay would otherwise argue it away.
        """
        b = int(round((pitch - _FMIN_MIDI) * _BINS_PER_SEMITONE))
        n = self.db.shape[1]
        if b < 0 or b >= self.db.shape[0] or not n:
            return -200.0
        i = min(max(int(np.floor(start * _SR / _HOP)), 0), n - 1)
        j = min(max(int(np.ceil(end * _SR / _HOP)), i + 1), n)
        return float(self.db[max(0, b - 1):b + 2, i:j].max())


def _head(note: Note) -> tuple[float, float]:
    """The window a note's level is measured over: its attack and the start of
    its sustain. A note held under a decaying chord fades into the same noise
    floor everything else is measured against, so judging it on its whole span
    would fail long notes for being long."""
    return note.start, min(note.end, note.start + CLEAN["measure_seconds"])


def _merge_repeats(notes: list[Note], spec: Spectrum) -> list[Note]:
    """Join same-pitch fragments the string was never struck twice for.

    A polyphonic model splits one held note wherever its own activation dips,
    so a ringing chord tone comes back as two or three notes butted end to
    end - 509 of crystallize's 1438 guitar notes are a fragment starting the
    instant another at the same pitch ended.

    Which of those were re-picked and which were one note cannot be settled
    from the note list: both look identical there. It can be settled from the
    audio, and only from the audio. **A struck string gets louder.** So the
    junction is merged when the pitch's own band does not rise across it, and
    left alone when it does - which keeps a driving eighth-note strum reading
    as eight notes rather than one long one.

    `merge_gap` still bounds how far apart the fragments may be: a gap longer
    than that is a rest, whatever the levels either side of it say.
    """
    gap, rise = CLEAN["merge_gap"], CLEAN["attack_db"]
    out: list[Note] = []
    last: dict[int, Note] = {}
    # Merging extends a note's end, so it works on copies: the caller's list
    # (`Song.notes[stem]`, usually) is not this function's to edit in place.
    for n in sorted((replace(n) for n in notes), key=lambda n: (n.start, n.pitch)):
        prev = last.get(n.pitch)
        if prev is not None and -0.02 <= n.start - prev.end <= gap \
                and _attack(spec, n.pitch, n.start) < rise:
            prev.end = max(prev.end, n.end)
            prev.velocity = max(prev.velocity, n.velocity)
            if prev.technique == "normal" and n.technique != "normal":
                prev.technique, prev.bend = n.technique, n.bend
            continue
        out.append(n)
        last[n.pitch] = n
    return out


def _attack(spec: Spectrum, pitch: int, t: float) -> float:
    """How far the pitch's own band rises across the moment `t`, in dB."""
    before = spec.level(pitch, max(0.0, t - 0.08), max(0.01, t - 0.01))
    return spec.level(pitch, t, t + 0.06) - before


def _drop_quiet(notes: list[Note], spec: Spectrum) -> list[Note]:
    """Drop notes whose own pitch band never rises out of the stem's floor.

    The threshold is relative to the stem's own loud level rather than
    absolute, so it means the same thing on a quiet acoustic take as on a
    loud one - and it is the measurement that tells a stem someone played
    from a stem separation invented.
    """
    floor = spec.ref + CLEAN["note_floor_db"]
    return [n for n in notes if spec.level(n.pitch, *_head(n)) >= floor]


def _drop_overtones(notes: list[Note], spec: Spectrum) -> list[Note]:
    """Drop notes that are no more than a partial of a note sounding under them.

    A candidate is only tested against notes already sounding at its onset and
    an overtone interval below it, and it survives unless its measured level
    is at or under what that note's own harmonic series predicts. Real
    doubling survives because a struck octave is not 10 dB down on its root;
    a partial that the tracker has named a note does not, because that is
    exactly the level it has.

    Notes are removed from the candidate pool as they fail, so a partial
    cannot go on to justify the partial above *it*.
    """
    rolloff = CLEAN["overtone_rolloff"]
    margin, window = CLEAN["overtone_margin_db"], CLEAN["chord_window"]
    order = sorted(notes, key=lambda n: (n.start, n.pitch))
    level = {id(n): spec.level(n.pitch, *_head(n)) for n in order}

    kept: list[Note] = []
    # What is still ringing. Kept separately from `kept` and pruned as the
    # scan moves forward: `order` is sorted by onset, so a note that has
    # already finished can never be underneath a later one again, and without
    # the pruning this is quadratic in the length of the stem.
    sounding: list[Note] = []
    for n in order:
        sounding = [q for q in sounding if q.end > n.start + 0.03]
        # Only what is already ringing when this note starts: a note is not a
        # partial of one that arrives after it.
        if any(level[id(n)] <= level[id(q)] + rolloff[n.pitch - q.pitch] + margin
               for q in sounding
               if q.start <= n.start + window and (n.pitch - q.pitch) in rolloff):
            continue
        kept.append(n)
        sounding.append(n)
    return kept


def clean(notes: list[Note], source, sr: int | None = None, *,
          spectrum: Spectrum | None = None) -> list[Note]:
    """Every pass, over one stem's notes. `source` is that stem's audio.

    Returns the notes unchanged if the audio cannot be read - a cleanup is an
    improvement to a transcription, never a precondition for having one.
    """
    if not notes:
        return notes
    try:
        spec = spectrum if spectrum is not None else Spectrum.of(source, sr)
    except Exception as exc:                       # noqa: BLE001 - optional dep
        print(f"[clean] skipped ({exc})")
        return notes
    if spec.silent:
        return notes
    return _drop_overtones(_drop_quiet(_merge_repeats(notes, spec), spec), spec)


def presence(spectra: dict[str, Spectrum]) -> dict[str, float]:
    """Each stem's loud level relative to the loudest stem in the song, in dB.

    Separation always writes all six files, so "is this instrument in the
    song at all" is a question about the *set* of stems and cannot be
    answered from one of them: a stem holding nothing but residue is perfectly
    self-consistent, and `_drop_quiet`'s own-floor threshold is measured
    against a floor that is all there is. Across this repo's material a stem
    nobody played sits 25 dB or more under the loudest one, and every stem
    somebody did play sits within 20.
    """
    refs = {s: sp.ref for s, sp in spectra.items() if not sp.silent}
    if not refs:
        return {}
    top = max(refs.values())
    return {s: r - top for s, r in refs.items()}


def absent(spectra: dict[str, Spectrum]) -> set[str]:
    """Stems that are separation residue rather than an instrument."""
    return {s for s, rel in presence(spectra).items() if rel < CLEAN["presence_db"]}
