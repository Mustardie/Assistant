"""Finding the folders in a library that are worth a run.

A "candidate" is a folder that **directly contains video files**. Nested
folders are candidates in their own right; a parent whose videos all live in
sub-folders is not one, because running it would process the same footage
twice under a different name.

Nothing here probes, decodes or opens a file. Discovery over a library of forty
episodes has to be instant or nobody will type ``--dry-run`` first, and the
only questions it answers are "is there footage here" and "has this folder been
run before".
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

from editing.auto import store as auto_store
from editing.batch.schema import BatchCandidate
from editing.config import VIDEO_EXTENSIONS, EditingConfig
from editing.errors import FootageError

logger = logging.getLogger("nova.editing.batch.discover")

#: Folder names that are never footage. Every one of these is somewhere this
#: system or an editor writes, and walking into them wastes time at best and
#: finds a proxy render at worst.
SKIP_FOLDERS = frozenset({
    "data", "cache", "render", "renders", "proxy", "proxies", "auto",
    "adobe premiere pro auto-save", "adobe premiere pro video previews",
    "adobe premiere pro audio previews", "node_modules", "__pycache__",
    ".git", ".venv", "venv", "frames", "transcripts", "artifacts",
})


def is_skippable(name: str) -> bool:
    lowered = (name or "").strip().lower()
    return lowered.startswith(".") or lowered in SKIP_FOLDERS


def find_candidates(
    root: str | Path,
    *,
    recursive: bool = True,
    extensions: Sequence[str] = VIDEO_EXTENSIONS,
    config: Optional[EditingConfig] = None,
    limit: int = 0,
) -> list[BatchCandidate]:
    """Every folder under ``root`` holding footage, in a stable order.

    When ``config`` is given, each candidate also carries the runs that already
    exist over it, which is what lets the batch skip finished work without
    re-deriving anything.
    """
    base = Path(root).expanduser()
    if not base.exists():
        raise FootageError(
            f"Batch root not found: {base}",
            hint="Point --root at the folder holding your episode folders.",
        )
    if base.is_file():
        raise FootageError(
            f"Batch root is a file, not a folder: {base}",
            hint="Use `auto run --folder` for a single clip, or point --root "
                 "at the folder above it.",
        )

    wanted = {ext.lower() for ext in extensions}
    existing = _existing_runs(config)
    found: list[BatchCandidate] = []

    for folder in _walk(base, recursive=recursive):
        files = [
            path for path in sorted(folder.iterdir(), key=lambda p: p.name)
            if path.is_file()
            and path.suffix.lower() in wanted
            and not path.name.startswith(".")
        ]
        if not files:
            continue
        found.append(BatchCandidate(
            folder=str(folder),
            label=_label(base, folder),
            video_files=len(files),
            total_bytes=_total_bytes(files),
            existing_runs=existing.get(_key(folder), []),
        ))
        if limit and len(found) >= limit:
            break
    return found


def _walk(base: Path, *, recursive: bool):
    """The base folder, then every sub-folder worth looking in."""
    yield base
    if not recursive:
        return
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            children = sorted(
                (path for path in current.iterdir() if path.is_dir()),
                key=lambda path: path.name.lower(),
            )
        except OSError as exc:  # noqa: BLE001 - an unreadable folder is a fact
            logger.debug("Could not read %s: %s", current, exc)
            continue
        for child in children:
            if is_skippable(child.name):
                continue
            yield child
            stack.append(child)


def _label(base: Path, folder: Path) -> str:
    """A short name for a folder, relative to the batch root."""
    try:
        relative = folder.relative_to(base)
    except ValueError:
        return folder.name
    text = str(relative)
    return folder.name if text in ("", ".") else text


def _total_bytes(files: Sequence[Path]) -> int:
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _existing_runs(config: Optional[EditingConfig]) -> dict:
    """Runs already on disk, keyed by the folder they were over.

    Read once for the whole batch rather than per folder: ``list_runs`` walks
    the runs directory, and doing that forty times to answer forty independent
    questions is the kind of thing that makes a dry run feel slow.
    """
    if config is None:
        return {}
    out: dict = {}
    try:
        runs = auto_store.list_runs(config, limit=500)
    except Exception as exc:  # noqa: BLE001 - no runs yet is the normal case
        logger.debug("Could not list existing runs: %s", exc)
        return {}
    for entry in runs:
        folder = entry.get("folder") or ""
        if not folder:
            continue
        out.setdefault(_key(folder), []).append(entry)
    return out


def _key(folder) -> str:
    """A comparable form of a path.

    ``normalise_path`` because Windows will hand back the same folder as
    ``E:\\Clips\\Ep1`` and ``e:/clips/ep1`` depending on who typed it, and a
    batch that ran the same episode twice for that reason would be a bad
    batch. It case-folds on Windows only, which is the right rule: two paths
    differing in case are one folder there and two folders on Linux.
    """
    try:
        from editing.fingerprint import normalise_path

        return str(normalise_path(str(folder)))
    except Exception:  # noqa: BLE001 - a bad path is still a key
        return str(folder).replace("\\", "/").rstrip("/")


def runs_for(config: EditingConfig, folder: str) -> list:
    """Every run over one folder, newest first."""
    return _existing_runs(config).get(_key(folder), [])
