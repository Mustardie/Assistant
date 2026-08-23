"""Measuring the footage a render is about to read, and keying on it.

Two jobs, and they are the same job seen from two sides.

**Before a render**, every distinct source file is measured once: is it there,
how big is it, does it have an audio track, what shape is it. That last
question is not curiosity -- a clip with no microphone track needs a generated
silent track or the concat refuses to join it, and finding that out with one
``ffprobe`` beforehand is much better than finding it out from a failed encode
forty seconds in.

**After a render**, the same measurements are the cache key. A render is
reusable when the plan, the sources, the settings and the FFmpeg build are all
unchanged; each of those is a field here.

The content hash is the load-bearing part. Keying on the path alone would
serve yesterday's render of footage that has since been re-exported, which is
the worst possible failure for a tool whose entire purpose is showing you what
your current cut looks like.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional, Sequence

from editing.cache import canonical_key
from editing.fingerprint import asset_id_for, content_hash, normalise_path
from editing.render.schema import RenderConfig, RenderInput, RenderSegment
from editing.roughcut.schema import RoughCutPlan
from editing.schema import short_hash

logger = logging.getLogger("nova.editing.render.sources")

#: Bumped when a change here makes previously cached renders wrong.
RENDER_SCHEMA_VERSION = 1


def describe_inputs(
    segments: Sequence[RenderSegment],
    *,
    runner=None,
    probe: bool = True,
    hash_content: bool = True,
) -> tuple[list[RenderInput], list[str]]:
    """Measure every distinct source the segments read from.

    One entry per file, not per segment: a cut that uses the same recording
    forty times should cost one stat, one hash and one probe.

    Never raises. A file that has gone missing comes back as an unusable
    ``RenderInput`` with a warning, because the useful thing to do about three
    missing clips out of thirty is to say which three.
    """
    warnings: list[str] = []
    by_path: dict = {}
    order: list[str] = []

    for segment in segments:
        key = normalise_path(segment.source_path) if segment.source_path \
            else ""
        if not key:
            continue
        if key not in by_path:
            by_path[key] = _measure(
                segment.source_path,
                asset_id=segment.asset_id,
                hash_content=hash_content,
            )
            order.append(key)
        by_path[key].segments += 1

    inputs = [by_path[key] for key in order]

    if probe and runner is not None:
        for item in inputs:
            if not item.usable:
                continue
            _apply_probe(item, runner, warnings)

    for item in inputs:
        if not item.exists:
            warnings.append(
                f"{Path(item.path).name}: the source file is not there any "
                "more. Reconnect the drive, or rebuild the rough cut from "
                "where the footage lives now."
            )
        elif not item.size_bytes:
            warnings.append(
                f"{Path(item.path).name}: the source file is empty.")
        warnings.extend(item.warnings)
    return inputs, warnings


def _measure(
    path: str, *, asset_id: str = "", hash_content: bool = True
) -> RenderInput:
    target = Path(path).expanduser()
    item = RenderInput(
        path=str(target),
        asset_id=asset_id or _asset_id_or_blank(target),
    )
    try:
        stat = target.stat()
    except OSError:
        return item
    item.exists = True
    item.size_bytes = stat.st_size
    item.mtime = stat.st_mtime
    if hash_content and stat.st_size:
        try:
            item.content_hash = content_hash(target, size_bytes=stat.st_size)
        except Exception as exc:  # noqa: BLE001 - a locked file is a fact
            item.warnings.append(
                f"{target.name}: could not read the file to fingerprint it "
                f"({exc}); this render will not be cacheable."
            )
    return item


def _asset_id_or_blank(target: Path) -> str:
    try:
        return asset_id_for(target)
    except Exception:  # noqa: BLE001 - an unrepresentable path, not a crash
        return ""


def _apply_probe(item: RenderInput, runner, warnings: list[str]) -> None:
    """Fill in duration, size and -- the important one -- ``has_audio``.

    A probe that fails leaves ``has_audio`` at its default of True and says
    so. That is the safer default: assuming audio and being wrong produces one
    clear FFmpeg error naming the clip, while assuming silence and being wrong
    silently throws away the commentary, which is the half of the cut a person
    is actually judging.
    """
    try:
        info = runner.probe(item.path) or {}
    except Exception as exc:  # noqa: BLE001 - probing is best-effort
        warnings.append(
            f"{Path(item.path).name}: could not be probed ({exc}). Assuming "
            "it has an audio track."
        )
        return
    if not info:
        warnings.append(
            f"{Path(item.path).name}: ffprobe read nothing from this file. "
            "It may be corrupt, or still being written."
        )
        return
    item.duration = float(info.get("duration") or 0.0)
    item.width = int(info.get("width") or 0)
    item.height = int(info.get("height") or 0)
    item.fps = float(info.get("fps") or 0.0)
    item.has_audio = bool(info.get("has_audio", True))
    if not item.has_audio:
        item.warnings.append(
            f"{Path(item.path).name} has no audio track; its segments get "
            "silence so the render can still be joined."
        )


def check_ranges(
    segments: Sequence[RenderSegment], inputs: Sequence[RenderInput]
) -> list[str]:
    """Ranges that ask for footage past the end of their file.

    A rough cut built from one probe and rendered after the file was
    re-exported shorter is the realistic case. FFmpeg would produce a short
    segment without complaining, and the render would come out quietly wrong;
    saying it here is the difference between a puzzling proxy and an explained
    one.
    """
    durations = {
        item.path: item.duration for item in inputs if item.duration > 0
    }
    out: list[str] = []
    for segment in segments:
        known = durations.get(segment.source_path)
        if known is None:
            continue
        if segment.source_out > known + 0.5:
            out.append(
                f"{Path(segment.source_path).name}: the cut asks for "
                f"{segment.source_in:.1f}-{segment.source_out:.1f}s but the "
                f"file is only {known:.1f}s long. That segment will be short."
            )
    return out


# ---------------------------------------------------------------------------
# The cache key
# ---------------------------------------------------------------------------

def _time(value, places: int = 3) -> str:
    """One canonical spelling for a time, whatever type it arrived as.

    Load-bearing, and the reason is not obvious. A plan built in memory can
    carry ``source_in=1`` while the same plan read back from JSON carries
    ``1.0``, and ``repr`` spells those differently -- so hashing the raw values
    made "build a plan, then render it" and "load that plan, then render it"
    produce different keys for the identical cut. The cache then missed on the
    exact path it exists for, and re-rendered an hour of footage to produce a
    byte-identical file.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:.{places}f}"


