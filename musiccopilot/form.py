"""Song form: the repeated blocks of a pop/rock arrangement, found and named.

Western pop and rock is built from a handful of blocks - intro, verse,
pre-chorus, chorus, bridge, solo, outro - that repeat, sometimes transposed for
the last one. This module

  1. segments the song by *repetition* (spectral clustering of a beat-synchronous
     recurrence matrix, the McFee/Ellis laplacian recipe) and snaps the result to
     the bar grid, because pop sections are multiples of four bars;
  2. names each block from arrangement conventions - where it sits, how often it
     comes back, how loud it is, whether anyone is singing, and whether the words
     are the same words each time (that last one is what really marks a chorus);
  3. reduces each block to the chord loop you would write on a chart, and
     compares every repeat against the most typical occurrence of its role, so a
     transposed final chorus shows up as "the same loop, two semitones higher"
     rather than as a part of its own.

Everything downstream (snippets, tabs, the recreate sheet) hangs off `Part`.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field

import numpy as np

from . import texture as tex
from .analysis import KK_MAJOR, KK_MINOR
from .config import (CHORD_QUALITIES, FORM, NOTE_NAMES, QUALITY_SUFFIX, SR,
                     base_stem)

_QUALITY_OF = {suffix: quality for quality, suffix in QUALITY_SUFFIX.items()}


@dataclass
class Part:
    """One block of the arrangement - a verse, a chorus, the guitar solo."""
    start: float
    end: float
    bar: int                  # 1-based bar number this part starts on
    bars: int
    role: str                 # Verse, Chorus, Guitar solo, ...
    index: int                # 1-based occurrence of that role
    total: int                # how many occurrences the role has
    family: str               # letter id of the repeated material behind it
    kind: str                 # "vocal" | "instrumental"
    energy: float             # 0..1, relative to the loudest part
    key: str
    chords: list[str] = field(default_factory=list)      # one entry per bar
    loop: list[str] = field(default_factory=list)        # repeating unit of `chords`
    loop_times: int = 1
    transpose: int = 0        # semitones vs. the reference occurrence of this role
    varies: bool = False      # loop differs from it beyond transposition
    lead: str = ""            # stem carrying it, for solos
    snippet: str = ""         # file name inside snippets/

    @property
    def name(self) -> str:
        """'Chorus 2', but just 'Chorus' when the role only occurs once."""
        return f"{self.role} {self.index}" if self.total > 1 else self.role

    @property
    def slug(self) -> str:
        """Filesystem/URL-safe id for this part, e.g. for snippet file names."""
        s = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        return s or "part"

    def loop_text(self) -> str:
        """`| Em | G | D | C |  x4` - the chart line for this part."""
        if not self.loop:
            return "(no chords detected)"
        bars = " | ".join(self.loop)
        return f"| {bars} |" + (f"  x{self.loop_times}" if self.loop_times > 1 else "")


@dataclass
class Form:
    """The whole arrangement: the bar grid plus the named parts on it."""
    tempo: float
    beats_per_bar: int
    key: str
    bar_times: list[float]              # bar n spans bar_times[n-1:n+1]
    parts: list[Part] = field(default_factory=list)

    # --- json contract ------------------------------------------------------
    def to_dict(self) -> dict:
        """Plain-dict form for form.json; round-trips through `from_dict`."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Form":
        """Rebuild from form.json - `parts` needs its own dataclass conversion."""
        d = dict(d)
        d["parts"] = [Part(**p) for p in d["parts"]]
        return cls(**d)

    # --- lookups ------------------------------------------------------------
    def time_of_bar(self, bar: int) -> float:
        """Seconds at the start of bar `bar` (1-based)."""
        return bar_start(self.bar_times, bar)

    def bar_of(self, t: float) -> int:
        """1-based bar number containing time `t`."""
        return bar_index(self.bar_times, t)

    def find(self, query: str) -> Part | None:
        """Resolve 'chorus', 'verse 2', 'solo', '#4' to a part."""
        q = _norm(query)
        if not q:
            return None
        if q.startswith("#") and q[1:].isdigit():
            i = int(q[1:]) - 1
            return self.parts[i] if 0 <= i < len(self.parts) else None

        m = re.match(r"^(.*?)(\d+)$", q)
        base, want = (m.group(1), int(m.group(2))) if m else (q, None)
        hits = ([p for p in self.parts if base and base == _norm(p.role)]
                or [p for p in self.parts if base and base in _norm(p.role)]
                or [p for p in self.parts if base and base in _norm(p.name)])
        if want is not None:
            hits = [p for p in hits if p.index == want] or hits[want - 1:want]
        return hits[0] if hits else None

    def matching(self, query: str) -> list[Part]:
        """Every part sharing the role of the one `query` resolves to."""
        p = self.find(query)
        return [q for q in self.parts if q.role == p.role] if p else []

    def roles(self) -> dict[str, list[Part]]:
        """All parts grouped by role, in song order within each role."""
        out: dict[str, list[Part]] = {}
        for p in self.parts:
            out.setdefault(p.role, []).append(p)
        return out

    def outline(self) -> str:
        """One-line summary of the song, e.g. 'Intro (4) -> Verse 1 (16) -> ...'."""
        return " -> ".join(f"{p.name} ({p.bars})" for p in self.parts)


