"""Content-addressed cache for the expensive steps.

Visual analysis of one 40-minute recording is hundreds of model calls. Doing it
twice because the user re-ran the CLI is the single worst failure mode this
layer could have, so caching is not an optimisation here -- it is the feature
that makes the CLI usable.

A key is the SHA-256 of a canonical JSON document containing:

* the cache ``kind`` (``visual`` / ``transcript`` / ``probe`` / ``motion``)
* the file fingerprint (path, size, mtime, content hash)
* the model name
* the sampling configuration
* the schema version

Every one of those genuinely changes the result, so a hit means the stored
value is the value this run would have computed. Nothing is keyed on wall-clock
time; entries live until deleted.

Entries are one JSON file each rather than a database. Debuggability is the
point: when the model says something strange, the user can open the exact file
that produced it, and delete it to force a re-analysis of that one window.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from editing.config import SCHEMA_VERSION

logger = logging.getLogger("nova.editing.cache")


def canonical_key(payload: dict) -> str:
    """Hash a key document so equal documents always hash equal.

    ``sort_keys`` plus ``default=str`` makes dict ordering and stray Path
    objects irrelevant, which matters because these documents are assembled
    from several layers.
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class CacheStats:
    """Hit/miss counters. Reported by the CLI so a slow run can be explained."""

    hits: int = 0
    misses: int = 0
    writes: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def to_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate": round(self.hit_rate, 3),
        }


@dataclass
class Cache:
    """A JSON-file cache rooted at ``root``.

    ``enabled=False`` turns every lookup into a miss and every write into a
    no-op, which is how ``--no-cache`` and most tests run.
    """

    root: Path
    enabled: bool = True
    stats: CacheStats = field(default_factory=CacheStats)

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    # -- key building ---------------------------------------------------

    def key(self, kind: str, **parts: Any) -> str:
        """Build a key for ``kind`` from arbitrary keyed parts."""
        return canonical_key({
            "kind": kind,
            "schema_version": SCHEMA_VERSION,
            "parts": parts,
        })

    def path_for(self, kind: str, key: str) -> Path:
        # Sharded by the first two hex characters: a long session can produce
        # tens of thousands of window entries and flat directories that size
        # are slow to list on Windows.
        return self.root / kind / key[:2] / f"{key}.json"

    # -- read/write -----------------------------------------------------

    def get(self, kind: str, key: str) -> Optional[dict]:
        """Return the cached payload, or None on a miss. Never raises."""
        if not self.enabled:
            self.stats.misses += 1
            return None
        target = self.path_for(kind, key)
        if not target.exists():
            self.stats.misses += 1
            return None
        try:
            with open(target, "r", encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # A half-written entry (power loss mid-write on an older version)
            # must degrade to a miss, never to a crash.
            logger.warning("Discarding unreadable cache entry %s: %s", target, exc)
            self._unlink(target)
            self.stats.misses += 1
            return None

        if not isinstance(entry, dict) or "value" not in entry:
            self._unlink(target)
            self.stats.misses += 1
            return None

        self.stats.hits += 1
        return entry["value"]

    def put(self, kind: str, key: str, value: Any, *, meta: Optional[dict] = None) -> None:
        """Store ``value``. Cache failures are logged, never raised."""
        if not self.enabled:
            return
        target = self.path_for(kind, key)
        entry = {
            "kind": kind,
            "key": key,
            "schema_version": SCHEMA_VERSION,
            "stored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "meta": meta or {},
            "value": value,
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(target, entry)
            self.stats.writes += 1
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not write cache entry %s: %s", target, exc)

    def get_or_compute(self, kind: str, key: str, compute, *,
                       meta: Optional[dict] = None):
        """Classic memo. ``compute`` runs only on a miss.

        A raising ``compute`` stores nothing, so a transient model failure does
        not get baked into the cache and returned forever after.
        """
        cached = self.get(kind, key)
        if cached is not None:
            return cached
        value = compute()
        if value is not None:
            self.put(kind, key, value, meta=meta)
        return value

    # -- maintenance ----------------------------------------------------

    def clear(self, kind: Optional[str] = None) -> int:
        """Delete entries (of one kind, or all). Returns how many went."""
        base = self.root / kind if kind else self.root
        if not base.exists():
            return 0
        removed = 0
        for entry in base.rglob("*.json"):
            if self._unlink(entry):
                removed += 1
        return removed

    def info(self) -> dict:
        """Entry counts and total size per kind, for `cache info`."""
        kinds: dict[str, dict] = {}
        total_bytes = 0
        if self.root.exists():
            for entry in self.root.rglob("*.json"):
                # <root>/<kind>/<shard>/<key>.json
                try:
                    kind = entry.relative_to(self.root).parts[0]
                except (ValueError, IndexError):
                    kind = "unknown"
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                bucket = kinds.setdefault(kind, {"entries": 0, "bytes": 0})
                bucket["entries"] += 1
                bucket["bytes"] += size
                total_bytes += size
        return {
            "root": str(self.root),
            "enabled": self.enabled,
            "kinds": kinds,
            "total_entries": sum(k["entries"] for k in kinds.values()),
            "total_bytes": total_bytes,
            "stats": self.stats.to_dict(),
        }

    # -- internals ------------------------------------------------------

    @staticmethod
    def _atomic_write(target: Path, entry: dict) -> None:
        """Write via a temp file in the same directory, then replace.

        Two analysis workers can be writing different windows concurrently;
        a partially written file that another process reads as a hit would be
        worse than no cache at all.
        """
        handle_fd, temp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".tmp-", suffix=".json"
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False, indent=2, default=str)
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _unlink(target: Path) -> bool:
        try:
            target.unlink()
            return True
        except OSError:
            return False


def build_cache(config, *, enabled: bool = True) -> Cache:
    """Cache rooted at the configured cache directory."""
    return Cache(root=config.cache_dir, enabled=enabled)
