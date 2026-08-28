"""Musical analysis: tempo, beats, key, chord track, song structure."""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

import librosa
import numpy as np

from .config import (CHORD_QUALITIES, NOTE_NAMES, QUALITY_BIAS,
                     QUALITY_SUFFIX, SR)

# Krumhansl-Kessler key profiles (form.py reuses them for per-part keys).
KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


@dataclass
class Chord:
    """One chord-vocabulary match spanning a run of beats; see `detect_chords`."""
    start: float
    end: float
    name: str
    root: int          # pitch class, -1 for "no chord"
    quality: str


@dataclass
class Section:
    """One segment of `detect_structure`'s song map."""
    start: float
    end: float
    label: str         # A, B, C ... = musically similar segments
    kind: str          # "vocal" or "instrumental"
    energy: float


@dataclass
class Analysis:
    """Top-level result of `analyze`: everything `analysis.json` caches."""
    duration: float
    tempo: float
    beat_times: list[float]
    downbeats: list[float]
    beats_per_bar: int
    key: str
    chords: list[Chord] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-ready dict; `asdict` recurses into the nested Chord/Section lists."""
        return asdict(self)          # asdict recurses into Chord / Section

    @classmethod
    def from_dict(cls, d: dict) -> "Analysis":
        """Inverse of `to_dict` - rebuilds the nested Chord/Section dataclasses."""
        d = dict(d)
        d["chords"] = [Chord(**c) for c in d["chords"]]
        d["sections"] = [Section(**s) for s in d["sections"]]
        return cls(**d)

    def chord_at(self, t: float) -> Chord | None:
        """The chord sounding at time `t`, or None outside the chord track."""
        return next((c for c in self.chords if c.start <= t < c.end), None)

    def progression(self, start: float, end: float) -> list[str]:
        """Chord names overlapping a time window, consecutive repeats collapsed."""
        names, out = [c.name for c in self.chords if c.end > start and c.start < end], []
        for n in names:
            if not out or out[-1] != n:
                out.append(n)
        return out


# --- chord recognition -------------------------------------------------------

def _chord_templates() -> tuple[np.ndarray, np.ndarray, list[tuple[int, str]]]:
    """One unit-norm chroma template per (root, quality), with its score bias."""
    templates, bias, labels = [], [], []
    for quality, intervals in CHORD_QUALITIES.items():
        for root in range(12):
            v = np.zeros(12)
            for i in intervals:
                v[(root + i) % 12] = 1.0
            templates.append(v / np.linalg.norm(v))
            bias.append(QUALITY_BIAS[quality])
            labels.append((root, quality))
    return np.array(templates), np.array(bias), labels


def chord_name(root: int, quality: str) -> str:
    """Root + quality suffix (e.g. "Am7"), or "N.C." for root -1 ("no chord")."""
    if root < 0:
        return "N.C."
    return NOTE_NAMES[root] + QUALITY_SUFFIX[quality]


def _beat_bounds(beat_times: np.ndarray, sr: int, n_frames: int) -> np.ndarray:
    """Frame indices delimiting each beat, always starting at 0 and ending at
    n_frames - so `sync` returns exactly len(bounds) - 1 columns."""
    f = librosa.time_to_frames(np.asarray(beat_times), sr=sr)
    return np.unique(np.clip(np.concatenate([[0], f, [n_frames]]), 0, n_frames))


def detect_chords(y: np.ndarray, sr: int, beat_times: np.ndarray,
                  self_prob: float = 0.85, sharpness: float = 25.0,
                  nc_score: float = 0.72, nc_drop: float = 0.25) -> list[Chord]:
    """Beat-synchronous template matching, smoothed with Viterbi decoding.

    `self_prob` is the chance a chord holds from one beat to the next (raise it
    for slow ballads). `sharpness` converts match scores into probabilities -
    lower it for heavier smoothing. `nc_score` is the score a chord has to beat
    to be preferred over "no chord"; it is a constant rather than a flat
    template, which would otherwise match every beat reasonably well and win.

    That constant is scaled down by `nc_drop` on loud beats: "no chord" should
    mean nothing is sounding, not that the match was mediocre. Left flat it also
    fights the Viterbi prior - one weak beat enters the N.C. state and `self_prob`
    keeps it there for bars at a time.
    """
    y_harm = librosa.effects.harmonic(y, margin=3.0)
    chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr, bins_per_octave=36)
    bounds = _beat_bounds(beat_times, sr, chroma.shape[1])
    sync = librosa.util.normalize(
        librosa.util.sync(chroma, bounds, aggregate=np.median), norm=2, axis=0)

    rms = librosa.feature.rms(y=y)[0]      # same 512 hop as the chroma
    if len(rms) < bounds[-1]:
        rms = np.pad(rms, (0, int(bounds[-1]) - len(rms)), mode="edge")
    loud = librosa.util.sync(rms[None, :bounds[-1]], bounds, aggregate=np.median)[0]
    loud = np.clip(loud / (np.percentile(loud, 95) + 1e-9), 0.0, 1.0)

    templates, bias, labels = _chord_templates()
    scores = templates @ sync + bias[:, None]      # (n_chords, n_beats)
    scores = np.vstack([scores, nc_score - nc_drop * loud])
    labels = labels + [(-1, "N")]

    probs = np.exp((scores - scores.max(axis=0)) * sharpness)
    probs /= probs.sum(axis=0, keepdims=True)
    transition = librosa.sequence.transition_loop(len(labels), self_prob)
    path = librosa.sequence.viterbi_discriminative(probs, transition)

    edges = librosa.frames_to_time(bounds, sr=sr)      # len == n_beats + 1
    chords: list[Chord] = []
    for i, state in enumerate(path):
        root, quality = labels[state]
        name = chord_name(root, quality)
        if chords and chords[-1].name == name:     # merge repeats
            chords[-1].end = float(edges[i + 1])
        else:
            chords.append(Chord(float(edges[i]), float(edges[i + 1]), name, root, quality))
    return chords


# --- key, beats, structure ---------------------------------------------------

def detect_key(y: np.ndarray, sr: int, chords: list[Chord] | None = None) -> str:
    """Krumhansl-Kessler profile correlation.

    A major key and its relative minor share every note, so the profiles alone
    cannot separate them; when a chord track is available the most-played chord
    root breaks the tie.
    """
    chroma = librosa.feature.chroma_cqt(y=librosa.effects.harmonic(y), sr=sr).mean(axis=1)
    chroma = (chroma - chroma.mean()) / (chroma.std() + 1e-9)

    scores = {}
    for root in range(12):
        for profile, mode in ((KK_MAJOR, "major"), (KK_MINOR, "minor")):
            p = np.roll(profile, root)
            scores[(root, mode)] = np.corrcoef(chroma, (p - p.mean()) / p.std())[0, 1]

    (root, mode), best = max(scores.items(), key=lambda kv: kv[1])
    rel = ((root - 3) % 12, "minor") if mode == "major" else ((root + 3) % 12, "major")

    played = [c for c in chords or [] if c.root >= 0]
    if played and abs(best - scores[rel]) < 0.25:
        weight: dict[int, float] = {}
        for c in played:
            weight[c.root] = weight.get(c.root, 0.0) + (c.end - c.start)
        # music establishes and resolves on the tonic, so the opening and
        # closing chords are the strongest evidence of which of the pair it is
        span = played[-1].end - played[0].start
        for c in (played[0], played[-1]):
            weight[c.root] = weight.get(c.root, 0.0) + 0.25 * span
        if weight.get(rel[0], 0.0) > weight.get(root, 0.0):
            root, mode = rel
    return f"{NOTE_NAMES[root]} {mode}"


def _downbeats(onset_env: np.ndarray, beats: np.ndarray, beat_times: np.ndarray,
               beats_per_bar: int = 4) -> list[float]:
    """Pick the bar phase whose beats carry the most onset energy."""
    strength = onset_env[np.clip(beats, 0, len(onset_env) - 1)]
    phase = max(range(beats_per_bar),
                key=lambda p: strength[p::beats_per_bar].sum())
    return [float(t) for t in beat_times[phase::beats_per_bar]]


def detect_structure(y: np.ndarray, sr: int, beats: np.ndarray,
                     vocals: np.ndarray | None = None,
                     n_segments: int | None = None,
                     min_seconds: float = 8.0) -> list[Section]:
    """Segment the song and letter-label musically similar parts.

    Roughly one segment per 20s, then anything shorter than `min_seconds` is
    merged away so the map stays readable.
    """
    from sklearn.cluster import KMeans

    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    bounds = _beat_bounds(librosa.frames_to_time(beats, sr=sr), sr,
                          min(mfcc.shape[1], chroma.shape[1]))
    feat = np.vstack([librosa.util.sync(mfcc, bounds), librosa.util.sync(chroma, bounds)])
    feat = librosa.util.normalize(feat, axis=0)

    if n_segments is None:
        n_segments = int(np.clip(round(len(y) / sr / 20), 3, 12))
    n_segments = int(min(n_segments, max(2, feat.shape[1] // 8)))
    seg = librosa.segment.agglomerative(feat, n_segments)
    seg = np.unique(np.concatenate([[0], seg, [feat.shape[1]]]))
    seg_feat = np.array([feat[:, a:b].mean(axis=1) for a, b in zip(seg[:-1], seg[1:])])

    k = int(min(4, len(seg_feat)))
    labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(seg_feat)

    edges = librosa.frames_to_time(bounds, sr=sr)      # column i spans edges[i:i+2]
    sections = []
    for (a, b), lab in zip(zip(seg[:-1], seg[1:]), labels):
        t0, t1 = float(edges[a]), float(edges[min(b, len(edges) - 1)])
        energy = float(np.sqrt(np.mean(y[int(t0 * sr):int(t1 * sr)] ** 2) + 1e-12))
        kind = "instrumental"
        if vocals is not None:
            v = vocals[int(t0 * sr):int(t1 * sr)]
            if v.size and np.sqrt(np.mean(v ** 2)) > 0.02 * (np.abs(vocals).max() + 1e-9):
                kind = "vocal"
        sections.append(Section(t0, t1, chr(ord("A") + int(lab)), kind, energy))
    return _merge_short(sections, min_seconds)


def _merge_short(sections: list[Section], min_seconds: float) -> list[Section]:
    """Absorb stub segments into their neighbour, keeping the longer one's label."""
    out: list[Section] = []
    for s in sections:
        if out and (s.end - s.start < min_seconds or out[-1].end - out[-1].start < min_seconds):
            prev = out[-1]
            if s.end - s.start > prev.end - prev.start:
                prev.label, prev.kind = s.label, s.kind
            prev.end, prev.energy = s.end, max(prev.energy, s.energy)
        else:
            out.append(s)
    return out


