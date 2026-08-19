"""Stable identity for a media file.

Two different questions get two different answers here, and conflating them is
the classic caching bug:

``asset_id`` answers *"is this the same file I saw before?"* -- derived from the
normalised path only, so a re-encoded or renamed-in-place file keeps its ID and
its transcript stays attached to it.

``Fingerprint`` answers *"has the content changed?"* -- size, mtime and a cheap
content hash. This is what a cache key is built from, so a re-export to the same
filename correctly misses the cache.

The content hash reads the head and tail of the file rather than all of it. A
40-minute 4K capture is 20+ GB; hashing it fully would cost more than the
analysis being cached. Head+tail+size catches every realistic case here
(re-encode, re-render, truncated transfer) because a video container's header
carries duration, timestamps and index offsets that shift whenever the content
does.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from editing.errors import FootageError

#: Bytes read from each end of the file for the content hash.
_HASH_CHUNK = 1 << 20  # 1 MiB

#: Files at or below this size are hashed in full -- for a short clip the
#: head/tail windows would overlap anyway.
_FULL_HASH_LIMIT = 4 * _HASH_CHUNK


def normalise_path(path: str | os.PathLike) -> str:
    """Absolute, symlink-resolved, case-folded on Windows.

    Case folding is Windows-only on purpose: ``C:\\Foo\\a.mp4`` and
    ``c:\\foo\\A.MP4`` are the same file there and must share an ID, while on
    Linux they are genuinely two files.
    """
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve()
    except (OSError, RuntimeError):
        resolved = resolved.absolute()
    text = str(resolved)
    return text.lower() if os.name == "nt" else text


def asset_id_for(path: str | os.PathLike) -> str:
    """The stable ID a file keeps across analyses."""
    digest = hashlib.sha256(normalise_path(path).encode("utf-8")).hexdigest()
    return f"a_{digest[:16]}"


@dataclass(frozen=True)
class Fingerprint:
    """Everything that says "this exact content, at this path"."""

    path: str
    asset_id: str
    size_bytes: int
    mtime: float
    content_hash: str

    def to_dict(self) -> dict:
        return asdict(self)

    def cache_key_part(self) -> dict:
        """The identity fields that belong in a cache key.

        ``path`` is included so two identical files in different folders keep
        separate cache entries -- their Premiere mapping and transcripts differ
        even when their pixels do not.
        """
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "mtime": round(self.mtime, 3),
            "content_hash": self.content_hash,
        }


def content_hash(path: str | os.PathLike, *, size_bytes: Optional[int] = None) -> str:
    """Cheap content hash: size + head chunk + tail chunk."""
    target = Path(path)
    try:
        total = size_bytes if size_bytes is not None else target.stat().st_size
    except OSError as exc:
        raise FootageError(
            f"Cannot stat {target}", detail={"reason": str(exc)}
        ) from exc

    digest = hashlib.sha256()
    digest.update(str(total).encode("ascii"))
    try:
        with open(target, "rb") as handle:
            if total <= _FULL_HASH_LIMIT:
                while True:
                    chunk = handle.read(_HASH_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
            else:
                digest.update(handle.read(_HASH_CHUNK))
                handle.seek(max(0, total - _HASH_CHUNK))
                digest.update(handle.read(_HASH_CHUNK))
    except OSError as exc:
        raise FootageError(
            f"Cannot read {target}",
            hint="The file may be open in another application, or on a "
                 "disconnected drive.",
            detail={"reason": str(exc)},
        ) from exc
    return digest.hexdigest()[:32]


def fingerprint(path: str | os.PathLike, *, hash_content: bool = True) -> Fingerprint:
    """Measure a file. ``hash_content=False`` skips the read for a fast scan."""
    target = Path(path)
    try:
        stat = target.stat()
    except OSError as exc:
        raise FootageError(
            f"Media file not found: {target}",
            hint="Check the path, and that the drive holding the footage is "
                 "connected.",
            detail={"reason": str(exc)},
        ) from exc

    return Fingerprint(
        path=normalise_path(target),
        asset_id=asset_id_for(target),
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        content_hash=content_hash(target, size_bytes=stat.st_size)
        if hash_content else "",
    )
