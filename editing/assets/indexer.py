"""Scanning local folders into an asset index.

Everything a placement decision needs, gathered once and written to JSON so the
matching pass is pure and fast. The index is derived data: delete it and
re-index, and nothing is lost.

Four sources of truth, in increasing priority:

1. **the folder** — ``sfx/impacts/heavy/`` gives a category and two tags;
2. **the filename** — ``whoosh_fast_01.wav`` gives ``whoosh`` and ``fast``;
3. **the probe** — ffprobe gives a real duration, when ffprobe exists;
4. **the sidecar** — a person typed it, so it wins over all of the above.

The degradation rules matter more than the happy path here:

* **No ffmpeg is normal, not an error.** Duration stays ``None`` and every
  duration-dependent match rule becomes "cannot tell", which is different from
  "does not fit". The library records that it could not probe, once, rather
  than once per file.
* **A file that vanished is marked, not dropped.** Re-indexing keeps the record
  with ``missing: True`` so a plan referring to it explains itself instead of
  silently losing a placement.
* **Nothing recurses into a build tree.** A user who points this at a project
  root by accident should wait a second, not an hour.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional

from editing.assets import library as library_module
from editing.assets import sidecar as sidecar_module
from editing.assets.schema import (
    AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, MOGRT_EXTENSIONS, SUPPORTED_EXTENSIONS,
    VIDEO_EXTENSIONS, AssetItem, AssetLibrary, AssetTag, asset_id_for,
    merge_tags,
)
from editing.config import EditingConfig

logger = logging.getLogger("nova.editing.assets.indexer")

#: Words split out of a filename and kept as tags. Two characters minimum so
#: separators and stray letters do not become tags.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z']+")

#: Filename tokens that carry no information. Dropping them keeps the tag sets
#: small enough to read in a report.
STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "final", "new", "copy", "version",
    "master", "mix", "edit", "wav", "mp", "audio", "sound", "sfx", "file",
    "untitled", "export", "render", "out", "raw", "temp", "test",
})

#: Filename hints that set a flag rather than just a tag.
LOOP_HINTS = ("loop", "loopable", "seamless", "bed", "cycle")
HIGH_INTENSITY_HINTS = ("impact", "boom", "slam", "hit", "crash", "heavy",
                        "hard", "big", "explosion", "stinger", "scream")
LOW_INTENSITY_HINTS = ("soft", "subtle", "quiet", "gentle", "light", "calm",
                       "ambient", "drone", "pad", "background")

#: Bytes read from each end of a file for the content hash. Same reasoning as
#: ``editing.fingerprint``: a 200 MB wav should not be hashed in full to notice
#: that it changed.
_HASH_CHUNK = 1 << 18


def index_library(
    config: EditingConfig,
    *,
    root: Optional[str] = None,
    previous: Optional[AssetLibrary] = None,
    probe_durations: bool = True,
    say=None,
) -> AssetLibrary:
    """Scan ``root`` and return a fresh library.

    ``previous`` lets an unchanged file keep its probed duration without paying
    for ffprobe again -- the expensive part of indexing a large library is the
    probing, and a fingerprint match means the answer cannot have changed.
    """
    say = say or (lambda message: None)
    target = library_module.resolve_root(config, root)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    result = AssetLibrary(root=str(target), generated_at=now)

    if not target.exists():
        result.warnings.append(
            f"The asset library root {target} does not exist. Run "
            "`assets init` to create it, or pass --root."
        )
        return result

    known = {item.asset_id: item for item in (previous.items if previous else [])}
    seen: set = set()
    prober = _Prober(config, enabled=probe_durations)

    for path in _walk(target, result):
        asset_id = asset_id_for(str(path))
        seen.add(asset_id)
        item = _index_one(path, target, asset_id, known.get(asset_id), prober,
                          now, result)
        if item is not None:
            result.items.append(item)

    # Anything indexed before and not seen now has gone. Keep the record.
    for asset_id, item in known.items():
        if asset_id in seen:
            continue
        item.missing = True
        item.indexed_at = now
        result.items.append(item)

    result.items.sort(key=lambda item: (item.category, item.filename.lower()))
    _summarise(result, prober, say)
    return result


def _walk(root: Path, result: AssetLibrary) -> Iterable[Path]:
    """Every supported file under ``root``, skipping the obvious traps."""
    root = Path(root)
    for current, directories, filenames in os.walk(root):
        here = Path(current)
        try:
            depth = len(here.relative_to(root).parts)
        except ValueError:  # pragma: no cover - os.walk stays under root
            depth = 0

        if depth >= library_module.MAX_DEPTH:
            directories[:] = []
        else:
            directories[:] = [
                name for name in directories
                if not library_module.should_skip_directory(name)
            ]

        for filename in sorted(filenames):
            path = here / filename
            suffix = path.suffix.lower()
            if filename.endswith(sidecar_module.SUFFIX):
                continue
            if filename.startswith("."):
                continue
            if suffix in (".md", ".txt", ".json"):
                continue
            if suffix not in SUPPORTED_EXTENSIONS:
                result.skipped.append({
                    "path": str(path),
                    "reason": f"'{suffix or 'no extension'}' is not a supported "
                              "asset type",
                })
                continue
            yield path


def _index_one(
    path: Path,
    root: Path,
    asset_id: str,
    previous: Optional[AssetItem],
    prober: "_Prober",
    now: str,
    result: AssetLibrary,
) -> Optional[AssetItem]:
    try:
        stat = path.stat()
    except OSError as exc:
        result.skipped.append({"path": str(path), "reason": f"unreadable: {exc}"})
        return None

    fingerprint = _fingerprint(path, stat.st_size)
    unchanged = (
        previous is not None
        and previous.fingerprint == fingerprint
        and previous.duration is not None
    )

    category, folder_tags = library_module.category_for(path, root)
    media_type = media_type_for(path)

    item = AssetItem(
        asset_id=asset_id,
        path=str(path),
        filename=path.name,
        media_type=media_type,
        category=category,
        size_bytes=stat.st_size,
        fingerprint=fingerprint,
        indexed_at=now,
    )

    tags = merge_tags(
        [AssetTag(name=name, source="folder", confidence=0.8)
         for name in folder_tags],
        tags_from_filename(path.name),
    )

    # -- inference from the name, before the sidecar overrides it ---------
    lowered = path.stem.lower()
    item.loopable = any(hint in lowered for hint in LOOP_HINTS)
    item.intensity = _intensity_from(lowered, category)

    # -- duration ---------------------------------------------------------
    if unchanged and previous is not None:
        item.duration = previous.duration
        item.loudness_db = previous.loudness_db
    elif media_type in ("audio", "video"):
        item.duration = prober.duration(path)

    # -- sidecar wins ------------------------------------------------------
    parsed = sidecar_module.load(path)
    item.has_sidecar = parsed.found
    if parsed.needs_review:
        item.needs_review = True
        item.safe_for_auto = False
        item.review_reason = f"{Path(parsed.path).name} {parsed.error}"
        result.warnings.append(
            f"{path.name}: sidecar {parsed.error} -- indexed, but held out of "
            "automatic placement until it is fixed."
        )
    elif parsed.ok:
        tags = merge_tags(tags, parsed.get("tags", []))
        _apply_sidecar(item, parsed)
        if parsed.problems:
            result.warnings.append(
                f"{path.name}: " + " ".join(parsed.problems)[:300]
            )

    item.tags = tags
    if item.media_type == "unknown":
        item.safe_for_auto = False
        item.needs_review = True
        item.review_reason = item.review_reason or (
            "the file type is not one this system knows how to place"
        )
    return item


def _apply_sidecar(item: AssetItem, parsed: "sidecar_module.Sidecar") -> None:
    """Copy the readable half of a sidecar onto the asset."""
    for key in ("category", "intensity", "moods", "styles", "preferred_styles",
                "avoid_styles", "bpm", "duration", "loudness_db",
                "volume_adjust_db", "start_offset", "end_offset", "loopable",
                "safe_for_auto", "license_notes", "notes", "usage_notes"):
        if key in parsed.data:
            setattr(item, key, parsed.data[key])
    if parsed.get("hud_risk"):
        item.notes = (item.notes + " | " if item.notes else "") + (
            "sidecar marks this a HUD risk: never placed over gameplay "
            "automatically"
        )
        item.usage_notes = (
            item.usage_notes + " | " if item.usage_notes else ""
        ) + "hud_risk"


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def media_type_for(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in MOGRT_EXTENSIONS:
        return "mogrt"
    return "unknown"


def tags_from_filename(filename: str) -> list:
    """Tags read out of a filename.

    ``whoosh_fast_01.wav`` -> ``whoosh``, ``fast``. Numbers are dropped
    (``01`` says nothing), stopwords are dropped, and everything is lowercased
    so ``Impact`` and ``impact`` are one tag.
    """
    stem = Path(filename).stem
    words = [word.lower() for word in _TOKEN.findall(stem)]
    return [
        AssetTag(name=word, source="filename", confidence=0.6)
        for word in dict.fromkeys(words)
        if word not in STOPWORDS and len(word) >= 2
    ]


def _intensity_from(lowered: str, category: str) -> str:
    if any(hint in lowered for hint in HIGH_INTENSITY_HINTS):
        return "high"
    if any(hint in lowered for hint in LOW_INTENSITY_HINTS):
        return "low"
    # Ambience is quiet by nature; everything else sits in the middle until
    # something says otherwise.
    return "low" if category == "ambience" else "medium"


def _fingerprint(path: Path, size: int) -> str:
    """Head + tail + size. Enough to notice a file changed."""
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    try:
        with open(path, "rb") as handle:
            digest.update(handle.read(_HASH_CHUNK))
            if size > _HASH_CHUNK * 2:
                handle.seek(-_HASH_CHUNK, os.SEEK_END)
                digest.update(handle.read(_HASH_CHUNK))
    except OSError:
        return f"unreadable:{size}"
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

class _Prober:
    """Duration via ffprobe, degrading quietly when it is not installed.

    The missing-tool case is checked once and remembered: a library of four
    hundred files should not attempt four hundred failing subprocess launches
    to establish the same fact.
    """

    def __init__(self, config: EditingConfig, *, enabled: bool = True):
        self.config = config
        self.enabled = enabled
        self.available: Optional[bool] = None if enabled else False
        self.probed = 0
        self.failed = 0

    def duration(self, path: Path) -> Optional[float]:
        if not self.enabled or self.available is False:
            return None
        try:
            from editing import ffmpeg as ff
        except ImportError:  # pragma: no cover - ships with the package
            self.available = False
            return None

        from editing.errors import ToolMissingError

        try:
            info = ff.probe(str(path), ffprobe=self.config.ffprobe)
        except ToolMissingError as exc:
            # ffprobe is not installed. That is a fact about the machine, not
            # about this file, so it is established once and every later file
            # skips the subprocess launch entirely.
            self.available = False
            logger.debug("ffprobe unavailable: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - one bad file is not the scan
            # ffprobe exists and could not read *this* file: corrupt, still
            # being written, an odd container. Counted and moved past, rather
            # than taken as evidence that probing does not work at all.
            self.available = True
            self.failed += 1
            logger.debug("Could not probe %s: %s", path, exc)
            return None

        self.available = True
        self.probed += 1
        seconds = _duration_of(info)
        if seconds is None:
            self.failed += 1
        return seconds


def _duration_of(info) -> Optional[float]:
    """Pull a duration out of whatever shape ``ffmpeg.probe`` returned."""
    if info is None:
        return None
    value = getattr(info, "duration", None)
    if value is None and isinstance(info, dict):
        value = info.get("duration")
        if value is None:
            value = (info.get("format") or {}).get("duration")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _summarise(library: AssetLibrary, prober: _Prober, say) -> None:
    stats = library.stats()
    library.folders = {
        name: sum(1 for item in library.items if item.category == name)
        for name in sorted({item.category for item in library.items})
    }

    say(
        f"Indexed {stats['total']} asset(s) under {library.root}: "
        f"{stats['usable']} usable, {stats['needs_review']} needing review, "
        f"{stats['missing']} missing."
    )

    if not library.items:
        library.warnings.append(
            "No assets were found. The placement pass will still run and will "
            "report every placeholder as a missing asset, which is a useful "
            "shopping list."
        )
    if prober.enabled and prober.available is False:
        library.warnings.append(
            "ffprobe is not available, so no durations were measured. Duration "
            "fit is skipped when matching, which makes matches less precise "
            "rather than wrong -- set a duration in a sidecar for anything "
            "where the length matters."
        )
    elif prober.failed:
        library.warnings.append(
            f"{prober.failed} file(s) could not be probed for duration."
        )
    if stats["needs_review"]:
        library.warnings.append(
            f"{stats['needs_review']} asset(s) need review and will not be "
            "placed automatically. Run `assets validate` for the details."
        )
