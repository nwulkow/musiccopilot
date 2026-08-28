"""Note transcription: audio stem -> list of Notes (and MIDI export)."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

from .config import NOTE_NAMES, PITCH_RANGE, SR, pitch_name


@dataclass
class Note:
    """One transcribed note; also the unit a tab cell or MIDI event is built from."""
    start: float          # seconds
    end: float
    pitch: int            # MIDI number
    velocity: float = 0.8         # 0..1
    technique: str = "normal"     # normal|bend|slide|hammer|pull|vibrato|palm_mute
    bend: float = 0.0             # semitones, for technique == "bend"

    @property
    def name(self) -> str:
        """Pitch as a note name, e.g. 'A4'."""
        return pitch_name(self.pitch)

    @property
    def duration(self) -> float:
        """Seconds, floored so a zero-length event still renders/plays as a note."""
        return max(0.03, self.end - self.start)


def _basic_pitch(path: Path, fmin: float, fmax: float) -> list[Note]:
    """Polyphonic transcription via Basic Pitch, restricted to [fmin, fmax] and
    re-sorted into chronological order (see the sort's own comment for why)."""
    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.inference import predict

    _, _, events = predict(
        str(path), ICASSP_2022_MODEL_PATH,
        onset_threshold=0.55, frame_threshold=0.3, minimum_note_length=70,
        minimum_frequency=fmin, maximum_frequency=fmax,
    )
    # basic-pitch emits events grouped by pitch, not in time order, and every
    # consumer here (summarise's span, _group's chord clustering, the tab grid)
    # assumes chronological notes.
    return sorted((Note(float(s), float(e), int(p), float(np.clip(a, 0.1, 1.0)))
                   for s, e, p, a, *_ in events), key=lambda n: (n.start, n.pitch))


def _pyin(path: Path, fmin: float, fmax: float) -> list[Note]:
    """Monophonic fallback: track f0, then slice it into stable note events."""
    import librosa

    y, sr = librosa.load(str(path), sr=SR, mono=True)
    f0, voiced, _ = librosa.pyin(y, fmin=fmin, fmax=fmax, sr=sr, frame_length=2048)
    times = librosa.times_like(f0, sr=sr)
    midi = np.where(voiced & np.isfinite(f0), librosa.hz_to_midi(np.nan_to_num(f0, nan=1.0)), np.nan)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]

    notes: list[Note] = []
    cur, start = None, 0.0
    for i, m in enumerate(midi):
        p = int(round(m)) if np.isfinite(m) else None
        if p != cur:
            if cur is not None and times[i] - start > 0.06:
                amp = float(np.clip(rms[min(i, len(rms) - 1)] * 8, 0.15, 1.0))
                notes.append(Note(float(start), float(times[i]), cur, amp))
            cur, start = p, times[i]
    return notes


# --- monophonic lead lines (guitar/bass solos, vocal melody) ----------------
#
# Basic Pitch is a *polyphonic* model: on a single melodic line it still looks
# for simultaneous notes, and on a quiet or noisy stem it hallucinates extra
# short ones (see form.py's density-based lead detection, which this directly
# feeds). A solo is overwhelmingly one string at a time, so a monophonic pitch
# tracker is the better-fitting model - and because it tracks a *continuous*
# f0 contour rather than quantising straight to the nearest semitone, it can
# tell a real bend/slide/vibrato from a new note instead of chopping the bend
# into a staircase of separate pitches.
_CREPE_HOP_S = 0.01          # 10ms frames
_CREPE_SR = 16000            # torchcrepe's native rate


_CREPE_MIN_PERIODICITY = 0.21   # below this the tracker is guessing at noise


def _torchcrepe_contour(path: Path, fmin: float, fmax: float):
    """Frame-rate (times, midi_pitch, periodicity) for a file on disk."""
    import librosa

    y, _ = librosa.load(str(path), sr=_CREPE_SR, mono=True)
    return _torchcrepe_contour_audio(y, _CREPE_SR, fmin, fmax)