def plan_fingerprint(plan: RoughCutPlan) -> str:
    """A hash of everything about a rough cut that changes the video.

    Placements only, and only the fields the renderer reads. Markers,
    operations, explanations and the dry-run state are all deliberately out:
    re-running the style pass rewrites two hundred operations and changes not
    one frame of the assembly, and re-rendering an hour of footage for that
    would make the cache worthless exactly when it matters.
    """
    parts = [
        (
            str(p.source_file), _time(p.source_in), _time(p.source_out),
            _time(p.sequence_start), _time(p.speed, 4), str(p.track),
            int(p.index),
        )
        for p in sorted(
            plan.placements,
            key=lambda item: (round(float(item.sequence_start), 4),
                              int(item.index)),
        )
    ]
    return short_hash(str(plan.sequence_name), repr(parts), length=16)


def segments_fingerprint(segments: Sequence[RenderSegment]) -> str:
    """The same idea, for segments that did not come from a plan."""
    parts = [
        (
            str(s.source_path), _time(s.source_in), _time(s.source_out),
            _time(s.timeline_in), _time(s.speed, 4), bool(s.audio_enabled),
        )
        for s in segments
    ]
    return short_hash(repr(parts), length=16)


def render_cache_key(
    *,
    segments: Sequence[RenderSegment],
    inputs: Sequence[RenderInput],
    config: RenderConfig,
    plan_hash: str = "",
    ffmpeg_version: str = "",
) -> str:
    """The key that decides whether a previous render can be reused.

    Four independent things, and every one of them has produced a wrong render
    at some point in somebody's pipeline:

    * **the cut** -- ranges, order, speeds
    * **the sources** -- content hashes, so a re-export invalidates
    * **the settings** -- everything that changes a pixel
    * **the encoder** -- FFmpeg's own version, because a new build can
      legitimately produce a different file from identical inputs

    ``ffmpeg_version`` is allowed to be empty (nothing could ask), and an
    empty value participates in the key like any other: a render made when the
    version was unknown does not satisfy a request made when it is known.
    """
    return canonical_key({
        "kind": "render",
        "schema_version": RENDER_SCHEMA_VERSION,
        "plan": plan_hash or segments_fingerprint(segments),
        "segments": segments_fingerprint(segments),
        "sources": [item.cache_key_part() for item in inputs],
        "config": config.cache_key_part(),
        "ffmpeg": ffmpeg_version,
    })
