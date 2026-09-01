"""Telling apart the several players inside one separated stem.

htdemucs_6s has exactly one `guitar` output, however many guitarists played.
A band with a rhythm part, a lead and an acoustic therefore gets all three
summed into one file - and everything downstream reads that file as one
player, so the tab is three guitarists' notes stacked on one fretboard, in
one hand position, with the acoustic's open strings interleaved between the
lead's bends. That is not a hard tab to read; it is an unreadable one.

**The split is driven by the notes, not by the audio.** The obvious approach -
decompose the stem with NMF and cluster the parts by how they sound - was
built first and measured against ground truth (two known tracks summed, split,
compared back). It reliably found *two* of something and just as reliably got
them wrong: it separated low notes from high ones, or the fundamentals of a
guitar from its own upper partials, and scored several dB *worse* than not
splitting at all. The reason is structural. An NMF basis has no known pitch,
so a timbre feature read off one is anchored on a guessed fundamental, and a
wrong guess turns a timbre measurement into a pitch measurement.

A transcribed note does not have that problem: its pitch is already known, so
the energy at each of its harmonics can be read at the frequencies those
harmonics are actually at. That single fact is what makes the timbre cue work,
and it is why this stage runs *after* transcription rather than before it. It
also means the split produces the per-player note lists directly - they are
the thing being clustered - so no stem is ever transcribed twice.

**The three cues.**

- **Timbre**: the energy at the first several harmonics of the note, relative
  to the note's own total. Bright and warm guitars differ here whatever they
  are playing, which is what keeps two players apart when they play the same
  riff in the same octave.
- **Pan**: the inter-channel level difference in those same harmonic bins. The
  strongest single cue whenever it exists at all - two rhythm guitars are
  almost always spread left and right - and demucs preserves the mix's stereo
  image, so it survives separation intact.
- **Register**, weighted low on purpose. An acoustic strumming under a lead
  really does sit lower, but one guitarist who plays a low riff and then a
  high solo would be split into two "players" if register led. It breaks ties;
  it does not decide.

**The audio follows the notes.** Once each note belongs to a player, the stem
is split by masking the harmonics of that player's notes - pitch-informed
separation, rather than the blind kind that failed above. The masks are
normalised against each other so they sum to one, which makes the split a
*partition*: `guitar + guitar-2` is sample-for-sample the file they came from.
Nothing that mixes stems back together (`audio.harmonic_bed`, chord detection,
`Song.backing`) can tell the difference, which is why splitting a stem does
not invalidate the chord track.

**And when it decides not to.** One guitar overdubbed onto itself, or a song
with no guitar at all (where demucs still emits a stem full of bleed), must
come back as one player - a spurious `guitar-2` costs a real tab and buys a
tab of noise. So a split has to clear three separate bars: the stem has to be
audible at all (`min_level`) and give enough notes to cluster (`min_notes`),
every player has to carry a real share of it (`min_share`), and every pair of
players has to be *tellable apart* by `_distinct`. Failing any of them returns
a single voice, and the caller leaves the file alone.

That last bar is the one that does the work, and it is deliberately not a
measure of how tidy the clustering was. Cluster separation cannot decide this:
measured across five single-instrument stems and four two-player mixes, the
silhouette ran 0.22-0.43 on *both*, so one guitar cut in half scores exactly as
convincingly as two guitars do. Any set of notes cuts into two tidy halves; the
question is only ever whether the halves differ in something two different
instruments can differ in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import texture as tex
from .config import SR, TEXTURE, VOICES, pitch_name
from .notes import Note

EPS = np.float32(1e-9)

#: Instruments worth looking inside. `bass` and `vocals` are deliberately not
#: here - a band has one bassist, and a backing vocal split out of the lead
#: would be a second stem nobody asked to sing.
SPLIT_STEMS: tuple[str, ...] = tuple(VOICES["stems"])

#: Harmonics are read as the peak of a few bins either side, because a real
#: string is a few cents off an exact multiple of its fundamental and a
#: single-bin read samples the skirt rather than the partial.
_HARM_BINS = 2
#: ... and written back with a soft edge, so a mask boundary does not land in
#: the middle of a partial and buzz.
_BUMP = np.array([0.25, 0.7, 1.0, 0.7, 0.25], dtype=np.float32)

#: A note's timbre is read from its body: after the attack, which is broadband
#: and belongs to nobody in particular, and not so far in that the next note
#: has started ringing over it.
_SKIP_ATTACK = 0.02
_BODY = 0.35


@dataclass
class Voice:
    """One player found inside a stem, and what it sounds like.

    Round-trips through `asdict`/`Voice(**row)` into `voices.json` like every
    other cached dataclass here, so every field has a default - adding one
    later must not make an existing cache raise on load.
    """

    stem: str = ""              # the stem name this voice is written as
    share: float = 1.0          # fraction of the stem's note energy
    notes: int = 0
    pan: float = 0.0            # -1 hard left .. +1 hard right
    brightness: float = 0.5     # 0 warm .. 1 bright, read off the harmonics
    low: int = 40               # MIDI, the register it plays in
    high: int = 64
    role: str = ""              # what it plays: chords, single notes, or both

    @property
    def placement(self) -> str:
        """Where it sits in the stereo image, in words."""
        if self.pan <= -0.16:
            return "panned left"
        if self.pan >= 0.16:
            return "panned right"
        return "centred"

    @property
    def tone(self) -> str:
        """What it sounds like, in words."""
        if self.brightness >= 0.60:
            return "bright"
        if self.brightness <= 0.40:
            return "warm"
        return "mid-toned"

    def describe(self) -> str:
        """One line for a CLI table, a job log or a chip in the browser.

        The role leads, because it is the thing a player recognises first:
        "the one playing chords" identifies a part in a way "the bright one
        panned left" does not.
        """
        role = f"{self.role}, " if self.role else ""
        return (f"{role}{self.tone}, {self.placement}, "
                f"{pitch_name(self.low)}–{pitch_name(self.high)}, "
                f"{self.notes} notes ({round(self.share * 100)}%)")


@dataclass
class Split:
    """The result of looking inside one stem: its players, and why that many."""

    source: str = ""
    voices: list[Voice] = field(default_factory=list)
    separation: float = 0.0     # cluster silhouette; 0 when it was not split
    reason: str = ""

    @property
    def parts(self) -> list[str]:
        """The stem names this source became - `[source]` when it was left alone."""
        return [v.stem for v in self.voices] or [self.source]

    @property
    def split(self) -> bool:
        """Whether this actually became more than one stem."""
        return len(self.voices) > 1

    def to_dict(self) -> dict:
        """JSON row for `voices.json`."""
        from dataclasses import asdict
        return asdict(self)


def from_dict(row: dict) -> Split:
    """Rebuild a `Split` from `voices.json`."""
    return Split(source=row.get("source", ""),
                 voices=[Voice(**v) for v in row.get("voices", [])],
                 separation=row.get("separation", 0.0),
                 reason=row.get("reason", ""))


# --- reading a note off the spectrogram --------------------------------------

def _stft(y: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """Per-channel complex STFT of a (2, N) signal, as (2, F, T) complex64."""
    import librosa

    return np.stack([librosa.stft(np.ascontiguousarray(ch, dtype=np.float32),
                                  n_fft=n_fft, hop_length=hop).astype(np.complex64)
                     for ch in y])


def _bins(pitch: int, n_h: int, n_fft: int, n_bins: int) -> list[tuple[int, int]]:
    """(index, FFT bin) for each of a note's first `n_h` harmonics.

    Harmonics whose neighbourhood would fall off either end of the spectrum
    are skipped rather than clamped - a partial read from half its bins is
    not a smaller partial, it is a wrong one. The index comes back with the
    bin because skipping must not shift the rest of the envelope along.
    """
    f0 = 440.0 * 2.0 ** ((pitch - 69) / 12.0)
    out = []
    for h in range(1, n_h + 1):
        b = int(round(h * f0 * n_fft / SR))
        if b + _HARM_BINS >= n_bins:
            break
        if b >= _HARM_BINS:
            out.append((h - 1, b))
    return out


def _read(mag: np.ndarray, notes: list[Note], n_h: int, n_fft: int, hop: int):
    """Per-note harmonic amplitudes, per channel: (2, len(notes), n_h).

    Read from the note's *body* rather than its whole length. The attack is
    broadband - a pick hitting a string sounds much the same on any guitar -
    and the tail of a long note is under whatever came next, so both ends
    describe the room rather than the player.
    """
    n_ch, n_bins, n_frames = mag.shape
    amp = np.zeros((n_ch, len(notes), n_h), dtype=np.float32)
    for i, note in enumerate(notes):
        t0 = int((note.start + _SKIP_ATTACK) * SR / hop)
        t1 = int(min(note.end, note.start + _SKIP_ATTACK + _BODY) * SR / hop) + 1
        t0 = max(0, min(t0, n_frames - 1))
        t1 = max(t0 + 1, min(t1, n_frames))
        for h, b in _bins(note.pitch, n_h, n_fft, n_bins):
            band = mag[:, b - _HARM_BINS:b + _HARM_BINS + 1, t0:t1]
            amp[:, i, h] = band.max(axis=1).mean(axis=1)
    return amp


def _brightness(env: np.ndarray) -> np.ndarray:
    """Where a harmonic envelope's centre of mass sits, mapped onto 0..1.

    The *harmonic-index* centroid rather than a frequency centroid, so a low
    warm note and a high warm note score the same - which is the whole reason
    for reading harmonics relative to the fundamental in the first place.
    Takes one envelope or a stack of them; the gate needs it per note and a
    `Voice` needs it once for the whole player.
    """
    idx = np.arange(1, env.shape[-1] + 1, dtype=np.float32)
    mean = (env * idx).sum(-1) / (env.sum(-1) + EPS)
    return np.clip((mean - 1.0) / (env.shape[-1] / 2.0), 0.0, 1.0)


def _detrend(env: np.ndarray, pitch: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """Take the pitch back out of the harmonic envelope.

    Reading harmonics relative to a note's own fundamental is *supposed* to
    make timbre pitch-invariant, and it is not enough on its own: measured on
    a single DI guitar take, the envelope still moved with register hard
    enough that clustering split that one guitarist into a "warm low player"
    and a "bright high player" more convincingly than it split two real
    guitars apart (brightness gap 0.39 against 0.10). Some of that is the
    instrument - a low string genuinely has more audible partials - and some
    is the transcription, which mistakes an octave more often down low.

    Either way the cure is the same: fit how the envelope varies with pitch
    across this whole stem and keep only what is left over. What survives is
    the part of a note's tone that its pitch does not explain, which is the
    part that identifies a player. The quadratic matters - the relationship
    bends, and a straight line leaves enough curvature behind to cluster on.
    """
    x = (pitch - 60.0) / 12.0
    basis = np.column_stack([np.ones_like(x), x, x ** 2])
    w = weight[:, None]
    coef, *_ = np.linalg.lstsq(basis.T @ (basis * w), basis.T @ (env * w), rcond=None)
    return env - basis @ coef


def _features(amp: np.ndarray,
              notes: list[Note]) -> tuple[np.ndarray, np.ndarray, dict]:
    """Cluster features per note, how much each note is worth, and the raw cues.

    The three cues are stacked into one vector and weighted there rather than
    being combined later, so `VOICES["timbre"]/["pan"]/["register"]` mean
    exactly what they look like: how far apart two notes are along that cue,
    in the units the clustering measures distance in.

    None of the three is rescaled to its own spread, and that is deliberate.
    Standardising would make a cue that carries *no* information - pan, on a
    stem where both guitars sit dead centre - as loud as one that carries all
    of it, and measured against ground truth that is exactly what went wrong:
    weighting pan fully on a centred mix dropped note accuracy from 84% to
    60%. Left in its natural units, a cue that is not there contributes
    nothing, which is what it should contribute.
    """
    total = amp.sum(axis=0)                            # (notes, harmonics)
    env = total / (total.sum(axis=1, keepdims=True) + EPS)
    side = amp[1].sum(axis=1) - amp[0].sum(axis=1)
    pan = side / (amp.sum(axis=(0, 2)) + EPS)
    pitch = np.array([n.pitch for n in notes], dtype=np.float32)

    dur = np.array([n.duration for n in notes], dtype=np.float32)
    weight = np.maximum(total.sum(axis=1) * np.sqrt(dur), 1e-6).astype(np.float64)

    X = np.column_stack([
        _detrend(env, pitch, weight) * float(VOICES["timbre"]),
        pan.astype(np.float32) * float(VOICES["pan"]),
        ((pitch - 40.0) / 24.0) * float(VOICES["register"]),
    ]).astype(np.float64)

    # The same cues in the units a person would describe them in - what
    # `_distinct` gates on, and what a `Voice` is described by. The clustering
    # reads the weighted vector; the decision reads these.
    cues = {"env": env, "pan": pan, "pitch": pitch, "tone": _brightness(env)}
    return X, weight, cues


# --- what actually gets clustered --------------------------------------------
#
# Not a note. Six strings and one pick is one player, so the notes of a strum
# were all played by whoever played any of them - and a cue read off a single
# note does not know that. Measured against ground truth (a known power-chord
# part and a known lead line, summed and split back apart), clustering notes
# one at a time cut the *rhythm player* in half and handed the two halves to
# two different stems, because the root, fifth and octave of a power chord
# differ in register and in everything register drags with it. The chord is
# the evidence that they do not differ in player.
#
# So a strum is one row: one feature vector, one weight, one label, and every
# note in it inherits that label. `texture.strums` is what decides which notes
# those are, and its whole job is to be conservative about it - a lead note
# welded into a rhythm chord is an error this can no longer undo.


def _events(notes: list[Note], X: np.ndarray, weight: np.ndarray,
            cues: dict) -> tuple[list[np.ndarray], np.ndarray, np.ndarray, dict]:
    """Collapse the per-note view onto one row per strum.

    Every cue becomes the energy-weighted mean over the strum's notes, and its
    weight their sum, so a six-string chord counts for what it is worth rather
    than six times over. `texture` joins them as a cue in its own right: how
    chordal the event is, which is the one thing a per-note view could never
    say and the thing that tells a rhythm part from a lead line.
    """
    groups = tex.strums(notes)
    where = {id(n): i for i, n in enumerate(notes)}
    idx = [np.array([where[id(n)] for n in g]) for g in groups]
    w = np.array([float(weight[i].sum()) for i in idx])

    def mean(v: np.ndarray) -> np.ndarray:
        return np.array([float((v[i] * weight[i]).sum() / weight[i].sum()) for i in idx])

    texture = np.array([min(1.0, (len(g) - 1) / 2.0) for g in groups], dtype=np.float32)
    EX = np.stack([(X[i] * weight[i][:, None]).sum(axis=0) / weight[i].sum()
                   for i in idx])
    EX = np.column_stack([EX, texture * float(VOICES["texture"])]).astype(np.float64)
    ecues = {"pan": mean(cues["pan"]), "tone": mean(cues["tone"]),
             "pitch": mean(cues["pitch"]), "texture": texture,
             "start": np.array([min(n.start for n in g) for g in groups]),
             "end": np.array([max(n.end for n in g) for g in groups])}
    # A property of the stem rather than of any one pair of clusters, so it is
    # measured once, before there are any clusters to be misled by.
    ecues["interleaved"] = _interleaved(ecues, w)
    return idx, EX, w, ecues


# --- deciding how many players there are -------------------------------------

def _cluster(X: np.ndarray, weight: np.ndarray, k: int) -> np.ndarray:
    """K-means over the note features, weighted by what each note is worth."""
    from sklearn.cluster import KMeans

    return KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(
        X, sample_weight=weight)


def _gap(values: np.ndarray, labels: np.ndarray, weight: np.ndarray,
         a: int, b: int, *, means: bool = False):
    """How far apart two clusters are along one cue.

    Both halves are needed. The raw difference says whether anyone could hear
    it; the same difference in units of the spread *within* the two clusters
    says whether it is a property of the players or of the notes they happen
    to have played.

    `means=True` adds the two cluster means themselves, for a cue where the
    difference is not the whole question - texture has to say which side is
    which, not only that they differ.
    """
    ia, ib = labels == a, labels == b
    va, vb, wa, wb = values[ia], values[ib], weight[ia], weight[ib]
    ma = float((va * wa).sum() / wa.sum())
    mb = float((vb * wb).sum() / wb.sum())
    delta = abs(ma - mb)
    spread = float(np.sqrt(0.5 * (va.var() + vb.var()))) + 1e-6
    return (delta, delta / spread, (ma, mb)) if means else (delta, delta / spread)


def _distinct(cues: dict, labels: np.ndarray, weight: np.ndarray,
              a: int, b: int) -> str | None:
    """Why these two clusters are two players - or None if they are one.

    This is the whole judgement, and it deliberately does not ask how tidy the
    clustering was. Any set of notes cuts into two tidy halves; the question is
    whether the halves differ in something only two different instruments can
    differ in.

    **Placement** is the cue that carries it. Two guitars are nearly always
    spread across the stereo image, demucs preserves that image, and a note's
    pan does not depend on which note it is - so a real gap here is close to
    proof, and it is where the accuracy is (90% of notes placed right when the
    players are panned; 72% when they are not).

    **Tone** is the fallback for a mono or dead-centre mix, and it needs the
    extra clause. A single guitar's harmonic envelope still drifts with
    register after `_detrend` has taken the fitted part of that out, so a
    bright/warm gap on its own is not enough: on one real DI take the two
    halves of the *same* guitarist were further apart in tone (0.37) than two
    genuinely different guitars were (0.20). What told them apart was that the
    DI take's halves were further apart in *pitch* than in tone, and the two
    real guitars' were not. So tone has to beat pitch at explaining the split
    before it is believed.
    """
    d_pan, z_pan = _gap(cues["pan"], labels, weight, a, b)
    d_tone, z_tone = _gap(cues["tone"], labels, weight, a, b)
    d_tex, _, m_tex = _gap(cues["texture"], labels, weight, a, b, means=True)
    _, z_pitch = _gap(cues["pitch"], labels, weight, a, b)
    if d_pan >= VOICES["min_pan_gap"] and z_pan >= VOICES["min_gap_z"]:
        return f"{d_pan:.2f} apart in the stereo image"
    # Texture has to mean what the sentence says: one of them really is
    # playing chords and the other really is playing single notes. A *gap* is
    # not enough and testing one was the mistake worth recording - a rhythm
    # part whose chords the tracker caught whole half the time and one note of
    # the other half splits into "all chords" and "some chords", which are
    # 0.57 apart on this scale and are the same guitarist. Measured on ground
    # truth, gating on the gap alone claimed a third player in all three test
    # mixes and cost 16 points of note accuracy in the panned one (91.9 ->
    # 75.6); requiring both ends puts every one of them back at two players.
    chordal, single = max(m_tex), min(m_tex)
    if chordal >= TEXTURE["rhythm_at"] and single <= TEXTURE["lead_at"]:
        if cues["interleaved"] >= VOICES["min_interleave"]:
            return (f"one plays chords where the other plays single notes "
                    f"({d_tex:.2f} apart), both sounding throughout")
    if d_tone >= VOICES["min_tone_gap"] and z_tone > z_pitch:
        return f"{d_tone:.2f} apart in tone"
    return None


def _interleaved(cues: dict, weight: np.ndarray, grid: float = 2.0) -> float:
    """How reliably this stem holds chords and single notes *at the same time*.

    This is what makes the texture cue safe, and without it the cue could not
    be used at all: one guitarist who strums the verses and plays the solo
    differs from *himself* in texture by more than two players usually do, so
    a texture gap on its own splits every song with a solo in it into a
    "rhythm player" and a "lead player" who are the same person. Two people
    differ from one person doing two things in exactly one observable way -
    they can play at the same time.

    **It reads the notes, not the clusters.** The obvious version asks how
    much the two *clusters* overlap in time, and it is circular: the
    clustering is what is on trial, and when it is wrong it is wrong by
    scattering each player's notes through the other's half, which makes both
    look busy everywhere. On ground truth - one rig, one pan, eight bars of
    chords then eight of solo, not one bar of overlap - the cluster version
    scored 73% "at once" and let the split through.

    **And it reads the quiet end of the distribution, not the average.** Both
    textures are present *somewhere* in almost any stem, because a strum whose
    chord the tracker only half caught reads as single notes; in that same
    one-guitarist stem the single-note texture holds a quarter of the chord
    half. What one player cannot do is sound like both *throughout*: their
    solo half has no chords in it at all. So the measure is the 10th
    percentile of the smaller of the two shares across two-second windows -
    "in nine windows out of ten, both a chord part and a single-note part are
    audibly present" - and a window counts a texture only where it carries a
    real share of it, so a few stray notes cannot stand in for a part.

    Measured: 0.20, 0.25 and 0.34 for the three two-player mixes, and 0.00 for
    both one-guitarist mixes, for a real lead DI track, for a real rhythm DI
    track's own stem and for crystallize's guitar. The one real single track
    that scores at all is a rhythm take at 0.15, which is why the threshold
    sits at 0.18 rather than lower: like the tone-only path this is the
    marginal one, and it only ever *guards* the texture clause - pan still
    decides every mix that has any.
    """
    chordal = cues["texture"] >= TEXTURE["rhythm_at"]
    single = cues["texture"] <= TEXTURE["lead_at"]
    if not chordal.any() or not single.any():
        return 0.0
    t0, t1 = float(cues["start"].min()), float(cues["end"].max())
    n = max(1, int(np.ceil((t1 - t0) / grid)))
    energy = np.zeros((2, n), dtype=float)
    for row, which in enumerate((chordal, single)):
        for start, end, w in zip(cues["start"][which], cues["end"][which],
                                 weight[which]):
            i = max(0, min(int((start - t0) / grid), n - 1))
            energy[row, i:min(n, int((end - t0) / grid) + 1)] += w
    total = energy.sum(axis=0)
    playing = total > 0
    if playing.sum() < 2:
        return 0.0
    share = energy[:, playing] / total[playing]
    return float(np.percentile(share.min(axis=0), 10))


def _separation(X: np.ndarray, labels: np.ndarray) -> float:
    """How far apart the clusters actually are (silhouette), or 0 if undefined."""
    from sklearn.metrics import silhouette_score

    if len(set(labels.tolist())) < 2:
        return 0.0
    try:
        return float(silhouette_score(X, labels, sample_size=min(len(X), 2000),
                                      random_state=0))
    except ValueError:
        return 0.0


def _choose(X: np.ndarray, weight: np.ndarray, cues: dict, max_k: int,
            count: int | None) -> tuple[np.ndarray, float, str]:
    """Pick how many players are in the stem, and which note belongs to which.

    With an explicit `count` the only question left is the assignment. Without
    one, each k up to `max_k` has to earn itself twice over: every cluster has
    to carry `min_share` of the stem, and every *pair* of clusters has to be
    tellable apart by `_distinct`. The largest k that manages both wins, so a
    third player is only claimed when it stands apart from both of the others
    rather than merely from the pair of them averaged together.

    **The partition matters as much as the gates, and only one is offered.**
    Clustering each k twice - once with the texture cue and once on the
    instrument cues alone - and letting `_distinct` judge both was built and
    measured, on the theory that texture crowds pan out of the partition (it
    does) and that the gates would sort it out (they do not). It took the
    calibration set from five of six right to three: the pan-only proposal
    re-admits exactly the over-splits the texture axis was suppressing, one
    guitarist cut in half along a pan gap that is really the other player
    bleeding into their notes. `_distinct` cannot tell that from a second
    guitarist, so what keeps it out is never being asked - the partition is
    part of the evidence, not just a starting point for it.

    Falling through to one player is the normal outcome, not a failure: most
    guitar stems hold one guitarist, and a spurious `guitar-2` costs a real
    tab and buys a tab of leakage.
    """
    if count and count > 1:
        labels = _cluster(X, weight, min(count, len(X)))
        return labels, _separation(X, labels), f"asked for {count}"

    best = (np.zeros(len(X), dtype=int), 0.0,
            "nothing in the stem stands apart as a second player")
    for k in range(2, min(max_k, len(X) - 1) + 1):
        labels = _cluster(X, weight, k)
        groups = sorted(set(labels.tolist()))
        if len(groups) < k:
            continue
        shares = np.array([weight[labels == g].sum() for g in groups])
        if shares.min() < VOICES["min_share"] * weight.sum():
            continue                       # one of these is not a player
        why = [_distinct(cues, labels, weight, a, b)
               for i, a in enumerate(groups) for b in groups[i + 1:]]
        if any(w is None for w in why):
            continue                       # two of them are the same player
        best = (labels, _separation(X, labels), "; ".join(sorted(set(why))))
    return best


# --- turning the assignment back into audio ----------------------------------

def _comb(shape: tuple[int, int], notes: list[Note], level: np.ndarray,
          idx: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """Where one player's notes put energy: a harmonic comb over the spectrogram.

    Each note contributes a bump at each of its harmonics for as long as it
    sounds, scaled by how loud that harmonic actually measured - so the comb
    is shaped like the player rather than like an idealised sawtooth, and a
    warm guitar does not claim the bright one's upper partials.
    """
    n_bins, n_frames = shape
    comb = np.zeros(shape, dtype=np.float32)
    for i in idx:
        note = notes[i]
        t0 = max(0, min(int(note.start * SR / hop), n_frames - 1))
        t1 = max(t0 + 1, min(int(note.end * SR / hop) + 1, n_frames))
        for h, b in _bins(note.pitch, level.shape[1], n_fft, n_bins):
            if level[i, h] <= 0.0:
                continue
            comb[b - _HARM_BINS:b + _HARM_BINS + 1, t0:t1] += (
                float(level[i, h]) * _BUMP[:, None])
    return comb


def _masks(combs: list[np.ndarray]) -> list[np.ndarray]:
    """Normalise the combs against each other into masks that sum to one.

    Two things have to be handled or the parts stop summing back to the stem.
    Bins that no note explains - pick noise, amp hiss, the reverb between
    phrases - are shared out by how busy each player is in *that frame*, so a
    passage only one of them is playing does not hand half its room sound to
    someone who is silent. And a frame where nobody is playing at all is split
    by their overall share, because there is nothing better to go on and the
    energy still has to land somewhere.
    """
    total = np.zeros_like(combs[0])
    for c in combs:
        total += c
    per_frame = np.stack([c.sum(axis=0) for c in combs])          # (voices, T)
    frame_total = per_frame.sum(axis=0)
    overall = per_frame.sum(axis=1)
    overall = overall / (overall.sum() + EPS)
    share = np.where(frame_total > EPS, per_frame / (frame_total + EPS),
                     overall[:, None]).astype(np.float32)

    voiced = total > EPS
    return [np.where(voiced, c / (total + EPS), share[v][None, :]).astype(np.float32)
            for v, c in enumerate(combs)]


# --- the split itself --------------------------------------------------------

def split(path: str | Path, stem: str, notes: list[Note], *,
          count: int | None = None, max_voices: int | None = None,
          log=print) -> tuple[Split, list[np.ndarray], list[list[Note]]]:
    """Look inside one stem and return its players, their audio and their notes.

    Audio comes back as (2, N) arrays and notes as lists, both in the same
    order as `Split.voices` - loudest first, so the main guitar keeps the
    unsuffixed stem name it already had and every existing link to it still
    means roughly what it did. A single voice means "leave this stem alone":
    its audio is the input unchanged and its notes are the input list, so a
    caller that writes everything back unconditionally writes exactly what it
    read.
    """
    from . import audio as au

    path = Path(path)
    y = au.load(path, SR, mono=False)
    if y.ndim == 1:                       # a mono stem has no pan cue, but the
        y = np.stack([y, y])              # timbre and register ones still work
    y = np.asarray(y, dtype=np.float32)

    def alone(reason: str) -> tuple[Split, list[np.ndarray], list[list[Note]]]:
        """One player: hand back what was read, with the reason it was not split."""
        log(f"  · one player in {stem} ({reason})")
        return Split(stem, [Voice(stem=stem, notes=len(notes))], 0.0, reason), [y], [notes]

    level = float(np.sqrt(np.mean(y ** 2)))
    if level < VOICES["min_level"]:
        return alone(f"the stem is near-silent (rms {level:.4f})")
    if len(notes) < VOICES["min_notes"]:
        return alone(f"only {len(notes)} notes to go on")

    n_fft, hop, n_h = int(VOICES["n_fft"]), int(VOICES["hop"]), int(VOICES["harmonics"])
    log(f"  · reading {len(notes)} notes off {stem}…")
    S = _stft(y, n_fft, hop)
    mag = np.abs(S)
    amp = _read(mag, notes, n_h, n_fft, hop)
    del mag

    X, weight, cues = _features(amp, notes)
    # One row per strum, not per note: what a chord proves is that its notes
    # share a player, and that is exactly what a per-note clustering throws
    # away. `chordness` rides along as a cue of its own so a rhythm part and a
    # lead line can be told apart by what they *are*, not only by how they
    # sound and where they sit.
    idx, EX, ew, ecues = _events(notes, X, weight, cues)
    if len(EX) < VOICES["min_events"]:
        return alone(f"only {len(EX)} events to go on")
    labels, sep, reason = _choose(EX, ew, ecues, int(max_voices or VOICES["max"]),
                                  count)
    groups = sorted(set(labels.tolist()), key=lambda g: -float(ew[labels == g].sum()))
    if len(groups) < 2:
        return alone(reason)
    # Back down to notes: every note of a strum takes the strum's player.
    note_labels = np.zeros(len(notes), dtype=int)
    for i, g in zip(idx, labels):
        note_labels[i] = g
    labels = note_labels

    log(f"  · {len(groups)} players in {stem} ({reason})")
    harm, env, pan = amp.sum(axis=0), cues["env"], cues["pan"]
    chord = tex.chordness(notes)

    import librosa
    voices, parts, groups_notes, combs = [], [], [], []
    for rank, g in enumerate(groups, 1):
        mine = np.flatnonzero(labels == g)
        w = weight[mine]
        pitches = [notes[i].pitch for i in mine]
        voices.append(Voice(
            stem=stem if rank == 1 else f"{stem}-{rank}",
            share=round(float(w.sum() / weight.sum()), 4),
            notes=len(mine),
            pan=round(float((pan[mine] * w).sum() / w.sum()), 3),
            brightness=round(float(_brightness(
                (env[mine] * w[:, None]).sum(axis=0) / w.sum())), 3),
            low=int(np.percentile(pitches, 2)), high=int(np.percentile(pitches, 98)),
            role=tex.role(chord[mine], w)))
        groups_notes.append([notes[i] for i in mine])
        combs.append(_comb(S.shape[1:], notes, harm, mine, n_fft, hop))
        log(f"    {voices[-1].stem}: {voices[-1].describe()}")

    for mask in _masks(combs):
        parts.append(np.stack([librosa.istft(S[c] * mask, hop_length=hop, n_fft=n_fft,
                                             length=y.shape[1])
                               for c in (0, 1)]).astype(np.float32))
    return Split(stem, voices, sep, reason), parts, groups_notes