def _norm(s: str) -> str:
    """Lowercase, alphanumeric-plus-# only - for loose matching of user queries."""
    return re.sub(r"[^a-z0-9#]", "", str(s).lower())


# Part times are rounded to 2dp on the way through form.json, which can put a
# part start a few milliseconds below its own bar line.
BAR_TOLERANCE = 0.05


def bar_start(bar_times, bar: int) -> float:
    """Start of bar `bar` (1-based); past the end clamps to the last bar line."""
    return float(bar_times[int(np.clip(bar - 1, 0, len(bar_times) - 1))])


def bar_index(bar_times, t: float) -> int:
    """1-based bar containing `t`."""
    return max(1, int(np.searchsorted(bar_times, t + BAR_TOLERANCE, side="right")))


# --- chords by the bar --------------------------------------------------------

def parse_chord(name: str) -> tuple[int | None, str]:
    """'C#m7' -> (1, 'min7'). Root None for N.C. / anything unparseable."""
    for note in sorted(NOTE_NAMES, key=len, reverse=True):    # "C#" before "C"
        if name.startswith(note) and (rest := name[len(note):]) in _QUALITY_OF:
            return NOTE_NAMES.index(note), _QUALITY_OF[rest]
    return None, ""


def bar_chords(chords, edges: np.ndarray, i0: int, i1: int) -> list[str]:
    """The chord that owns most of each bar, one entry per bar in [i0, i1)."""
    out = []
    for i in range(i0, min(i1, len(edges) - 1)):
        t0, t1 = edges[i], edges[i + 1]
        best, cover = "N.C.", 0.0
        for c in chords:
            if c.end <= t0 or c.start >= t1:
                continue
            if (ov := min(c.end, t1) - max(c.start, t0)) > cover:
                cover, best = ov, c.name
        out.append(best)
    return out


# Template matching cannot reliably tell Em from E5 from Em7 on a single beat -
# and on a chart it does not matter. Compare chords by root plus "is it major or
# minor", with power chords and sus chords matching either.
_FAMILY = {"maj": "maj", "maj7": "maj", "7": "maj",
           "min": "min", "min7": "min", "dim": "min",
           "5": "any", "sus4": "any", "": "any"}


def chord_key(name: str) -> tuple[int | None, str]:
    """Root plus coarse family ('maj'/'min'/'any') used for loose chord comparison."""
    root, quality = parse_chord(name)
    return root, _FAMILY.get(quality, "any")


def same_chord(a: str, b: str) -> bool:
    """Same root and (major/minor agree, or either is a power/sus chord)."""
    (ra, fa), (rb, fb) = chord_key(a), chord_key(b)
    if ra is None or rb is None:
        return a == b                        # N.C. only ever matches N.C.
    return ra == rb and (fa == fb or "any" in (fa, fb))


def representative(names: list[str]) -> str:
    """The one name to print for a slot several noisy detections agree on.

    Majority root wins; the quality comes from the detections that actually
    committed to one, and a plain triad beats a fancier spelling on a tie.
    """
    if not names:
        return "N.C."
    keys = [chord_key(n) for n in names]
    roots = [r for r, _ in keys if r is not None]
    if not roots:
        return Counter(names).most_common(1)[0][0]
    root = Counter(roots).most_common(1)[0][0]
    same = [n for n, (r, _) in zip(names, keys) if r == root]
    pool = [n for n in same if chord_key(n)[1] != "any"] or same
    count = Counter(pool)
    return max(pool, key=lambda n: (count[n], -len(n)))


def find_loop(bars: list[str], agreement: float = 0.75) -> tuple[list[str], int]:
    """Shortest chord cycle the bar list is (mostly) built from.

    Chord detection is noisy, so a period counts as long as most bars agree with
    the consensus chord for their slot; the returned loop is that consensus.
    """
    n = len(bars)
    for p in (1, 2, 4, 8, 3, 6, 12, 16):
        if n < 2 * p:                          # need to see the cycle at least twice
            continue
        pattern = [representative(bars[j::p]) for j in range(p)]
        if loop_fit(bars, pattern) >= agreement:
            return pattern, int(round(n / p))
    return list(bars), 1


def loop_fit(bars: list[str], loop: list[str]) -> float:
    """How much of `bars` is explained by cycling `loop`."""
    if not bars or not loop:
        return 0.0
    return sum(same_chord(b, loop[i % len(loop)]) for i, b in enumerate(bars)) / len(bars)


def compare_loops(a: list[str], b: list[str]) -> tuple[int, float]:
    """Best transposition of `a` onto `b`, and how much of it then matches.

    Loops of different lengths are cycled rather than truncated: a four-bar loop
    played twice really is the same thing as an eight-bar loop of two identical
    halves, and truncating would call any two loops that share an opening bar
    the same.
    """
    ka, kb = [chord_key(x) for x in a], [chord_key(x) for x in b]
    if not ka or not kb:
        return 0, 0.0
    n = max(len(ka), len(kb))
    pairs = [(ka[i % len(ka)], kb[i % len(kb)]) for i in range(n)]
    pairs = [(x, y) for x, y in pairs if x[0] is not None and y[0] is not None]
    if not pairs:
        return 0, 0.0
    best = (0, -1.0)
    for shift in range(12):                       # shift 0 first, so ties keep it
        hits = sum((ra + shift) % 12 == rb and (fa == fb or "any" in (fa, fb))
                   for (ra, fa), (rb, fb) in pairs)
        if (ratio := hits / len(pairs)) > best[1]:
            best = (shift, ratio)
    return best[0], best[1]


