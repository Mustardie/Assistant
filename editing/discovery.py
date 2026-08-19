"""Footage discovery: what media exists, and where it sits in Premiere.

Turns a folder (or an explicit list of files, or the open Premiere project)
into ``MediaAsset`` records. Everything downstream keys off the ``asset_id``
this produces, so it runs before anything else and its output is written to
``assets.json`` for inspection.

The probe result is cached on the file's fingerprint. ffprobe on a 20 GB file
across a USB drive is not free, and a discovery pass runs at the start of every
command.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional, Sequence

from editing import ffmpeg as ff
from editing.cache import Cache
from editing.config import VIDEO_EXTENSIONS, EditingConfig
from editing.errors import FootageError
from editing.fingerprint import Fingerprint, fingerprint
from editing.premiere_link import ProjectSnapshot, describe, snapshot_project
from editing.schema import MediaAsset, PremiereRef

logger = logging.getLogger("nova.editing.discovery")


def find_media_files(
    folder: str | Path,
    *,
    recursive: bool = True,
    extensions: Sequence[str] = VIDEO_EXTENSIONS,
) -> list[Path]:
    """Every video file under ``folder``, sorted for a stable asset order.

    Hidden files and macOS resource forks (``._clip.mp4``) are skipped -- they
    are never real footage and ffprobe fails noisily on them.
    """
    root = Path(folder).expanduser()
    if not root.exists():
        raise FootageError(
            f"Footage folder not found: {root}",
            hint="Create it, or pass --folder with the path to your recordings.",
        )
    if root.is_file():
        return [root]

    wanted = {ext.lower() for ext in extensions}
    pattern = "**/*" if recursive else "*"
    found = [
        path for path in root.glob(pattern)
        if path.is_file()
        and path.suffix.lower() in wanted
        and not path.name.startswith(".")
    ]
    return sorted(found, key=lambda path: str(path).lower())


def probe_asset(
    path: str | Path,
    *,
    config: EditingConfig,
    cache: Optional[Cache] = None,
    print_: Optional[Fingerprint] = None,
) -> MediaAsset:
    """Build one ``MediaAsset``, reusing a cached ffprobe when nothing changed.

    A probe failure is recorded on the asset rather than raised: one unreadable
    file in a folder of fifty should not stop the run, and the empty duration
    makes it visible in the output.
    """
    target = Path(path)
    mark = print_ or fingerprint(target)

    key = None
    probed: Optional[dict] = None
    if cache is not None:
        key = cache.key("probe", file=mark.cache_key_part(), tool="ffprobe")
        probed = cache.get("probe", key)

    if probed is None:
        try:
            probed = ff.probe(target, ffprobe=config.ffprobe)
        except FootageError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the scan
            logger.warning("Could not probe %s: %s", target, exc)
            probed = {"error": str(exc)}
        if cache is not None and key is not None and "error" not in probed:
            cache.put("probe", key, probed, meta={"path": str(target)})

    return MediaAsset(
        asset_id=mark.asset_id,
        path=str(target),
        filename=target.name,
        duration=float(probed.get("duration") or 0.0),
        width=int(probed.get("width") or 0),
        height=int(probed.get("height") or 0),
        fps=float(probed.get("fps") or 0.0),
        has_audio=bool(probed.get("has_audio")),
        audio_channels=int(probed.get("audio_channels") or 0),
        size_bytes=mark.size_bytes,
        mtime=mark.mtime,
        content_hash=mark.content_hash,
        container=str(probed.get("container") or ""),
        video_codec=str(probed.get("video_codec") or ""),
        audio_codec=str(probed.get("audio_codec") or ""),
        probe_error=str(probed.get("error") or ""),
    )


def discover(
    *,
    config: EditingConfig,
    folder: Optional[str | Path] = None,
    files: Optional[Iterable[str | Path]] = None,
    cache: Optional[Cache] = None,
    recursive: bool = True,
    use_premiere: Optional[bool] = None,
    bridge=None,
) -> tuple[list[MediaAsset], ProjectSnapshot]:
    """Discover footage and map it to the open Premiere project.

    Sources are tried in order of how explicit they are: an explicit file list
    beats a folder, a folder beats the configured footage directory, and if
    none of those is given the media already imported into Premiere is used.
    That last path is what makes "analyse what I am already editing" work with
    no arguments.
    """
    consult_premiere = config.use_premiere if use_premiere is None else use_premiere
    project = (
        snapshot_project(bridge)
        if consult_premiere
        else ProjectSnapshot(available=False, note="Premiere lookup disabled")
    )

    paths = _resolve_sources(config, folder, files, recursive, project)

    assets: list[MediaAsset] = []
    seen: set[str] = set()
    for path in paths:
        try:
            mark = fingerprint(path)
        except FootageError as exc:
            logger.warning("Skipping %s: %s", path, exc)
            continue
        if mark.asset_id in seen:
            continue
        seen.add(mark.asset_id)

        asset = probe_asset(path, config=config, cache=cache, print_=mark)
        asset.premiere = (
            describe(str(path), project) if consult_premiere
            else PremiereRef(matched=False, note="Premiere lookup disabled")
        )
        assets.append(asset)

    return assets, project


def _resolve_sources(
    config: EditingConfig,
    folder: Optional[str | Path],
    files: Optional[Iterable[str | Path]],
    recursive: bool,
    project: ProjectSnapshot,
) -> list[Path]:
    if files:
        resolved = [Path(f).expanduser() for f in files]
        missing = [str(f) for f in resolved if not f.exists()]
        if missing:
            raise FootageError(
                f"{len(missing)} file(s) not found",
                hint="Paths must exist on this machine.",
                detail={"missing": missing[:20]},
            )
        return resolved

    if folder:
        return find_media_files(folder, recursive=recursive)

    if config.footage_dir:
        return find_media_files(config.footage_dir, recursive=recursive)

    if project.available:
        from_project = [
            Path(entry.get("path"))
            for entry in project.by_path.values()
            if entry.get("path")
            and str(entry.get("media_type")) in ("video", "unknown")
            and Path(entry["path"]).exists()
        ]
        if from_project:
            return sorted(from_project, key=lambda path: str(path).lower())
        raise FootageError(
            "The open Premiere project has no readable video on disk",
            hint="Pass --folder with your footage directory, or relink the "
                 "offline media in Premiere.",
        )

    raise FootageError(
        "No footage source given",
        hint="Pass --folder, set EDITING_FOOTAGE_DIR, or open a Premiere "
             "project containing the footage.",
    )
