"""Import a multitrack session from a DAW - GarageBand or BandLab.

Everything downstream of separation already works off `analyzed_songs/<id>/
stems/*.wav`, so importing a real multitrack is not a new pipeline: it is the
*same* pipeline with its slowest and least reliable stage deleted. This module
writes the stems demucs would otherwise have guessed at, synthesises the
mixdown the rest of the code treats as "the song", and records what came from
where in `sources.json` - whose presence is also what stops `--force` from
re-separating a recording back into an estimate of itself.

Two front doors, because the two DAWs give you different things:

**A folder of exported stems** is the exact case, and the only one BandLab
needs: Project > Download > Tracks gives per-track WAVs that all start at zero
and run the full length of the song. Point at the folder, nothing is guessed.

**A `.zip` of those tracks** is that same door with a lid on. BandLab hands the
tracks over one download at a time, so they land loose in `~/Downloads` among
everything else and have to be collected before they are worth pointing at -
and the collecting is where the wrong five files end up together. A zip is what
a browser can upload and what a phone can send, so reading one directly is also
what lets the tracks arrive from a machine that is not this one.

**A `.band` package** is the convenient case. GarageBand has no stem export at
all - the documented way out is to solo each track and Share > Export Song to
Disk, once per track - but the package is a folder, and `Media/` inside it
holds every recorded take as its own file. Reading those directly costs
nothing and needs no exporting, at the price of one assumption: that each
region starts at bar 1. Where a region actually sits on the timeline is in
`projectData`, an undocumented Apple format, and guessing at it would be worse
than saying so. For a practice-room take - hit record once, everyone plays
through - the assumption holds exactly, which is the case this is for. For an
edited or comped project, export the stems and use the folder door.

Software-instrument (MIDI) tracks leave no audio in `Media/` and so cannot be
imported this way; bounce them to audio first.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import audio
from .config import SR, STEM_NAMES, base_stem, workdir_for

AUDIO_SUFFIXES = {".wav", ".aif", ".aiff", ".caf", ".m4a", ".mp3", ".flac",
                  ".ogg", ".aac", ".opus"}

# A track whose name says "this is the whole song" is the mixdown, not a part
# of it - importing it as a stem would double the whole arrangement.
_MIX_NAMES = ("mixdown", "master", "full mix", "mix", "stereo out", "bounce")

# Which canonical stem a track name means. Matched against the lowercased name;
# the longest matching keyword wins, ties broken by the order here, so "bass
# drum" is a kit mic rather than the bass guitar. German names are in because
# that is what half the tracks in a German practice room get called.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("drums", ("drum", "kick", "snare", "hihat", "hi-hat", "hat", "tom",
               "overhead", "cymbal", "ride", "crash", "kit", "perc", "cajon",
               "schlagzeug", "becken")),
    ("bass", ("bass", "bassgitarre", "808", "sub")),
    ("vocals", ("vocal", "vox", "voc", "voice", "sing", "bgv", "harmony",
                "backing v", "gesang", "stimme", "choir", "chor")),
    ("guitar", ("guitar", "gtr", "gitarre", "git", "strat", "tele",
                "les paul", "telecaster", "stratocaster", "banjo", "mandolin")),
    ("piano", ("piano", "klavier", "keys", "keyboard", "rhodes", "wurli",
               "organ", "orgel", "synth", "pad", "flugel")),
]

# Second pass, tried only when nothing above matched at all. These words name
# an instrument only by implication, and half of them belong to two: "acoustic
# piano" is a piano, but a track called just "Acoustic" is a guitar. Running
# them as a fallback rather than as ordinary keywords is what lets both be
# true, without the scoring having to arbitrate between them.
_WEAK: list[tuple[str, tuple[str, ...]]] = [
    ("guitar", ("acoustic", "akustik", "electric", "e-git", "rhythm", "riff",
                "lead", "solo", "amp", "di")),
    ("vocals", ("mic", "mikro")),
    ("drums", ("room", "raum")),
]

# A weak keyword has to be a whole word, where a strong one may be a substring.
# The strong list is full of stems that only ever occur inside a longer word -
# "git" is there to catch "Gitarre" - but the weak list is short standalone
# words, and matching those loosely is actively wrong: "di" is a DI box, and it
# is also the middle of "VoiceAudio", which is BandLab's default name for a
# recorded track and is how a whole band's vocals came to be imported as
# guitars. Only letters count as the boundary, so "Amp2" and "Mic1" still match.
_WEAK_WORD = {word: re.compile(rf"(?<![a-z]){re.escape(word)}(?![a-z])")
              for _, words in _WEAK for word in words}


@dataclass
class Track:
    """One track on its way in: what the DAW called it, and what it became."""
    name: str                       # the DAW's name for it
    path: Path                      # the audio actually read
    stem: str = ""                  # canonical stem it was assigned
    why: str = ""                   # how that was decided - shown by --dry-run
    extra: list[Path] = field(default_factory=list)   # regions that were dropped


@dataclass
class Session:
    """A multitrack session ready to import, before anything has been written."""
    source: Path
    kind: str                       # garageband | folder
    tracks: list[Track]
    mixdown: Path | None = None     # an existing bounce, if the session had one
    warnings: list[str] = field(default_factory=list)


# --- reading a session --------------------------------------------------------

def _classify(name: str) -> tuple[str, str, int]:
    """The canonical stem a track name means, why, and how sure that is.

    The rank (0 named outright, 1 implied, 2 no idea) is what decides who gets
    the *unsuffixed* name when two tracks land on one instrument: "Electric
    Guitar" should be `guitar` and a track called only "Acoustic" should be the
    `guitar-2`, whatever order the files happen to be read in.
    """
    low = name.lower()
    for rank, rules in enumerate((_RULES, _WEAK)):
        best: tuple[int, int, str, str] | None = None
        for order, (stem, words) in enumerate(rules):
            for word in words:
                hit = word in low if rank == 0 else _WEAK_WORD[word].search(low)
                if hit:
                    cand = (len(word), -order, stem, word)
                    if best is None or cand > best:
                        best = cand
        if best:
            return best[2], f"matched {best[3]!r}", rank
    return "other", "no instrument in the name", 2


def _is_mix(name: str) -> bool:
    """Whether a track name claims to be the whole song rather than one part."""
    low = name.lower().strip()
    return any(low == m or low.startswith(m) or low.endswith(m) for m in _MIX_NAMES)


# GarageBand names region files after their track, then disambiguates repeats
# with `#01` or `.1` - so `Gtr Nik#03.aif` and `Gtr Nik#04.aif` are two regions
# of one track, not two tracks.
_REGION_SUFFIX = re.compile(r"(#\d+|\.\d+)+$")


def _track_name(path: Path) -> str:
    """The track a recorded region belongs to: `Gtr Nik#03.aif` -> `Gtr Nik`."""
    return _REGION_SUFFIX.sub("", path.stem).strip() or path.stem


def _audio_files(root: Path) -> list[Path]:
    """Every audio file under `root`, in a stable order."""
    return sorted((p for p in root.rglob("*")
                   if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
                   and not p.name.startswith(".")),
                  key=lambda p: (str(p.parent).lower(), p.name.lower()))


def _unwrap(root: Path) -> Path:
    """Descend through folders that hold nothing but one folder.

    Both Finder and BandLab wrap a download in a folder named after itself, and
    a zip made by selecting a folder wraps it again. The tracks are what is
    wanted; how many layers of packaging they arrived in is not information.
    """
    for _ in range(4):
        kids = [k for k in root.iterdir() if not k.name.startswith(".")]
        if len(kids) == 1 and kids[0].is_dir():
            root = kids[0]
        else:
            return root
    return root


def _unzip(src: Path) -> Path:
    """Extract a zip of exported tracks and return the folder holding them.

    Extraction is keyed by the zip's identity and reused, because the web
    layer reads a session twice - once to show the mapping, once to import the
    mapping that was corrected - and unpacking a few hundred megabytes of WAV
    a second time to answer the same question is not a cost worth paying.

    It goes to the temp directory rather than beside the zip: the copy is
    scaffolding, `import_session` transcodes every track into `stems/` anyway,
    and writing a folder into someone's Downloads as a side effect of reading
    a file there is not this function's business.
    """
    import hashlib
    import tempfile
    import zipfile

    st = src.stat()
    key = hashlib.sha1(f"{src}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:12]
    dest = Path(tempfile.gettempdir()) / f"musiccopilot-import-{key}"
    done = dest / ".unpacked"
    if not done.exists():
        shutil.rmtree(dest, ignore_errors=True)     # a half-written earlier try
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(src) as zf:
                # `extractall` already refuses to write outside `dest`; the
                # filter is only about __MACOSX, whose AppleDouble twins are
                # named like the real files and would double every track.
                zf.extractall(dest, members=[
                    m for m in zf.namelist()
                    if not m.startswith("__MACOSX/") and "/._" not in f"/{m}"])
        except zipfile.BadZipFile as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise ValueError(f"{src.name} is not a readable zip: {exc}") from exc
        done.touch()
    return _unwrap(dest)


def _group_regions(files: list[Path]) -> tuple[list[Track], list[str]]:
    """Group region files into tracks, keeping the longest region of each.

    A track with several regions is a project that has been edited or recorded
    in more than one take, and without `projectData` there is no way to know
    where the extras belong on the timeline. Laying them end to end would
    invent an arrangement nobody played. The longest region is the one honest
    choice available: for repeated takes of the same song it *is* the take, and
    for a punched-in fix it is everything but the fix - which the warning says
    out loud, so the folder door is one sentence away.
    """
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(_track_name(f), []).append(f)

    tracks, warnings = [], []
    for name, paths in groups.items():
        if len(paths) > 1:
            paths = sorted(paths, key=lambda p: p.stat().st_size, reverse=True)
            warnings.append(
                f"'{name}' has {len(paths)} regions; using the longest "
                f"({paths[0].name}) and ignoring the rest")
        tracks.append(Track(name=name, path=paths[0], extra=list(paths[1:])))
    return tracks, warnings


def _folder_session(root: Path, source: Path | None = None) -> Session:
    """A folder of one-file-per-track exports, read as a session.

    `source` is what the session came *from* when that is not the folder being
    read - a zip, whose name is the song's name and whose path is what
    `sources.json` should record, while the audio is in a temp folder nobody
    should be sent back to.
    """
    files = _audio_files(root)
    if not files:
        raise ValueError(f"no audio files in {root}")
    # A folder export is one file per track already, so each file is a track -
    # no region grouping, and a `#01` in a name here is part of the name.
    tracks = [Track(name=f.stem, path=f) for f in files]
    mixdown = next((t for t in tracks if _is_mix(t.name)), None)
    if mixdown:
        tracks = [t for t in tracks if t is not mixdown]
    return Session(source or root, "folder", tracks,
                   mixdown.path if mixdown else None)


def read_session(src: str | Path) -> Session:
    """Read a `.band` package, a folder of stems, or a zip of them."""
    src = Path(src).expanduser().resolve()
    if src.is_file() and src.suffix.lower() == ".zip":
        return _folder_session(_unzip(src), source=src)
    if not src.is_dir():
        raise ValueError(f"{src} is not a folder, a zip or a .band package - "
                         f"point at the GarageBand project, or at the stems "
                         f"you exported")

    if src.suffix.lower() == ".band":
        media = next((d for d in (src / "Media", src) if d.is_dir()), src)
        files = _audio_files(media)
        if not files:
            raise ValueError(
                f"no audio in {src.name}/Media - GarageBand keeps only *recorded* "
                f"tracks there, so a project of software instruments has nothing "
                f"to import until those are bounced to audio")
        tracks, warnings = _group_regions(files)
        bounce = _audio_files(src / "Output") if (src / "Output").is_dir() else []
        return Session(src, "garageband", tracks, bounce[0] if bounce else None,
                       warnings)

    return _folder_session(src)


# --- assigning stem names -----------------------------------------------------

def assign(session: Session, overrides: dict[str, str] | None = None) -> Session:
    """Give every track a canonical stem name, numbering repeats of one instrument.

    Two guitarists both become guitars - `guitar` and `guitar-2` - rather than
    one guitar and one `other`, or one summed pair. They stay separate stems
    with separate notes and separate tabs, and `config.base_stem` is what keeps
    the suffixed one a guitar everywhere it needs to be one.
    """
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}
    for want in overrides.values():
        if base_stem(want) not in STEM_NAMES:
            raise ValueError(f"unknown stem {want!r}; have {', '.join(STEM_NAMES)} "
                             f"(optionally suffixed, e.g. guitar-2)")

    # An override naming an exact slot ("guitar-2") claims it before anything
    # is auto-numbered, so the numbering fills in around the choice instead of
    # racing it for the same name.
    taken: set[str] = set()
    for track in session.tracks:
        want = overrides.get(track.name.lower())
        if want and want != base_stem(want):
            track.stem, track.why = want, "set by --map"
            taken.add(want)

    # Then everything else, most confident first, so the track that actually
    # names its instrument claims the bare stem and the guesses get numbered
    # around it - not whichever the filesystem happened to list first.
    todo = []
    for track in session.tracks:
        if track.stem:
            continue
        want = overrides.get(track.name.lower())
        todo.append((track, (want, "set by --map", -1) if want
                     else _classify(track.name)))

    for track, (instrument, why, _) in sorted(todo, key=lambda t: t[1][2]):
        name, n = instrument, 1
        while name in taken:
            n += 1
            name = f"{instrument}-{n}"
        if n > 1:
            why += f"; {instrument} was taken"
        track.stem, track.why = name, why
        taken.add(name)
    return session


# --- writing it out -----------------------------------------------------------

def _stereo(y: np.ndarray) -> np.ndarray:
    """A (2, n) view of a mono or stereo signal."""
    return y[:2] if y.ndim > 1 else np.vstack([y, y])


def _mixdown(paths: list[Path], sr: int = SR) -> np.ndarray:
    """Sum the stems into the stereo mix the rest of the pipeline calls the song.

    Peak-normalised because a summed multitrack clips: eight tracks that each
    peaked near 0 dB in the DAW (where the fader, not the file, did the mixing)
    add up to well over it, and a clipped mix would take beat tracking and
    chord detection down with it.
    """
    parts = [_stereo(audio.load(p, sr, mono=False)) for p in paths]
    n = max(p.shape[-1] for p in parts)
    total = np.zeros((2, n), dtype=np.float32)
    for p in parts:
        total[:, : p.shape[-1]] += p
    peak = float(np.abs(total).max())
    return total * (0.89 / peak) if peak > 0 else total


def describe(session: Session) -> str:
    """The table `--dry-run` prints: every track, and what it will become."""
    if not session.tracks:
        return "no tracks"
    w = max(len(t.name) for t in session.tracks)
    rows = [f"  {t.name:<{w}}  ->  {t.stem:<10}  ({t.why})" for t in session.tracks]
    return "\n".join(rows)


def import_session(session: Session, *, name: str | None = None,
                   out: str | Path = ".", log=print) -> Path:
    """Write an assigned session out, and return the song file's path.

    Takes the `Session` rather than the path it came from, so a caller can show
    the mapping (`describe`) and let it be corrected before a byte is written -
    which is the whole of `--dry-run`, and the reason nothing here re-reads the
    source folder a second time.

    Writes `<out>/<name>.wav` (the mixdown, which is what `Song.path` means)
    and `analyzed_songs/<name>/stems/*.wav` beside it, then leaves the rest to
    the normal pipeline: `analyze` on the result skips separation and goes
    straight to tempo, chords and form.
    """
    out = Path(out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    song_path = out / f"{slug(name or session.source.stem)}.wav"

    work = workdir_for(song_path)
    stem_dir = work / "stems"
    stem_dir.mkdir(parents=True, exist_ok=True)
    for old in stem_dir.glob("*.wav"):          # a re-import replaces the set
        old.unlink()

    written: list[Path] = []
    for track in session.tracks:
        y = audio.load(track.path, SR, mono=False)
        written.append(audio.save(stem_dir / f"{track.stem}.wav", y, SR))
        log(f"  {track.name} -> {track.stem}.wav")

    if session.mixdown is not None:
        # The DAW's own bounce beats anything summed here: it carries the mix
        # the band actually balanced, faders and all.
        shutil.copyfile(session.mixdown, song_path)
        log(f"• mix: {session.mixdown.name} (the session's own bounce)")
    else:
        audio.save(song_path, _mixdown(written), SR)
        log(f"• mix: summed from {len(written)} stems")

    (work / "sources.json").write_text(json.dumps({
        "source": str(session.source),
        "kind": session.kind,
        "imported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mixdown": session.mixdown.name if session.mixdown else "summed from stems",
        "warnings": session.warnings,
        "tracks": [{"name": t.name, "file": t.path.name, "stem": t.stem,
                    "why": t.why,
                    **({"ignored": [p.name for p in t.extra]} if t.extra else {})}
                   for t in session.tracks],
    }, indent=1))
    return song_path


# --- changing your mind, after the fact ---------------------------------------
#
# The mapping is shown before the import for a reason, but a name can be wrong
# in a way nobody notices until the analysis comes back - a track called
# "VoiceAudio" landing on `guitar` costs you the lyrics, and the first sign of
# it is a Lyrics tab with nothing in it. Re-importing to fix one row would mean
# uploading the whole multitrack again, so the stems are renamed in place
# instead: the audio is already correct, only the labels on it were wrong.


def _shuffle(folder: Path, suffix: str, moves: dict[str, str]) -> None:
    """Rename a set of files that may be permuting among themselves.

    Two guitarists swapping places is `guitar -> guitar-2` and `guitar-2 ->
    guitar` at once, so every file goes to a temporary name before any takes
    its final one - renaming them one at a time would have the first move
    overwrite the other half of the swap. The temporary name keeps the real
    suffix in the middle (`guitar.wav.moving`), so a crash between the two
    passes leaves files the `stems/*.wav` glob does not pick up as a stem
    called `~guitar`.
    """
    parked = {}
    for old in moves:
        src = folder / f"{old}{suffix}"
        if src.exists():
            parked[old] = src.rename(folder / f"{old}{suffix}.moving")
    for old, src in parked.items():
        src.rename(folder / f"{moves[old]}{suffix}")


def reassign(work: str | Path, mapping: dict[str, str], *, log=print) -> dict:
    """Point an imported song's tracks at different instruments, in place.

    Takes `{track name: stem}` for the rows being corrected - the tracks not
    named keep the instrument they have. Renames the stem wavs, carries or
    drops the notes read off them, deletes whatever downstream stage the change
    invalidates and rewrites `sources.json`. Returns what moved and what has to
    be computed again, so a caller can say so before running the pipeline.

    The mixdown is deliberately not touched: it is the same tracks summed in
    the same proportions, so the song is the same song - only the labels on its
    parts were wrong.
    """
    work = Path(work).expanduser().resolve()
    record = work / "sources.json"
    if not record.exists():
        raise ValueError("these stems were separated, not imported - there are "
                         "no tracks to reassign")
    data = json.loads(record.read_text())
    rows = data.get("tracks") or []
    by_name = {r["name"].lower(): r for r in rows}

    want = {r["name"]: r["stem"] for r in rows}
    chosen: set[str] = set()
    for name, stem in mapping.items():
        row = by_name.get(str(name).lower())
        if row is None:
            raise ValueError(f"no track named {name!r}; have "
                             f"{', '.join(r['name'] for r in rows)}")
        if base_stem(stem) not in STEM_NAMES:
            raise ValueError(f"unknown stem {stem!r}; have {', '.join(STEM_NAMES)} "
                             f"(optionally suffixed, e.g. guitar-2)")
        want[row["name"]] = stem
        chosen.add(row["name"])

    # The same rule the import uses: a track someone named outright claims its
    # slot, and everything else is numbered around it. So moving one of two
    # guitars to `vocals` renumbers the other rather than colliding with it,
    # and the names stay dense - a lone `guitar-2` with no `guitar` reads as a
    # second guitarist who is not there.
    order = ([r for r in rows if r["name"] in chosen]
             + [r for r in rows if r["name"] not in chosen])
    taken: set[str] = set()
    final: dict[str, str] = {}
    for row in order:
        pick = want[row["name"]]
        instrument = base_stem(pick)
        # An explicit `guitar-2` is honoured as asked; anything inherited or
        # bare numbers up from the instrument itself.
        name = pick if row["name"] in chosen and pick != instrument else instrument
        n = 1
        while name in taken:
            n += 1
            name = f"{instrument}-{n}"
        final[row["name"]] = name
        taken.add(name)

    moves = {r["stem"]: final[r["name"]] for r in rows if final[r["name"]] != r["stem"]}
    if not moves:
        return {"moves": {}, "kept": [], "recompute": []}

    # A track that only changed number is the same instrument on the same
    # audio, so what was read off it still stands. A change of instrument is
    # not: the pitch window and the monophonic/polyphonic split are both keyed
    # by instrument, so those notes were read with the wrong model and are
    # dropped rather than relabelled.
    kept = {old: new for old, new in moves.items() if base_stem(old) == base_stem(new)}
    dropped = [old for old in moves if old not in kept]

    notes = work / "notes"
    backends = json.loads((work / "note_backends.json").read_text()) \
        if (work / "note_backends.json").exists() else {}
    for old in dropped:
        (notes / f"{old}.json").unlink(missing_ok=True)
        backends.pop(old, None)
    _shuffle(work / "stems", ".wav", moves)
    _shuffle(notes, ".json", kept)
    # Rewritten even when it comes out empty: leaving the old file behind would
    # keep claiming an engine for a stem name nothing answers to any more.
    (work / "note_backends.json").write_text(
        json.dumps({kept.get(s, s): b for s, b in backends.items()}, indent=1))

    # What the labels were load-bearing for, and therefore what has to be read
    # again. Only the *instrument* matters to most of it: the chord track comes
    # from `audio.harmonic_bed`, which is the stems minus drums and vocals, so
    # a track crossing that line means chords were detected over different
    # audio; lyrics are Whisper on whatever `vocals` is. A pure renumber leaves
    # both alone - it is the same files in the same groups. The form reads stem
    # *names* (which one is a part's lead), so it goes either way, and the
    # snippets and the chart are cut from the form.
    recompute = ["form.json", "chart.md", "snippets"]
    if any(base_stem(old) != base_stem(new) for old, new in moves.items()):
        recompute[:0] = ["analysis.json", "lyrics.json"]
    for name in recompute:
        target = work / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)

    for old, new in moves.items():
        log(f"  {old} -> {new}" + ("" if old in kept else " (notes re-read)"))

    data["tracks"] = [{**r, "stem": final[r["name"]],
                       "why": "set by hand" if r["name"] in chosen else r.get("why", "")}
                      for r in rows]
    data["reassigned"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record.write_text(json.dumps(data, indent=1))
    return {"moves": moves, "kept": sorted(kept), "recompute": recompute}


# --- finding GarageBand's projects on this Mac --------------------------------
#
# Scriptum runs on the machine in the practice room (CLAUDE.md: "The mic is the
# server's"), which is the same machine GarageBand is open on. So "import the
# project I have open" is a question the *server* can answer, and none of this
# needs the browser to upload a thing.
#
# Two macOS permissions are in play, and they fail differently:
#
#  * **Reading `~/Music/GarageBand`** is what actually gates the feature - the
#    folder is TCC-protected, and opening the package raises PermissionError
#    even though the path is perfectly well known. `readable()` reports that as
#    a condition to fix rather than letting it surface as a stack trace, and
#    `_tcc_hint` says *whose* condition it is: TCC grants nothing to `python`,
#    it grants to the application that launched it, so the toggle is filed
#    under the terminal's name and not under Scriptum's. The Files and Folders
#    pane cannot be added to by hand - its rows appear only after an app has
#    asked and been answered - so the hint offers the two routes that do not
#    depend on a prompt ever appearing.
#  * **Sending Apple Events to GarageBand** is optional. It is the authoritative
#    answer (`path of every document`), but it needs an Automation grant and
#    errors -1743 without one, so it is tried *second* and its failure is not
#    an error - `lsof` has usually already answered.
#
# `lsof` first is the deliberate ordering: it reads the process file table
# rather than the files, so it works with no permissions at all and keeps
# working when the Apple Events grant is missing or was denied.

def _band_root(path: str) -> str | None:
    """The `.band` package a path is inside, or None: a project is a folder, and
    what `lsof` reports is some file *within* it (`.../x.band/projectData`)."""
    marker = ".band/"
    i = path.find(marker)
    if i != -1:
        return path[: i + len(marker) - 1]
    return path if path.endswith(".band") else None


def _lsof_projects() -> list[Path]:
    """Projects GarageBand currently holds files open in. No permissions needed."""
    import subprocess
    try:
        # -F n prints one field per line, so a path containing spaces survives
        # intact where splitting lsof's columns would tear it in half.
        out = subprocess.run(["lsof", "-c", "GarageBand", "-F", "n"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = {r for line in out.splitlines() if line.startswith("n")
             if (r := _band_root(line[1:]))}
    return sorted((Path(p) for p in found), key=lambda p: str(p).lower())


def _applescript_projects() -> list[Path]:
    """Same question asked of GarageBand itself; [] if Automation is not granted.

    GarageBand's dictionary is only the Standard Suite, but that is enough:
    `document` carries a `path`, which is exactly the one fact needed here.
    """
    import subprocess
    script = ('tell application "GarageBand" to get path of every document')
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:           # -1743 = no Automation grant; not an error
        return []
    return [Path(s.strip()) for s in p.stdout.strip().split(",") if s.strip()]


def open_projects() -> list[Path]:
    """Every GarageBand project currently open, most reliable source first."""
    seen, out = set(), []
    for finder in (_lsof_projects, _applescript_projects):
        for path in finder():
            if path.suffix.lower() == ".band" and str(path) not in seen:
                seen.add(str(path))
                out.append(path)
    return out


# Where GarageBand puts projects by default, plus the places people move them to.
_PROJECT_DIRS = ("~/Music/GarageBand", "~/Documents", "~/Desktop",
                 "~/Music", "~/Downloads")


def recent_projects(limit: int = 25) -> list[Path]:
    """`.band` projects lying around this Mac, newest first.

    A shallow scan of the handful of places they actually live, not a disk
    search: Spotlight does not index package *names* reliably, and a recursive
    walk of a home directory to populate a menu is not a trade worth making.
    """
    out: list[Path] = []
    for d in _PROJECT_DIRS:
        root = Path(d).expanduser()
        try:
            if not root.is_dir():
                continue
            for p in root.iterdir():
                if p.suffix.lower() == ".band":
                    out.append(p)
        except (PermissionError, OSError):
            continue                # TCC, or a folder that is not there
    out.sort(key=lambda p: _mtime(p), reverse=True)
    return out[:limit]


def _mtime(p: Path) -> float:
    """Modification time, or 0 if it cannot be read (a TCC-blocked package)."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _ancestry() -> list[str]:
    """This process and everything that launched it, innermost first."""
    import os
    import subprocess
    out: list[str] = []
    pid = os.getpid()
    for _ in range(12):
        try:
            row = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True,
                                 timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        if not row:
            break
        ppid, _, comm = row.partition(" ")
        out.append(comm.strip())
        try:
            pid = int(ppid)
        except ValueError:
            break
        if pid <= 1:
            break
    return out