def transpose_loop(loop: list[str], semitones: int) -> list[str]:
    """The same progression in another key."""
    from .analysis import chord_name
    out = []
    for name in loop:
        root, quality = parse_chord(name)
        out.append(name if root is None else chord_name((root + semitones) % 12, quality))
    return out


def key_of_chords(chords) -> str:
    """Key of a chord span, from the notes those chords put in the air."""
    w = np.zeros(12)
    for c in chords:
        if c.root < 0:
            continue
        for i in CHORD_QUALITIES.get(c.quality, (0, 4, 7)):
            w[(c.root + i) % 12] += (c.end - c.start) * (1.4 if i == 0 else 1.0)
    if w.sum() <= 0:
        return ""
    z = (w - w.mean()) / (w.std() + 1e-9)
    best, out = -9.0, ""
    for root in range(12):
        for profile, mode in ((KK_MAJOR, "major"), (KK_MINOR, "minor")):
            p = np.roll(profile, root)
            s = float(np.corrcoef(z, (p - p.mean()) / p.std())[0, 1])
            if s > best:
                best, out = s, f"{NOTE_NAMES[root]} {mode}"
    return out


def bar_edges(analysis) -> np.ndarray:
    """Bar boundaries in seconds; bar n spans edges[n-1:n+1]."""
    d = [t for t in analysis.downbeats if t < analysis.duration - 0.05]
    if len(d) < 2:                     # no usable downbeat grid - lay one down
        spb = 60.0 / max(analysis.tempo, 1e-6) * analysis.beats_per_bar
        d = list(np.arange(0.0, analysis.duration, spb))
    return np.array(d + [analysis.duration], dtype=float)


# --- segmentation by repetition ----------------------------------------------

def _beat_features(y: np.ndarray, sr: int, beat_times) -> tuple:
    """Chroma (harmony) and MFCC (timbre), beat-synced, plus the beat edges used to do it."""
    import librosa

    from .analysis import _beat_bounds
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, bins_per_octave=36)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    n = min(chroma.shape[1], mfcc.shape[1])
    bounds = _beat_bounds(np.asarray(beat_times), sr, n)
    return (librosa.util.sync(chroma[:, :n], bounds, aggregate=np.median),
            librosa.util.sync(mfcc[:, :n], bounds, aggregate=np.mean),
            librosa.frames_to_time(bounds, sr=sr))