def _torchcrepe_contour_audio(y: np.ndarray, sr: int, fmin: float, fmax: float):
    """Frame-rate (times, midi_pitch, periodicity); unvoiced frames are NaN.

    Both gates here are *relative* to the clip. A demucs stem is far quieter
    than a full mix (this solo peaks around -42 dBA and sits at -55), so the
    fixed thresholds torchcrepe ships - tuned for close-mic speech - throw
    away most of the playing: an absolute -50 dB silence gate alone cut 82% of
    the frames in the crystallize guitar solo, taking the whole sustain of
    every note with it. A note that is decaying is still a note.
    """
    import torch
    import torchcrepe
    import librosa

    if sr != _CREPE_SR:
        y = librosa.resample(np.asarray(y, dtype=np.float32), orig_sr=sr,
                             target_sr=_CREPE_SR)
    audio = torch.from_numpy(np.asarray(y, dtype=np.float32)).unsqueeze(0).float()
    hop = int(_CREPE_SR * _CREPE_HOP_S)
    # `weighted_argmax`, not torchcrepe's default Viterbi decoder. The model's
    # logits are bit-identical run to run, but the Viterbi path is not: where
    # two adjacent pitch bins are near-equally likely, float noise flips which
    # one wins, and a bin is ~0.25 semitones. That was enough to move notes
    # across the segmenter's thresholds, so transcribing the same solo twice
    # gave different note counts and different bends - the same take had to
    # read the same way twice before any of the tuning below meant anything.
    pitch, periodicity = torchcrepe.predict(
        audio, _CREPE_SR, hop_length=hop, fmin=max(fmin, 32.7), fmax=min(fmax, 2006.0),
        model="tiny", batch_size=2048, device="cpu", return_periodicity=True,
        decoder=torchcrepe.decode.weighted_argmax)

    periodicity = torchcrepe.filter.median(periodicity, 3)
    loud = torchcrepe.loudness.a_weighted(audio, _CREPE_SR, hop)[0].numpy()
    floor = float(np.percentile(loud, 5))          # this clip's own noise floor
    quiet = torch.from_numpy(loud <= max(floor, loud.max() - 35.0))
    periodicity[0][quiet] = 0.0

    pitch = torchcrepe.filter.mean(pitch, 3)
    midi = librosa.hz_to_midi(pitch[0].numpy())
    # Torch's CPU convolutions do not reduce in a fixed order, so the same clip
    # gives slightly different pitches from one *process* to the next, and the
    # segmenter's thresholds turn that into a different reading each time you
    # re-transcribe. Two things here contain it, and one known limit remains:
    #
    #  * the decoder choice above (Viterbi -> weighted_argmax) is the big one:
    #    it took the run-to-run pitch spread from 0.27 semitones - more than a
    #    quarter tone, enough to move notes across the segmenter's tests - down
    #    to a bounded ~0.1, and made the voiced/unvoiced mask exactly stable;
    # What survives: ~90% of notes reproduce exactly, and the rest are mostly
    # one 10ms frame of boundary jitter. The residue is upstream of this file -
    # torch gives different logits in a fresh process even single-threaded with
    # use_deterministic_algorithms(True) - so it cannot be fixed from here.
    #
    # Deliberately NOT smoothed further. A median filter over the contour does
    # damp the wobble, but it also flattens the approach to each note enough
    # that a sustained note reads as two or three: on this solo it chopped one
    # held D4 into three, and the extra onsets then pulled the fretting off the
    # string the phrase is actually played on. Stability bought by inventing
    # notes is not stability worth having.
    per = periodicity[0].numpy()
    midi[per < _CREPE_MIN_PERIODICITY] = np.nan
    times = np.arange(len(midi)) * _CREPE_HOP_S
    return times, midi, per


def _crepe_notes(path: Path, fmin: float, fmax: float) -> list[Note]:
    """Segment a file's pitch contour into notes. See `_segment_contour`."""
    return _segment_contour(*_torchcrepe_contour(path, fmin, fmax))


def _crepe_notes_from_audio(y: np.ndarray, sr: int, fmin: float,
                            fmax: float) -> list[Note]:
    """Same, for audio already in memory - the live recorder's entry point, so
    a take being played and a stem on disk go through identical segmentation."""
    return _segment_contour(*_torchcrepe_contour_audio(y, sr, fmin, fmax))