def responsible_app() -> str:
    """The application macOS holds responsible for what this process does.

    A permission is never granted to `python`; it is granted to the app that
    launched it - Terminal, iTerm, VS Code - and that is the name the toggle is
    filed under. Someone told to "grant Scriptum access" will look for Scriptum
    in a list it can never appear in.

    The *outermost* ancestor is the one that counts, and its first `.app`
    component with it: VS Code's Python runs under `Code Helper (Plugin).app`
    nested inside `Visual Studio Code.app`, and only the outer bundle is a row
    in System Settings.
    """
    for comm in reversed(_ancestry()):
        for part in Path(comm).parts:
            if part.endswith(".app"):
                return part[:-4]
    return ""


def _tcc_hint(path: Path) -> str:
    """What to do about a folder macOS will not let this process read."""
    app = responsible_app()
    who = f"'{app}'" if app else "the app running Scriptum"
    return (
        f"macOS is blocking {who} from reading {path.parent.name}/. Files and "
        f"Folders cannot be added to by hand - its rows appear only after an "
        f"app has asked - so either copy the project out of {path.parent.name}/ "
        f"in Finder (drag it to Downloads and open it from there, which needs "
        f"no permission at all), or give {who} Full Disk Access under System "
        f"Settings > Privacy & Security and start Scriptum again.")


