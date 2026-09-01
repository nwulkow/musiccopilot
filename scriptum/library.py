"""The song library: where uploads live and how they are addressed.

A song's id is the stem of its audio file, which is also the name of its
`analyzed_songs/<id>/` cache folder - so the web app and the CLI address the
same song the same way, and a song analysed from the terminal shows up in the
browser (and vice versa) with its cache intact.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path

from musiccopilot.audio import STEM_EXT
from musiccopilot.config import workdir_for

AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
                  ".aiff", ".aif", ".wma"}


def library_root() -> Path:
    """Where uploads are stored. `SCRIPTUM_LIBRARY` overrides it; the default
    is the working directory, so `crystallize.mp3` and its existing
    `analyzed_songs/crystallize/` cache are picked up with no migration."""
    import os
    root = Path(os.getenv("SCRIPTUM_LIBRARY", ".")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def slugify(name: str) -> str:
    """A filesystem-safe id from an uploaded file name.

    Kept close to the original: the id is the folder name a user will see in
    `analyzed_songs/`, and matching it to the file they uploaded is worth more
    than making it short.
    """
    stem = Path(name).stem
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^\w\s-]", "", stem).strip().lower()
    stem = re.sub(r"[\s_]+", "-", stem)
    return re.sub(r"-{2,}", "-", stem).strip("-") or "song"


def find(song_id: str) -> Path | None:
    """The audio file for an id, or None. Ids are matched against file stems,
    so this never walks outside the library root."""
    if not song_id or "/" in song_id or "\\" in song_id or song_id.startswith("."):
        return None
    root = library_root()
    for p in sorted(root.iterdir()):
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES and p.stem == song_id:
            return p
    return None


def unique_path(root: Path, slug: str, suffix: str) -> tuple[Path, str]:
    """A free `<slug><suffix>` in `root`, numbering collisions rather than
    overwriting - two different songs may well share a title."""
    candidate, n = root / f"{slug}{suffix}", 1
    while candidate.exists():
        n += 1
        candidate = root / f"{slug}-{n}{suffix}"
    return candidate, candidate.stem


def stages(work: Path) -> dict:
    """Which pipeline stages are cached for a song - one flag per file the
    pipeline writes, which is exactly what the client shows as progress."""
    notes = work / "notes"
    stem_dir = work / "stems"
    has_stems = stem_dir.is_dir() and (any(stem_dir.glob("*.wav"))
                                       or any(stem_dir.glob(f"*{STEM_EXT}")))
    return {
        "stems": has_stems,
        "analysis": (work / "analysis.json").exists(),
        "notes": notes.is_dir() and any(notes.glob("*.json")),
        "lyrics": (work / "lyrics.json").exists(),
        "form": (work / "form.json").exists(),
        # A snippet is cut on demand from the form's own bounds (see
        # `serialize`/`app.media_snippet`), not pre-rendered to disk any more
        # - so "done" means the form exists, the same as the `form` flag.
        # `musiccopilot snippets` can still write files for someone who wants
        # them, which the badge does not try to reflect.
        "snippets": (work / "form.json").exists(),
        "chart": (work / "chart.md").exists(),
        "llm_notes": (work / "llm_notes.txt").exists(),
    }


def entry(path: Path) -> dict:
    """One library row: enough to list and sort songs without loading a cache."""
    work = workdir_for(path)
    st = stages(work)
    return {
        "id": path.stem,
        "filename": path.name,
        "title": path.stem.replace("-", " ").replace("_", " "),
        "size": path.stat().st_size,
        "added": path.stat().st_mtime,
        "analyzed": st["analysis"],
        "stages": st,
    }


def listing() -> list[dict]:
    """Every audio file in the library, newest first."""
    root = library_root()
    out = [entry(p) for p in root.iterdir()
           if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES]
    return sorted(out, key=lambda e: e["added"], reverse=True)


def remove(song_id: str, drop_cache: bool = True) -> bool:
    """Delete a song's audio and, by default, its analysis cache."""
    path = find(song_id)
    if path is None:
        return False
    work = workdir_for(path)
    path.unlink(missing_ok=True)
    if drop_cache and work.is_dir():
        shutil.rmtree(work, ignore_errors=True)
    return True