def _segment_contour(times, midi, periodicity) -> list[Note]:
    """Segment a continuous pitch contour into notes, keeping smooth pitch
    movement as a bend/slide/vibrato technique instead of new discrete notes.

    A new note starts when the contour is silent, jumps more than a semitone
    within one frame (a fret change, not a bend), or settles onto a new
    integer pitch for long enough to be a deliberate landing rather than the
    tail of a bend passing through it. Within a note, drift of a semitone or
    more from where it started is recorded as a bend target; fast (>=5Hz),
    small oscillation is vibrato instead.
    """
    n = len(times)
    if not n:
        return []
    voiced = np.isfinite(midi)
    gap_frames = int(round(0.06 / _CREPE_HOP_S))   # ride out a dropout this long
    hold_frames = int(round(0.05 / _CREPE_HOP_S))  # ... and this is a new landing
    _GLIDE_FRAMES = int(round(0.09 / _CREPE_HOP_S))  # look-back for a glide

    def next_voiced(k: int) -> int:
        """First index at or after k with a finite pitch, or n if none remain."""
        while k < n and not voiced[k]:
            k += 1
        return k

    notes: list[Note] = []
    i = next_voiced(0)
    while i < n:
        base = int(round(midi[i]))
        j = i
        while j + 1 < n:
            k = j + 1
            if not voiced[k]:
                # a short dropout inside a ringing note is the tracker losing
                # confidence, not the end of the note - only a longer silence
                # (or a different pitch on the far side) really ends it
                nxt = next_voiced(k)
                if nxt - k > gap_frames or nxt >= n:
                    break
                if abs(midi[nxt] - midi[j]) > 1.0:
                    break
                j = nxt
                continue
            if abs(midi[k] - midi[j]) > 1.4:           # fret/string change
                break
            # Settling onto a different semitone and *staying* there is the
            # next note; passing through one mid-bend is not. But a whole-step
            # bend also settles on its target and holds it, so "it stopped
            # somewhere new" cannot be the whole test - what separates them is
            # how the pitch got there. A new note is picked or fretted: it
            # arrives within a frame or two. A bend glides, and the frames in
            # between are spread across the semitones it passes through.
            if round(midi[k]) != base:
                hold = midi[k:k + hold_frames]
                if len(hold) == hold_frames and np.all(np.isfinite(hold)) \
                        and np.all(np.abs(hold - round(midi[k])) < 0.35):
                    approach = midi[max(i, k - _GLIDE_FRAMES):k + 1]
                    approach = approach[np.isfinite(approach)]
                    glided = (len(approach) >= 4
                              and float(np.std(approach)) > 0.18
                              and abs(midi[k] - base) <= 2.2)
                    if not glided:
                        break
            j += 1

        span = midi[i:j + 1]
        span = span[np.isfinite(span)]
        start = float(times[i])
        end = float(times[j] + _CREPE_HOP_S)
        nxt = next_voiced(j + 1)
        if nxt < n:            # sustain up to the next attack: guitars ring on
            end = min(float(times[nxt]), end + 0.35)
        if len(span) and end - start >= 0.045:
            seg_per = periodicity[i:j + 1]
            vel = float(np.clip(np.nanmean(seg_per) * 1.15, 0.15, 1.0))

            # The glide out of a note into the next one is legato, not part of
            # this note's own shape, so judge technique on the body only.
            body = span[: max(1, len(span) - int(round(0.04 / _CREPE_HOP_S)))]
            technique, bend = "normal", 0.0
            if len(body) >= 8:
                # oscillation around the mean, too fast to be a bend aimed at a
                # target pitch - that is vibrato
                centered = body - body.mean()
                crossings = int(np.sum(np.diff(np.sign(centered)) != 0))
                rate = crossings / 2 / max(end - start, 1e-6)
                if rate >= 4.0 and 0.12 < body.std() < 0.8:
                    technique = "vibrato"
            if technique == "normal" and len(body) >= 8:
                # A bend is pitch pushed *up* off the fretted note and held
                # there - the player pulls the string sharp. Downward drift is
                # a release or a slur into the next note, not a bend, and a
                # peak that is never held is just the attack sliding into pitch.
                rise = float(body.max() - body[:3].min())
                # How far the bend actually *arrives*, not how far it averages:
                # the top of the contour is where the string is held, and the
                # climb into it drags any mean or mid-percentile down. A whole
                # step read across the whole second half comes out around 0.75
                # and then rounds to a half-step bend, which is not a thing a
                # guitarist plays.
                # Where the bend is *held*, measured from where it started.
                # Not the peak (the push overshoots) and not the end (many
                # bends are released back down to the fretted note, which reads
                # as no bend at all). It is the pitch the string spends longest
                # sitting at above the start - so find the most-occupied level
                # in the part of the contour that is actually bent.
                # Measure from `base` - the pitch this note is *written* as -
                # not from wherever the contour happened to start. The tab
                # prints `fret + round(bend)`, so a bend measured off a
                # different origin can render as "7b7": a bend to the note you
                # are already on.
                top = float(np.percentile(body, 90))
                held = top - base
                if rise >= 0.7 and held >= 0.5:
                    # bends are fretted intervals: half step or whole step (and
                    # occasionally a step and a half), never 0.8 of one
                    semis = min((0.5, 1.0, 1.5, 2.0), key=lambda s: abs(s - held))
                    technique, bend = "bend", semis
            notes.append(Note(start, end, base, vel, technique, bend))
        i = nxt
    return notes