def _embedding(csync: np.ndarray, msync: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Laplacian eigenvectors of (harmonic repetition + timbral continuity).

    The recurrence matrix says "these two beats sound harmonically alike"; the
    path matrix says "these two beats are neighbours". Balancing the two by
    degree is what keeps a repeat from being shredded into fragments.
    """
    import librosa
    import scipy.linalg
    import scipy.ndimage
    import scipy.sparse.csgraph

    rec = librosa.segment.recurrence_matrix(csync, width=3, mode="affinity", sym=True)
    rec = librosa.segment.timelag_filter(scipy.ndimage.median_filter)(rec, size=(1, 7))

    dist = np.sum(np.diff(msync, axis=1) ** 2, axis=0)
    sigma = float(np.median(dist)) or 1.0
    sim = np.exp(-dist / sigma)
    path = np.diag(sim, 1) + np.diag(sim, -1)

    deg_path, deg_rec = path.sum(axis=1), rec.sum(axis=1)
    denom = float(np.sum((deg_path + deg_rec) ** 2)) or 1.0
    mu = float(deg_path.dot(deg_path + deg_rec)) / denom
    affinity = mu * rec + (1.0 - mu) * path

    lap = scipy.sparse.csgraph.laplacian(affinity, normed=True)
    _, evecs = scipy.linalg.eigh(lap)
    evecs = scipy.ndimage.median_filter(evecs, size=(9, 1))
    return evecs, np.cumsum(evecs ** 2, axis=1) ** 0.5


def _cluster(evecs: np.ndarray, cnorm: np.ndarray, k: int) -> np.ndarray:
    """K-means over the first k Laplacian eigenvectors, row-normalised (spectral clustering)."""
    from sklearn.cluster import KMeans
    x = evecs[:, :k] / (cnorm[:, k - 1:k] + 1e-9)
    return KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(x)


def _bar_labels(beat_labels: np.ndarray, beat_edges: np.ndarray,
                edges: np.ndarray) -> list[int]:
    """Majority vote of the beats inside each bar."""
    mid = 0.5 * (beat_edges[:-1] + beat_edges[1:])
    mid = mid[:len(beat_labels)]
    out, last = [], int(beat_labels[0])
    for i in range(len(edges) - 1):
        inside = beat_labels[(mid >= edges[i]) & (mid < edges[i + 1])]
        last = Counter(inside.tolist()).most_common(1)[0][0] if inside.size else last
        out.append(int(last))
    return out


def _runs(labels: list[int]) -> list[list[int]]:
    """Collapse a per-bar label sequence into [start, end, label] runs."""
    runs: list[list[int]] = []
    for i, lab in enumerate(labels):
        if runs and runs[-1][2] == lab:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1, lab])
    return runs


def _merge_short(runs: list[list[int]], min_bars: int) -> list[list[int]]:
    """Absorb stub runs into whichever neighbour is longer."""
    runs = [list(r) for r in runs]
    while len(runs) > 1:
        i = min(range(len(runs)), key=lambda j: runs[j][1] - runs[j][0])
        if runs[i][1] - runs[i][0] >= min_bars:
            break
        left = runs[i - 1] if i > 0 else None
        right = runs[i + 1] if i + 1 < len(runs) else None
        into = (left if right is None else
                right if left is None else
                left if (left[1] - left[0]) >= (right[1] - right[0]) else right)
        into[0], into[1] = min(into[0], runs[i][0]), max(into[1], runs[i][1])
        runs.pop(i)
    return _join_same(runs)


def _join_same(runs: list[list[int]]) -> list[list[int]]:
    """Merge adjacent runs that ended up with the same label (e.g. after `_snap`)."""
    out: list[list[int]] = []
    for r in runs:
        if out and out[-1][2] == r[2]:
            out[-1][1] = r[1]
        else:
            out.append(list(r))
    return out


def _snap(runs: list[list[int]], snap: int, min_bars: int) -> list[list[int]]:
    """Nudge boundaries onto the 4-bar grid when they are already close."""
    runs = [list(r) for r in runs]
    for i in range(1, len(runs)):
        b = runs[i][0]
        target = int(round(b / snap) * snap)
        if abs(target - b) > snap // 2 or target <= runs[i - 1][0]:
            continue
        if target >= runs[i][1] or (target - runs[i - 1][0]) < min_bars:
            continue
        runs[i - 1][1] = runs[i][0] = target
    return runs


def _score(runs: list[list[int]]) -> float:
    """How much this segmentation looks like a pop song."""
    if len(runs) < 3:
        return -1e9
    lens = np.array([b - a for a, b, _ in runs], dtype=float)
    counts = Counter(lab for _, _, lab in runs)
    repeated = float(np.mean([counts[lab] > 1 for _, _, lab in runs]))
    sane = float(np.mean((lens >= 4) & (lens <= 40)))
    tidy = float(np.mean(lens % 4 == 0))
    return 2.0 * repeated + 1.0 * sane + 0.7 * tidy - 0.6 * abs(len(runs) - 10) / 10.0


def segment(y: np.ndarray, sr: int, beat_times, edges: np.ndarray) -> list[list[int]]:
    """Bar ranges [start, end, family] of self-similar material."""
    n_bars = len(edges) - 1
    csync, msync, beat_edges = _beat_features(y, sr, beat_times)
    if csync.shape[1] < 16 or n_bars < 8:
        return [[0, n_bars, 0]]

    evecs, cnorm = _embedding(csync, msync)
    lo, hi = FORM["k_range"]
    best: tuple[float, list[list[int]]] = (-np.inf, [[0, n_bars, 0]])
    for k in range(lo, min(hi, evecs.shape[1]) + 1):
        labels = _bar_labels(_cluster(evecs, cnorm, k), beat_edges, edges)
        runs = _merge_short(_runs(labels), FORM["min_bars"])
        runs = _snap(runs, FORM["snap_bars"], FORM["min_bars"])
        runs = _merge_short(_join_same(runs), FORM["min_bars"])
        if (s := _score(runs)) > best[0]:
            best = (s, runs)

    # too many parts is unreadable, but truncating would drop the end of the
    # song - merge the shortest ones away instead until it fits
    runs, limit = best[1], FORM["min_bars"]
    while len(runs) > FORM["max_parts"]:
        limit += 2
        runs = _merge_short(runs, limit)
    return runs


# --- tidying the block boundaries --------------------------------------------

def _trim_silence(runs: list[list[int]], energy: np.ndarray, floor: float = 0.05) -> list[list[int]]:
    """Drop dead air at the very start and end - a fade-in is not an intro."""
    thr = floor * float(energy.max() or 1.0)
    while runs[0][1] - runs[0][0] > FORM["min_bars"] and energy[runs[0][0]] < thr:
        runs[0][0] += 1
    while runs[-1][1] - runs[-1][0] > FORM["min_bars"] and energy[runs[-1][1] - 1] < thr:
        runs[-1][1] -= 1
    return runs


def _phase_fit(seq: list[str], loop: list[str]) -> float:
    """Best fit of `seq` against `loop`, over every rotation of the loop."""
    if not seq or not loop:
        return 0.0
    return max(sum(same_chord(c, loop[(i + ph) % len(loop)]) for i, c in enumerate(seq))
               for ph in range(len(loop))) / len(seq)


def core_loop(seq: list[str], agreement: float) -> tuple[list[str], int]:
    """The loop a block *starts* on, and how many bars it holds for.

    `find_loop` asks "is the whole block one cycle?", which fails the moment a
    four-bar pre-chorus is glued onto the end of a verse - and that is exactly
    the case worth detecting. This asks the weaker question instead: what is the
    block cycling on when it begins, and where does it stop cycling?
    """
    for p in (2, 4, 8, 3, 6, 16):
        if len(seq) < 2 * p:
            continue
        candidate = [representative(seq[j::p][:2]) for j in range(p)]
        if loop_fit(seq[:2 * p], candidate) < agreement:
            continue
        held = 2 * p
        while held + p <= len(seq) and loop_fit(seq[held:held + p], candidate) >= agreement:
            held += p
        return candidate, held
    return find_loop(seq, agreement)[0], len(seq)


def _refine(runs: list[list[int]], bars: list[str], agreement: float,
            min_bars: int) -> list[list[int]]:
    """Trim every occurrence of a block back to the bars that fit its own loop.

    A block that comes back three times is strong evidence for how long it is,
    so an occurrence that runs long is cut back by whole cycles of its chord
    loop - and, crucially, the bars it gives up are kept as parts in their own
    right rather than smeared into the neighbour. Leftovers that turn out to
    share a progression become one family: that is how a four-bar pre-chorus,
    which the timbre-based segmenter always glues to the verse or the chorus,
    surfaces as its own repeated block.
    """
    runs = [list(r) for r in runs]
    fams: dict[int, list[int]] = {}
    for i, r in enumerate(runs):
        fams.setdefault(r[2], []).append(i)

    leftovers: list[list[int]] = []
    for fam in sorted(fams, key=lambda f: -len(fams[f])):     # most repeated first
        idx = fams[fam]
        if len(idx) < 2:
            continue
        lens = [runs[i][1] - runs[i][0] for i in idx]
        target = Counter(lens).most_common(1)[0][0]
        ref = next(i for i in idx if runs[i][1] - runs[i][0] == target)
        loop, _ = core_loop(bars[runs[ref][0]:runs[ref][1]], agreement)
        if len(loop) < 2:
            continue

        for i in idx:
            i0, i1 = runs[i][0], runs[i][1]
            seq = bars[i0:i1]
            # a repeat may be transposed (the lifted last chorus); match the loop
            # in that occurrence's own key before deciding where it ends
            shift, _ = compare_loops(loop, seq)
            loop_here = transpose_loop(loop, shift) if shift else loop
            base_fit = _phase_fit(seq, loop_here)
            best = (base_fit, 0, 0)
            for head in (0, len(loop), 2 * len(loop)):
                for tail in (0, len(loop), 2 * len(loop)):
                    core = seq[head:len(seq) - tail]
                    if head + tail == 0 or head + tail > len(seq) // 2:
                        continue
                    if len(core) < max(min_bars, len(loop)):
                        continue
                    fit = _phase_fit(core, loop_here) - 0.02 * (head + tail)
                    if fit > best[0]:
                        best = (fit, head, tail)
            fit, head, tail = best
            # An occurrence whose length disagrees with the length the rest of
            # the family agreed on (e.g. a lone 20-bar block among three
            # 16-bar ones) is independent evidence something is glued on, even
            # when the chord-fit improvement from trimming it is modest - the
            # extra bars are usually a differently-worded pre-chorus/bridge
            # whose chords partly resemble the loop either side of it. Without
            # this, only a clean fit jump (>=0.1) triggers a trim, and a messy
            # but real boundary like that gets glued onto its neighbour.
            off_length = (i1 - i0) != target and (head or tail) and \
                (i1 - i0 - head - tail) == target
            gain = fit - base_fit
            if gain < 0.1 and not (off_length and gain >= 0.02):
                continue
            if head:
                leftovers.append([i0, i0 + head])
            if tail:
                leftovers.append([i1 - tail, i1])
            runs[i][0], runs[i][1] = i0 + head, i1 - tail

    # leftovers with the same progression are the same block coming back
    next_fam, groups = max(r[2] for r in runs) + 1, []
    for lo in sorted(leftovers):
        seq = bars[lo[0]:lo[1]]
        for gfam, gseq in groups:
            shift, ratio = compare_loops(gseq, seq)
            if shift == 0 and ratio >= agreement:
                lo.append(gfam)
                break
        else:
            groups.append((next_fam, seq))
            lo.append(next_fam)
            next_fam += 1

    runs = sorted(runs + leftovers, key=lambda r: r[0])
    return _merge_short(_join_same(runs), min_bars)


# --- what each block is -------------------------------------------------------

# Bass is deliberately not a lead-solo candidate: it is loud and active
# through almost the whole arrangement (walking lines, held roots), so
# loudness/range heuristics that correctly rule out a near-silent or
# bleed-through stem cannot tell "bassline" from "bass solo" - and a real bass
# solo is rare enough in rock/pop that this is the right default to be wrong
# about, rather than mislabelling the far more common guitar/piano solo.
LEAD_STEMS = ("guitar", "piano", "other")

# "other" is Demucs' catch-all bucket, not a named instrument - on a guitar or
# piano solo it usually holds a quieter, smeared copy of whichever named stem
# didn't separate cleanly (bleed), rather than an actual different instrument.
# It should only be picked as the lead when it clearly dominates, not merely
# edges out a named stem on a noisy per-part estimate.
LEAD_STEM_BIAS = {"guitar": 1.0, "piano": 1.0, "other": 0.7}


@dataclass
class _Seg:
    """A segment while it is being worked out; becomes a Part at the end."""
    i0: int
    i1: int
    fam: int
    start: float
    end: float
    voice: float
    energy: float
    text: str
    density: dict[str, float]
    role: str = ""
    lead: str = ""


def _bar_stat(y: np.ndarray, sr: int, edges: np.ndarray, voice: bool) -> np.ndarray:
    """Per-bar loudness, or per-bar fraction-of-time-someone-is-singing."""
    import librosa
    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    t = librosa.times_like(rms, sr=sr, hop_length=512)
    thr = 0.10 * float(np.percentile(rms, 99))          # ignores stem bleed
    out = np.zeros(len(edges) - 1)
    for i in range(len(edges) - 1):
        m = (t >= edges[i]) & (t < edges[i + 1])
        if m.any():
            out[i] = float(np.mean(rms[m] > thr)) if voice else float(np.mean(rms[m]))
    return out


def _bar_voice_from_lyrics(lyrics, edges: np.ndarray) -> np.ndarray:
    """Fallback when there is no vocal stem: where the words are."""
    out = np.zeros(len(edges) - 1)
    for i in range(len(edges) - 1):
        t0, t1 = edges[i], edges[i + 1]
        covered = sum(max(0.0, min(l.end, t1) - max(l.start, t0)) for l in lyrics)
        out[i] = float(np.clip(covered / max(t1 - t0, 1e-6), 0, 1))
    return out


def _words(text: str) -> set[str]:
    """Lowercased words of length >=3 in `text` - short words are too common to signal repetition."""
    return set(re.findall(r"[a-z']{3,}", text.lower()))


def _lyric_repeat(texts: list[str]) -> float:
    """Mean word overlap between occurrences - a chorus says the same thing."""
    sets = [w for w in map(_words, texts) if w]
    if len(sets) < 2:
        return 0.0
    pairs = [len(a & b) / len(a | b) for i, a in enumerate(sets) for b in sets[i + 1:]]
    return float(np.mean(pairs)) if pairs else 0.0


def _minmax(d: dict) -> dict:
    """Rescale a dict's values to 0..1; an all-equal dict maps to 0.5 rather than dividing by zero."""
    lo, hi = min(d.values()), max(d.values())
    return {k: (v - lo) / (hi - lo) if hi > lo else 0.5 for k, v in d.items()}


def _family_stats(segs: list[_Seg]) -> dict:
    """Per family of repeated material, judged on the occurrences that are sung.

    Clustering is coarse: one family routinely holds the verse *and* the solo
    that plays over the verse changes *and* the outro. Averaging over all of
    them would hide the verse, so a family counts as vocal when half its
    occurrences are, and everything that decides a vocal role is measured on
    those occurrences alone.
    """
    fams: dict[int, list[int]] = {}
    for i, s in enumerate(segs):
        fams.setdefault(s.fam, []).append(i)

    out = {}
    for f, idx in fams.items():
        sung = [i for i in idx if segs[i].voice >= FORM["vocal_threshold"]]
        on = sung or idx
        out[f] = dict(
            members=idx,
            sung=sung,
            count=len(on),
            voice=len(sung) / len(idx),
            energy=float(np.mean([segs[i].energy for i in on])),
            bars=float(np.mean([segs[i].i1 - segs[i].i0 for i in on])),
            first=segs[on[0]].start,
            lyric_rep=_lyric_repeat([segs[i].text for i in on]),
        )
    return out


def _family_roles(segs: list[_Seg], stats: dict, duration: float) -> dict[int, str]:
    """Which family is the chorus, which is the verse, and so on.

    The chorus is the loud one that comes back with the same words; the verse is
    the one that comes back early and often with *different* words; a pre-chorus
    is a short block that keeps handing over to the chorus; a bridge shows up
    late and only once or twice.
    """
    vocal_th = FORM["vocal_threshold"]
    vocal = [f for f, s in stats.items() if s["voice"] >= 0.5]
    roles: dict[int, str] = {}
    if not vocal:
        return roles

    if len(vocal) == 1:          # nothing to contrast: the words decide which it is
        only = vocal[0]
        roles[only] = "Chorus" if stats[only]["lyric_rep"] >= 0.4 else "Verse"
        return roles

    first_vocal = next((s.fam for s in segs if s.voice >= vocal_th), None)
    energy = _minmax({f: stats[f]["energy"] for f in vocal})
    chorus = max(vocal, key=lambda f: (
        1.0 * energy[f]
        + 0.8 * min(stats[f]["count"], 4) / 4
        + 1.2 * stats[f]["lyric_rep"]
        - 0.25 * (f == first_vocal)))
    roles[chorus] = "Chorus"

    rest = [f for f in vocal if f != chorus]
    verse = None
    if rest:
        energy = _minmax({f: stats[f]["energy"] for f in rest})
        # A verse is a block the song comes back to, so a family that was
        # only ever sung once - a one-off pre-chorus or bridge glued onto a
        # neighbour, however early it sits or however different its words -
        # must not outscore a family that is genuinely repeated. Without this
        # a short, single-occurrence block can win "Verse" outright and bump
        # the real (multi-occurrence) verse out of the family_roles dict
        # entirely, since only one role per family is ever assigned.
        repeated = [f for f in rest if len(stats[f]["sung"]) >= 2] or rest
        verse = max(repeated, key=lambda f: (
            1.0 * min(stats[f]["count"], 4) / 4
            + 0.7 * (1 - stats[f]["first"] / max(duration, 1e-6))
            + 0.8 * (1 - stats[f]["lyric_rep"])
            + 0.4 * (1 - energy[f])))
        roles[verse] = "Verse"
        rest.remove(verse)

    for f in list(rest):
        handover = np.mean([i + 1 < len(segs) and segs[i + 1].fam == chorus
                            for i in stats[f]["sung"]])
        if handover >= 0.5 and (verse is None or stats[f]["bars"] <= stats[verse]["bars"]):
            roles[f] = "Pre-Chorus"
            rest.remove(f)
            break

    late = [f for f in rest if stats[f]["first"] > 0.45 * duration and stats[f]["count"] <= 2]
    if late:
        roles[max(late, key=lambda f: stats[f]["first"])] = "Bridge"
    return roles


def _assign(segs: list[_Seg], duration: float, letters: dict[int, str]) -> None:
    """Fill in `role` (and `lead`, for solos) on every segment, in place.

    Two different naming strategies, split by whether anyone is singing:

    - Vocal segments are named by *family* (`_family_stats` + `_family_roles`):
      the chorus is the loud, repeated family whose occurrences share the most
      words (`_lyric_repeat`); the verse is whichever other family comes back
      early and often with *different* words each time - and only a family
      sung more than once is eligible, so a one-off pre-chorus or bridge can't
      steal the verse slot just because it scores well on "early and unique
      words" (a family gets at most one role, so if it did, the real,
      repeated verse would end up with no role at all). A pre-chorus is a
      short family that mostly hands straight over to the chorus; a bridge is
      whatever late, rarely-repeated family is left. Every segment in a
      family gets that family's role, whichever specific occurrence it is.
    - Instrumental segments are named per-segment, not per-family, because an
      instrumental break has no words to compare and its identity is really
      "is one instrument clearly out front here". That is read off
      `s.density` (per-stem note rate, pre-weighted by audibility and pitch
      variety - see `detect_form`) as a *relative* margin over the runner-up
      stem, deliberately not an absolute notes/sec threshold, because a solo
      window is later re-transcribed monophonically
      (`pipeline._refine_lead_notes`) and will honestly report far fewer notes
      than the polyphonic pass every other part is still measured with - one
      shared rate threshold would not be comparable across parts. The first
      and last segments are always Intro/Outro; a short segment with no clear
      lead is a Break, a long one is Instrumental.
    """
    stats = _family_stats(segs)
    roles = _family_roles(segs, stats, duration)
    vocal_th = FORM["vocal_threshold"]

    for i, s in enumerate(segs):
        if s.voice >= vocal_th:                    # someone is singing over it
            s.role = roles.get(s.fam) or (
                "Intro" if i == 0 else "Outro" if i == len(segs) - 1 else
                f"Section {letters[s.fam]}")
            continue
        ranked = sorted(s.density.items(), key=lambda kv: -kv[1])
        lead, density = ranked[0] if ranked else ("", 0.0)
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        # Nobody is singing here, so the question is only whether one instrument
        # is carrying it. That is a *relative* judgement - how far the lead is
        # clear of the next stem - deliberately not an absolute notes/sec bar:
        # solo windows get re-transcribed monophonically afterwards
        # (pipeline._refine_lead_notes), which honestly reports far fewer notes
        # than the polyphonic pass every other part is still measured with, so
        # one shared rate threshold is not comparable across parts.
        leads = density >= FORM["solo_density"] * max(runner_up, 0.35)
        if i == 0:
            s.role = "Intro"
        elif i == len(segs) - 1:
            s.role = "Outro"
        elif leads:
            s.lead = lead
            # Named for the *instrument*, not the stem: a split guitar's lead
            # voice is `guitar-2`, and without `base_stem` here it is a plain
            # "Solo" - which is the one place a suffixed stem name reaches the
            # user as a part title. See CLAUDE.md, "Stem names are load-bearing".
            instrument = base_stem(lead)
            s.role = (f"{instrument.capitalize()} solo"
                      if instrument in ("guitar", "piano", "bass") else "Solo")
        elif s.i1 - s.i0 <= FORM["min_bars"]:
            s.role = "Break"
        else:
            s.role = "Instrumental"


# --- entry point --------------------------------------------------------------

def detect_form(analysis, y: np.ndarray, sr: int = SR, vocals: np.ndarray | None = None,
                notes: dict | None = None, lyrics: list | None = None,
                stems: dict | None = None) -> Form:
    """Find the parts of the arrangement and describe each one."""
    edges = bar_edges(analysis)
    runs = segment(y, sr, analysis.beat_times, edges)

    energy = _bar_stat(y, sr, edges, voice=False)
    if vocals is not None and np.abs(vocals).max() > 1e-4:
        voice = _bar_stat(vocals, sr, edges, voice=True)
    elif lyrics:
        voice = _bar_voice_from_lyrics(lyrics, edges)
    else:
        voice = np.zeros(len(edges) - 1)

    # Loudness per lead-candidate stem, so a near-silent stem that Basic Pitch
    # hallucinates notes on (transcription noise on the noise floor Demucs
    # leaves behind) can never outscore the stem that is actually audible.
    loudness: dict[str, np.ndarray] = {}
    for stem, path in (stems or {}).items():
        if base_stem(stem) not in LEAD_STEMS:
            continue
        try:
            import librosa
            sy = librosa.load(str(path), sr=sr, mono=True)[0]
        except Exception:                              # noqa: BLE001
            continue
        loud = _bar_stat(sy, sr, edges, voice=False)
        loudness[stem] = loud / (float(loud.max()) or 1.0)

    every_bar = bar_chords(analysis.chords, edges, 0, len(edges) - 1)
    runs = _trim_silence(runs, energy)
    runs = _refine(runs, every_bar, FORM["loop_agreement"], FORM["min_bars"])

    segs: list[_Seg] = []
    for i0, i1, fam in runs:
        start, end = float(edges[i0]), float(edges[min(i1, len(edges) - 1)])
        text = " ".join(l.text for l in (lyrics or []) if l.end > start and l.start < end)
        span = max(end - start, 1e-6)
        density = {}
        for stem, ns in (notes or {}).items():
            if base_stem(stem) not in LEAD_STEMS:
                continue
            win = [n for n in ns if n.end > start and n.start < end]
            rate = len(win) / span
            loud = loudness.get(stem)
            # Weight by how audible the stem actually is over this part - a
            # stem sitting near the noise floor should never win on note
            # count alone, however many spurious notes got transcribed on it.
            weight = float(loud[i0:i1].mean()) if loud is not None else 1.0
            # And by how much it actually moves around: a bassline can be as
            # loud and as "busy" (onset-wise) as a solo while just walking a
            # repetitive low-register groove, which a wide melodic range and
            # varied pitch classes rule out.
            pitches = [n.pitch for n in win]
            if pitches:
                spread = min(1.0, (max(pitches) - min(pitches)) / 24.0)
                variety = min(1.0, len(set(p % 12 for p in pitches)) / 8.0)
                weight *= 0.4 + 0.6 * (0.5 * spread + 0.5 * variety)
            # And by whether it is playing a *line* at all. A lead is one note
            # at a time; a guitar strumming underneath one can match it for
            # note rate and is never the lead - it is the same argument that
            # keeps `bass` out of LEAD_STEMS, and it only became measurable
            # once `texture` could say which notes were struck together.
            #
            # This matters most exactly where it is newest. Before a stem is
            # split into players, a solo's runner-up is a near-silent piano
            # and the ratio is huge; after `voices.py` splits one guitar into
            # a rhythm part and a lead, the runner-up *is* the rhythm guitar,
            # playing all the way through the solo, and `solo_density` stops
            # being clearable. Without this a split silently demotes every
            # "Guitar solo" to "Instrumental" and takes the monophonic
            # re-transcription - and every bend in the tab - with it.
            if win:
                chordal = float(tex.chordness(win).mean())
                weight *= 1.0 - FORM["lead_chord_penalty"] * chordal
            density[stem] = rate * weight * LEAD_STEM_BIAS.get(base_stem(stem), 1.0)
        segs.append(_Seg(i0, i1, fam, start, end,
                         float(voice[i0:i1].mean()), float(energy[i0:i1].mean()),
                         text, density))
    if runs[0][0] == 0:                     # keep any pickup before the first downbeat
        segs[0].start = 0.0
    loudest = max((s.energy for s in segs), default=1.0) or 1.0
    for s in segs:
        s.energy = s.energy / loudest

    # A vocal stem carries reverb tails and bleed, so it can report singing over
    # an outro. If the song is well transcribed, no words in a part means nobody
    # is singing in it.
    sung = sum(l.end - l.start for l in (lyrics or []))
    if lyrics and sung > 0.15 * analysis.duration:
        for s in segs:
            if not any(l.end > s.start and l.start < s.end for l in lyrics):
                s.voice = 0.0

    letters = {fam: chr(ord("A") + i)
               for i, fam in enumerate(dict.fromkeys(s.fam for s in segs))}
    _assign(segs, analysis.duration, letters)

    counts = Counter(s.role for s in segs)
    seen: Counter = Counter()
    parts: list[Part] = []
    for s in segs:
        seen[s.role] += 1
        window = [c for c in analysis.chords if c.end > s.start and c.start < s.end]
        chords = every_bar[s.i0:s.i1]           # the same bars _refine reasoned about
        loop, times = find_loop(chords, FORM["loop_agreement"])
        parts.append(Part(
            start=round(s.start, 2), end=round(s.end, 2),
            bar=s.i0 + 1, bars=s.i1 - s.i0,
            role=s.role, index=seen[s.role], total=counts[s.role],
            family=letters[s.fam],
            kind="vocal" if s.voice >= FORM["vocal_threshold"] else "instrumental",
            energy=round(s.energy, 3),
            key=key_of_chords(window) or analysis.key,
            chords=chords, loop=loop, loop_times=times, lead=s.lead))

    _mark_variants(parts)
    return Form(tempo=analysis.tempo, beats_per_bar=analysis.beats_per_bar,
                key=analysis.key, bar_times=[float(t) for t in edges], parts=parts)


def reference_part(parts: list[Part]) -> Part:
    """The occurrence that best represents them all - the one to put on the chart:
    the one most others agree with harmonically, at the most usual length."""
    lengths = Counter(p.bars for p in parts)
    return max(parts, key=lambda p: (sum(compare_loops(p.loop, q.loop)[1]
                                         for q in parts if q is not p),
                                     lengths[p.bars], -p.bars))


def _mark_variants(parts: list[Part]) -> None:
    """Say how each repeat differs from the role's reference occurrence:
    nothing at all, the same loop transposed, or genuinely other chords."""
    by_role: dict[str, list[Part]] = {}
    for p in parts:
        by_role.setdefault(p.role, []).append(p)
    for group in by_role.values():
        ref = reference_part(group)
        for p in group:
            if p is ref:
                continue
            shift, ratio = compare_loops(ref.loop, p.loop)
            p.transpose = shift - 12 if shift > 6 else shift
            p.varies = ratio < FORM["same_loop"]