def analyze(y: np.ndarray, sr: int = SR, harmonic: np.ndarray | None = None,
            vocals: np.ndarray | None = None, beats_per_bar: int = 4) -> Analysis:
    """Full analysis. Pass stems when you have them - chords get much better."""
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, trim=False)
    tempo = float(np.atleast_1d(tempo)[0])
    beat_times = librosa.frames_to_time(beats, sr=sr)

    bed = harmonic if harmonic is not None else y
    chords = detect_chords(bed, sr, beat_times)
    a = Analysis(
        duration=len(y) / sr,
        tempo=tempo,
        beat_times=[float(t) for t in beat_times],
        downbeats=_downbeats(onset_env, beats, beat_times, beats_per_bar),
        beats_per_bar=beats_per_bar,
        key="",                     # filled in below, once chords are known
        chords=chords,
        sections=detect_structure(y, sr, beats, vocals),
    )
    a.key = detect_key(bed, sr, chords)
    return a


# --- repeated patterns --------------------------------------------------------

def common_progressions(chords: list[Chord], n: int = 4, top: int = 5) -> list[tuple[str, int]]:
    """Most frequent n-chord loops - the harmonic 'patterns' of the song."""
    from collections import Counter

    names = [c.name for c in chords if c.name != "N.C."]
    if len(names) < n:
        return []
    grams = Counter(" - ".join(names[i:i + n]) for i in range(len(names) - n + 1))
    return [(g, c) for g, c in grams.most_common(top) if c > 1]


def find_riffs(notes, min_notes: int = 8, window: float = 4.0, top: int = 5):
    """Dense melodic passages - good candidates to print as tab.

    Returns [(start, end, n_notes)] for the busiest non-overlapping windows.
    """
    if not notes:
        return []
    starts = np.array([n.start for n in notes])
    hits = [(t, int(((starts >= t) & (starts < t + window)).sum()))
            for t in np.arange(0, starts.max() + 1e-6, window / 2)]
    hits = [h for h in sorted(hits, key=lambda h: -h[1]) if h[1] >= min_notes]
    chosen: list[tuple[float, float, int]] = []
    for t, c in hits:
        if all(t + window <= s or t >= e for s, e, _ in chosen):
            chosen.append((float(t), float(t + window), c))
        if len(chosen) >= top:
            break
    return sorted(chosen)