def transcribe(path: str | Path, instrument: str = "other",
               polyphonic: bool = True) -> list[Note]:
    """Transcribe one stem. Uses Basic Pitch if installed, else pYIN."""
    path = Path(path)
    fmin, fmax = PITCH_RANGE.get(instrument, PITCH_RANGE["other"])
    if polyphonic:
        try:
            return _basic_pitch(path, fmin, fmax)
        except Exception as exc:                   # noqa: BLE001 - optional dep
            print(f"[transcribe] basic-pitch unavailable ({exc}); pYIN for {instrument}")
    return _pyin(path, fmin, fmax)


def transcribe_lead(path: str | Path, instrument: str = "guitar") -> list[Note]:
    """Monophonic re-transcription for a single lead line (a solo, a riff).

    Prefers the CREPE-based tracker (continuous pitch -> real bends/slides
    instead of quantised note fragments); falls back to pYIN if torch/
    torchcrepe are not installed.
    """
    path = Path(path)
    fmin, fmax = PITCH_RANGE.get(instrument, PITCH_RANGE["other"])
    try:
        return _crepe_notes(path, fmin, fmax)
    except Exception as exc:                       # noqa: BLE001 - optional dep
        print(f"[transcribe] torchcrepe unavailable ({exc}); pYIN for {instrument}")
        return _pyin(path, fmin, fmax)


def in_window(notes: list[Note], start: float, end: float) -> list[Note]:
    """Notes overlapping [start, end) at all, including ones that only partly
    poke into the window."""
    return [n for n in notes if n.end > start and n.start < end]


def replace_window(notes: list[Note], start: float, end: float,
                   replacement: list[Note]) -> list[Note]:
    """Splice `replacement` into `notes` over [start, end), dropping whatever
    was there before. Used to swap a solo's polyphonic guess for a monophonic
    re-transcription without disturbing the rest of the stem's notes."""
    kept = [n for n in notes if n.end <= start or n.start >= end]
    return sorted(kept + replacement, key=lambda n: n.start)


def to_dicts(notes: list[Note]) -> list[dict]:
    """Serialize for the `notes/<stem>.json` cache."""
    return [asdict(n) for n in notes]


def from_dicts(rows: list[dict]) -> list[Note]:
    """Inverse of `to_dicts`; relies on the dataclass <-> JSON round-trip
    contract, so a required field added to `Note` breaks old caches."""
    return [Note(**r) for r in rows]


def write_midi(notes: list[Note], path: str | Path, tempo: float = 120.0,
               program: int = 30) -> Path:
    """Export notes as a MIDI file (program 30 = overdriven guitar, 33 = bass)."""
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(initial_tempo=tempo)
    inst = pretty_midi.Instrument(program=program)
    for n in notes:
        inst.notes.append(pretty_midi.Note(
            velocity=int(np.clip(n.velocity, 0.05, 1.0) * 127),
            pitch=int(n.pitch), start=float(n.start), end=float(max(n.end, n.start + 0.05))))
    pm.instruments.append(inst)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pm.write(str(path))
    return Path(path)


def summarise(notes: list[Note], top: int = 8) -> dict:
    """Quick stats used for reports and as context for the LLM."""
    if not notes:
        return {"count": 0}
    pitches = np.array([n.pitch for n in notes])
    hist = np.bincount(pitches % 12, minlength=12)
    order = np.argsort(hist)[::-1]
    return {
        "count": len(notes),
        "range": f"{pitch_name(int(pitches.min()))}-{pitch_name(int(pitches.max()))}",
        "notes_per_second": round(len(notes) / max(1e-6, notes[-1].end - notes[0].start), 2),
        "pitch_classes": [NOTE_NAMES[i] for i in order[:top] if hist[i] > 0],
    }
