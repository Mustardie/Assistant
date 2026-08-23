"""``RoughCutPlan`` -> ``RenderSegment[]``.

The rough cut is written for Premiere: placements on a track, operations that
create a sequence and append clips to it, markers, and a plan that ripples.
A flat proxy has none of that. This module is the translation, and it has one
governing rule:

**What cannot be represented becomes a warning, and the cut still renders.**

A rough cut carrying captions, markers, zooms and sound effects is the *normal*
case by Session 6, and refusing to render it would make this whole package
useless exactly when it becomes valuable. So the unsupported features are
listed, once each, in language that says what is missing from the video -- and
the video gets made.

## What survives the translation

* the clip order, from ``sequence_start``
* the exact source in and out points
* speed changes, within the range ``atempo`` can be chained to cover
* which clips are audible
* the placement and recommendation IDs, so a moment in the proxy is traceable

## What does not

* everything on a track other than V1 (overlays, SFX, music, B-roll)
* markers, captions, cards, effects, transitions, fades, ducking
* anything a later pass would have rippled

That list is not a gap to close later. A proxy exists to answer "does this cut
work", and it answers that question in exactly the terms the rough cut decided:
these ranges, in this order, at these speeds.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional, Sequence

from editing.render.schema import (
    REPRESENTED_OPS, SPEED_MAX, SPEED_MIN, UNSUPPORTED_FEATURES, RenderSegment,
    segment_id_for,
)
from editing.roughcut.schema import ClipPlacement, RoughCutPlan

logger = logging.getLogger("nova.editing.render.convert")

#: The track the rough assembly lives on. Anything else is an overlay, and a
#: flat single-stream render has nowhere to put it.
BASE_VIDEO_TRACK = "V1"

#: Ranges shorter than this are dropped rather than rendered. Two frames at
#: 30fps is not a shot; it is a rounding error in somebody's selection maths,
#: and encoding it costs a whole FFmpeg invocation.
MIN_SEGMENT_SECONDS = 0.08


class ConversionResult:
    """Segments, plus everything the conversion wanted to say about them.

    A small class rather than a tuple because three of the four fields are
    routinely ignored by callers who only want the segments, and a tuple makes
    that mistake silent.
    """

    __slots__ = ("segments", "warnings", "unsupported", "dropped")

    def __init__(
        self,
        segments: list[RenderSegment],
        warnings: list[str],
        unsupported: list[str],
        dropped: int = 0,
    ):
        self.segments = segments
        self.warnings = warnings
        self.unsupported = unsupported
        self.dropped = dropped

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def duration(self) -> float:
        return round(sum(segment.duration for segment in self.segments), 3)

    def to_dict(self) -> dict:
        return {
            "segments": [segment.to_dict() for segment in self.segments],
            "warnings": list(self.warnings),
            "unsupported": list(self.unsupported),
            "dropped": self.dropped,
            "duration": self.duration,
        }


def to_segments(
    plan: RoughCutPlan,
    *,
    include_audio: bool = True,
    muted_placements: Optional[Iterable[str]] = None,
    max_seconds: float = 0.0,
    source_overrides: Optional[dict] = None,
) -> ConversionResult:
    """Convert a rough cut into an ordered list of renderable segments.

    ``muted_placements`` is the seam for a future pass that decides a clip
    should play silently -- the rough cut has no such concept today, and
    inventing one here would be a decision this module has no business making.

    ``max_seconds`` truncates the render, cutting the segment that straddles
    the boundary rather than dropping it, so "the first two minutes" really is
    the first two minutes.

    ``source_overrides`` maps a source path to a replacement. Used when
    footage has moved and the person knows where it went; the plan on disk is
    left alone.
    """
    muted = {str(item) for item in (muted_placements or ())}
    overrides = {str(k): str(v) for k, v in (source_overrides or {}).items()}
    warnings: list[str] = []
    segments: list[RenderSegment] = []
    dropped = 0

    placements = _ordered(plan.placements)
    off_track = [p for p in placements if p.track != BASE_VIDEO_TRACK]
    if off_track:
        warnings.append(
            f"{len(off_track)} placement(s) are not on {BASE_VIDEO_TRACK} and "
            "were skipped: a flat proxy has one video stream, so overlays, "
            "B-roll and picture-in-picture have nowhere to go."
        )

    timeline = 0.0
    for placement in placements:
        if placement.track != BASE_VIDEO_TRACK:
            dropped += 1
            continue

        segment, notes = _segment_for(
            placement,
            index=len(segments),
            timeline_in=timeline,
            include_audio=include_audio,
            muted=placement.placement_id in muted,
            overrides=overrides,
        )
        if segment is None:
            dropped += 1
            warnings.extend(notes)
            continue

        warnings.extend(notes)
        if max_seconds > 0:
            if segment.timeline_in >= max_seconds - 1e-6:
                dropped += 1
                continue
            if segment.timeline_out > max_seconds:
                segment = _truncated(segment, max_seconds)
        segments.append(segment)
        timeline = segment.timeline_out

    if max_seconds > 0 and segments and timeline < plan.total_duration - 1e-6:
        warnings.append(
            f"Only the first {max_seconds:.0f}s of a "
            f"{plan.total_duration:.0f}s cut was rendered (--max-seconds)."
        )

    unsupported = describe_unsupported(plan)
    if plan.warnings:
        warnings.extend(
            f"from the rough cut: {text}" for text in plan.warnings[:20])
    if not segments:
        warnings.append(
            "The rough cut produced no renderable segments. Build one with "
            "`python -m editing.cli roughcut build` first."
        )
    return ConversionResult(segments, warnings, unsupported, dropped)


def _ordered(placements: Sequence[ClipPlacement]) -> list[ClipPlacement]:
    """Placements in the order they play.

    Sorted by ``sequence_start`` and then by ``index``, rather than trusted in
    list order. The list order *is* the play order today, and the day a pass
    appends a placement without re-sorting, a cut rendered from list order
    would play in a different order than the same plan executed in Premiere --
    which would look like a renderer bug and would not be one.
    """
    return sorted(
        placements, key=lambda p: (round(p.sequence_start, 4), p.index))


def _segment_for(
    placement: ClipPlacement,
    *,
    index: int,
    timeline_in: float,
    include_audio: bool,
    muted: bool,
    overrides: dict,
) -> tuple[Optional[RenderSegment], list[str]]:
    """One placement, as a segment. ``None`` when it cannot be rendered."""
    notes: list[str] = []
    name = Path(placement.source_file).name or placement.source_file

    source_path = overrides.get(placement.source_file, placement.source_file)
    if not source_path:
        return None, [
            f"placement {placement.placement_id} names no source file and was "
            "skipped."
        ]

    source_in = max(0.0, placement.source_in)
    source_out = max(source_in, placement.source_out)
    if source_out - source_in < MIN_SEGMENT_SECONDS:
        return None, [
            f"{name} {source_in:.2f}-{source_out:.2f}s is shorter than "
            f"{MIN_SEGMENT_SECONDS:.2f}s and was dropped."
        ]

    speed, speed_notes = resolve_speed(placement.speed, label=name)
    notes.extend(speed_notes)

    audio_enabled = include_audio and not muted
    if muted and include_audio:
        notes.append(f"{name}: audio muted for this clip.")

    duration = (source_out - source_in) / speed
    segment = RenderSegment(
        segment_id=segment_id_for(index, placement.placement_id, source_in),
        index=index,
        source_path=source_path,
        asset_id=placement.asset_id,
        source_in=source_in,
        source_out=source_out,
        timeline_in=round(timeline_in, 4),
        timeline_out=round(timeline_in + duration, 4),
        speed=speed,
        audio_enabled=audio_enabled,
        placement_id=placement.placement_id,
        recommendation_ids=list(placement.recommendation_ids),
        keep_reason=placement.keep_reason,
        protected=placement.protected,
        label=_label_for(placement),
        warnings=list(notes),
    )
    return segment, notes


def resolve_speed(speed: float, *, label: str = "") -> tuple[float, list[str]]:
    """A speed FFmpeg can actually produce, and what was given up to get it.

    Anything outside ``SPEED_MIN``..``SPEED_MAX`` falls back to 1x rather than
    being clamped. Clamping would silently render a 20x timelapse at 8x, which
    looks like a bug in the *cut*; refusing it and saying so does not.
    """
    try:
        rate = float(speed)
    except (TypeError, ValueError):
        rate = 0.0
    prefix = f"{label}: " if label else ""
    if rate <= 0:
        return 1.0, [
            f"{prefix}speed {speed!r} is not a usable rate; rendered at 1x."
        ]
    if rate < SPEED_MIN or rate > SPEED_MAX:
        return 1.0, [
            f"{prefix}speed {rate:g}x is outside the {SPEED_MIN:g}x-"
            f"{SPEED_MAX:g}x this renderer supports; rendered at 1x."
        ]
    return rate, []


def _truncated(segment: RenderSegment, limit: float) -> RenderSegment:
    """The part of ``segment`` that fits before ``limit`` on the timeline."""
    keep = max(0.0, limit - segment.timeline_in)
    source_keep = keep * segment.speed
    return RenderSegment(
        segment_id=segment.segment_id,
        index=segment.index,
        source_path=segment.source_path,
        asset_id=segment.asset_id,
        source_in=segment.source_in,
        source_out=round(segment.source_in + source_keep, 4),
        timeline_in=segment.timeline_in,
        timeline_out=round(segment.timeline_in + keep, 4),
        speed=segment.speed,
        audio_enabled=segment.audio_enabled,
        placement_id=segment.placement_id,
        recommendation_ids=list(segment.recommendation_ids),
        keep_reason=segment.keep_reason,
        protected=segment.protected,
        label=segment.label,
        warnings=list(segment.warnings) + ["cut short by --max-seconds"],
    )


def _label_for(placement: ClipPlacement) -> str:
    """A short human label for the review notes."""
    parts = [placement.keep_reason or "unknown"]
    if placement.speed and abs(placement.speed - 1.0) > 1e-6:
        parts.append(f"{placement.speed:g}x")
    if placement.protected:
        parts.append("protected")
    return " / ".join(parts)


def describe_unsupported(plan: RoughCutPlan) -> list[str]:
    """What this plan asks for that a flat proxy cannot show, once each.

    Read off the operations rather than off the placements, because that is
    where the style and asset passes put their work: by the time a cut has been
    through Session 5 and 6 the placements are unchanged and the op list has
    grown by two hundred entries.
    """
    seen: dict[str, int] = {}
    for op in plan.ops or ():
        name = str((op or {}).get("op") or "")
        if not name or name in REPRESENTED_OPS:
            continue
        if name in UNSUPPORTED_FEATURES:
            seen[name] = seen.get(name, 0) + 1
        else:
            seen.setdefault(name, 0)
            seen[name] += 1

    # ``marker.add`` operations and ``plan.markers`` are the same markers seen
    # from two sides, so reporting both would say the same thing twice with
    # two different numbers. The marker line below covers them.
    markers = max(seen.pop("marker.add", 0), len(plan.markers))

    out: list[str] = []
    for name, count in sorted(seen.items(), key=lambda pair: -pair[1]):
        described = UNSUPPORTED_FEATURES.get(
            name, f"'{name}' operations, which this renderer does not know")
        out.append(f"{count} x {described}")

    if markers:
        out.append(
            f"{markers} sequence marker(s) -- the review notes beside this "
            "video carry the same information in a form you can read while "
            "watching."
        )
    return out