def readable(path: str | Path) -> tuple[bool, str]:
    """Whether this process can actually open a project, and what to do if not.

    `~/Music/GarageBand` is TCC-protected on current macOS, so the common
    failure is knowing the path perfectly well and still being refused it. That
    is a setting to change, not a bug, and it deserves to be reported as one.
    """
    path = Path(path).expanduser()
    if not path.exists():
        return False, f"{path.name} is not there any more"
    try:
        if path.is_file():                  # a zip of exported tracks
            path.open("rb").close()
        else:
            next(iter(path.iterdir()), None)
    except PermissionError:
        return False, _tcc_hint(path)
    except OSError as exc:
        return False, f"cannot read {path.name}: {exc}"
    return True, ""


def reveal(path: str | Path) -> None:
    """Show a path in Finder - which works even when this process cannot read it.

    `open -R` hands the path to LaunchServices and *Finder* opens it, so a
    TCC-blocked project can still be pointed at. That is the whole point: the
    fix for the block is a drag in Finder, and the app cannot do that drag for
    anyone - it is not allowed to copy what it is not allowed to read.
    """
    import subprocess
    try:
        subprocess.run(["open", "-R", str(Path(path).expanduser())],
                       capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def slug(name: str) -> str:
    """A filesystem-safe song id, matching `scriptum.library.slugify`'s shape so
    an imported song addresses the same in the browser as on the command line.

    Public because the web layer needs to know a song's id *before* the import
    runs, to key the job by the row it will eventually create."""
    import unicodedata
    s = unicodedata.normalize("NFKD", Path(name).stem).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-") or "session"
