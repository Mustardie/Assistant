"""Turning a computed layout into Premiere operations.

Everything here is offline. The operation list is built from the placements
and recommendations alone, using only ops already in ``premiere.catalog``, and
is validated before anything runs.

**Operation order is load-bearing**, so it is fixed here rather than left to
the caller:

1. ``project.import`` — the media must exist in the project before it can be
   placed.
2. ``sequence.create`` from the first source clip, so the scratch sequence
   inherits its resolution and frame rate. Premiere puts that whole clip on the
   timeline as a side effect, so it is removed immediately.
3. ``sequence.activate`` — every later op acts on the active sequence.
4. ``clip.append`` per placement, in playback order. Appending is what makes
   the layout deterministic: clip *n* is the *n*-th range.
5. ``clip.speed`` for retimed clips, **in reverse order with ripple**. Rippling
   shifts everything after the clip, so working backwards means each clip is
   still where the plan says when its turn comes.
6. Punch-ins and push-ins, targeted by sequence time.
7. ``marker.add`` last, at final post-retime positions.

Steps 6 and 7 run after all retiming precisely because retiming moves clips.
Placing a marker before a speed change would leave it pointing at whatever
slid into that spot.

**Only conservative edits convert.** A punch-in is refused on a protected clip,
on anything with a full-screen UI or visible low health, and above a scale
ceiling. Everything refused is recorded in ``unconverted`` with its reason
rather than dropped.
"""
from __future__ import annotations

import time
from typing import Optional, Sequence

from editing.recommend.schema import EditRecommendation, RecommendationSet
from editing.roughcut.schema import (
    ClipPlacement, RoughCutPlan, SequenceMarker, Unconverted,
)
from editing.roughcut.select import map_to_sequence

#: Bin the rough cut's media is imported into.
SOURCE_BIN = "Nova Rough Cut Source"

#: Hard ceiling on any automatic zoom. 115% is noticeable and safe; past ~125%
#: a 1080p source starts to soften and the HUD begins leaving the frame.
MAX_PUNCH_SCALE = 115.0
MAX_PUSH_SCALE = 108.0

#: A zoom shorter than this reads as a glitch rather than emphasis.
MIN_ZOOM_SECONDS = 1.5

#: Categories that become markers on the sequence. Placeholders included: a
#: marker is the honest form of "something belongs here but the asset does not
#: exist yet".
MARKER_CATEGORIES = {
    "marker": ("NOTE", "comment"),
    "structure_cut": ("CUT", "comment"),
    "beat_marker": ("BEAT", "comment"),
    "music_cue": ("MUSIC", "comment"),
    "sound_effect": ("SFX", "comment"),
    "ducking": ("DUCK", "comment"),
    "audio_fade": ("FADE", "comment"),
    "text_overlay": ("TEXT", "comment"),
    "caption_emphasis": ("CAPTION", "comment"),
    "visual_callout": ("CALLOUT", "comment"),
    "color_adjust": ("COLOR", "comment"),
}

#: Categories converted into real picture edits rather than annotations.
ZOOM_CATEGORIES = {"punch_in", "slow_push_in"}

#: Handled by the assembly itself rather than by a per-recommendation op.
ASSEMBLY_CATEGORIES = {"trim_dead_air", "hold", "speed_ramp"}


def build_ops(
    plan: RoughCutPlan,
    recommendations: Optional[RecommendationSet] = None,
    *,
    sequence_name: str = "Nova Rough Cut",
    allow_zooms: bool = True,
    segments_by_id: Optional[dict] = None,
    preset: str = "",
) -> RoughCutPlan:
    """Fill ``plan.ops``, ``plan.markers`` and ``plan.unconverted``.

    Mutates and returns the plan so the placements and the operations that
    realise them stay in one object.
    """
    recommendations = recommendations or RecommendationSet()
    segments_by_id = segments_by_id or {}
    plan.sequence_name = sequence_name
    plan.generated_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    if not plan.placements:
        plan.warnings.append(
            "Nothing was selected for the cut, so there is nothing to build. "
            "Check the timeline has usable segments."
        )
        return plan

    ops: list[dict] = []
    ops.extend(_setup_ops(plan, sequence_name, preset))
    ops.extend(_assembly_ops(plan))
    ops.extend(_speed_ops(plan))

    zoom_ops, zoom_markers = _zoom_ops(
        plan, recommendations, segments_by_id, allow_zooms=allow_zooms
    )
    ops.extend(zoom_ops)

    plan.markers = _markers(plan, recommendations) + zoom_markers
    plan.markers.sort(key=lambda marker: (marker.time, marker.name))
    ops.extend(_marker_ops(plan))

    plan.ops = ops
    return plan


# ---------------------------------------------------------------------------
# Setup and assembly
# ---------------------------------------------------------------------------

def _setup_ops(plan: RoughCutPlan, sequence_name: str, preset: str) -> list[dict]:
    """Import the media and create an empty scratch sequence."""
    paths = list(dict.fromkeys(p.source_file for p in plan.placements))
    plan.source_paths = paths

    ops: list[dict] = [{
        "op": "project.import",
        "paths": paths,
        "bin": SOURCE_BIN,
        "note": "Ensure the rough cut's source media is in the project.",
    }]

    if preset:
        ops.append({
            "op": "sequence.create",
            "name": sequence_name,
            "preset": preset,
            "note": "Scratch sequence from the given preset.",
        })
    else:
        # No preset available, so the sequence is created from the first clip
        # to inherit its resolution and frame rate. Premiere places that clip
        # on the timeline as a side effect; it is removed on the next line so
        # the assembly starts from an empty track.
        ops.append({
            "op": "sequence.create",
            "name": sequence_name,
            "from_asset": paths[0],
            "note": "Scratch sequence; inherits settings from the first clip.",
        })

    ops.append({
        "op": "sequence.activate",
        "name": sequence_name,
        "note": "Everything after this acts on the scratch sequence.",
    })

    if not preset:
        ops.append({
            "op": "clip.remove",
            "clip": {"track": "V1", "index": 0},
            "ripple": True,
            "note": "Remove the clip Premiere auto-placed when creating the "
                    "sequence, so the assembly starts empty.",
        })
    return ops


def _assembly_ops(plan: RoughCutPlan) -> list[dict]:
    """One ``clip.append`` per placement, in playback order."""
    ops: list[dict] = []
    for placement in plan.placements:
        ops.append({
            "op": "clip.append",
            "asset": placement.source_file,
            "track": placement.track,
            "in": round(placement.source_in, 3),
            "out": round(placement.source_out, 3),
            "note": (
                f"{placement.keep_reason} "
                f"({placement.source_in:.1f}-{placement.source_out:.1f}s of "
                f"{placement.source_file.rsplit('/', 1)[-1]}) "
                f"[{placement.placement_id}]"
            ),
        })
    return ops


def _speed_ops(plan: RoughCutPlan) -> list[dict]:
    """Retime the filler clips, back to front.

    Rippling shifts every clip after the one being retimed. Working in reverse
    means each clip is still at its planned position when its own op runs, and
    once they have all run the timeline matches the computed layout exactly.
    """
    ops: list[dict] = []
    retimed = [p for p in plan.placements if p.speed != 1.0 and not p.protected]

    for placement in sorted(retimed, key=lambda p: p.index, reverse=True):
        ops.append({
            "op": "clip.speed",
            # Targeted by index rather than time: nothing has moved yet at this
            # point in the plan, and index is exact for a freshly appended
            # track.
            "clip": {"track": placement.track, "index": placement.index},
            "rate": round(placement.speed, 3),
            "maintain_pitch": True,
            "ripple": True,
            "note": f"{placement.keep_reason} sped to {placement.speed:g}x "
                    f"[{placement.placement_id}]",
        })
    return ops


# ---------------------------------------------------------------------------
# Zooms
# ---------------------------------------------------------------------------

def _zoom_ops(
    plan: RoughCutPlan,
    recommendations: RecommendationSet,
    segments_by_id: dict,
    *,
    allow_zooms: bool,
) -> tuple[list[dict], list[SequenceMarker]]:
    """Convert punch-ins and push-ins that are safe on this cut.

    Returns the operations plus markers for the ones that were refused, so a
    human editor still sees where the planner wanted emphasis even when the
    automatic version was judged unsafe.
    """
    ops: list[dict] = []
    markers: list[SequenceMarker] = []

    for entry in recommendations.recommendations:
        if entry.category not in ZOOM_CATEGORIES:
            continue
        if entry.status != "accepted":
            plan.unconverted.append(Unconverted(
                entry.recommendation_id, entry.category, entry.start, entry.end,
                f"Not accepted by the safety pass: {entry.status_reason}",
            ))
            continue

        if not allow_zooms:
            plan.unconverted.append(Unconverted(
                entry.recommendation_id, entry.category, entry.start, entry.end,
                "Zooms disabled for this build (--no-zooms).",
            ))
            markers.append(_zoom_marker(plan, entry, "zooms disabled"))
            continue

        placement = _placement_for(plan, entry)
        if placement is None:
            plan.unconverted.append(Unconverted(
                entry.recommendation_id, entry.category, entry.start, entry.end,
                "The footage this applies to was cut out of the rough cut.",
            ))
            continue

        refusal = _zoom_refusal(entry, placement, segments_by_id)
        if refusal:
            plan.unconverted.append(Unconverted(
                entry.recommendation_id, entry.category, entry.start, entry.end,
                refusal,
            ))
            markers.append(_zoom_marker(plan, entry, refusal))
            continue

        start = map_to_sequence(plan.placements, entry.asset_id, entry.start)
        end = map_to_sequence(plan.placements, entry.asset_id, entry.end)
        if start is None:
            continue
        if end is None or end <= start:
            end = min(placement.sequence_end, start + 2.0)

        scale = (
            MAX_PUNCH_SCALE if entry.category == "punch_in" else MAX_PUSH_SCALE
        )
        ops.append(_zoom_op(entry, placement, start, end, scale))

    return ops, markers


def _zoom_op(
    entry: EditRecommendation,
    placement: ClipPlacement,
    start: float,
    end: float,
    scale: float,
) -> dict:
    """One ``animate`` on Motion > Scale, relative to the sequence."""
    duration = max(MIN_ZOOM_SECONDS, min(end - start, placement.sequence_duration))
    if entry.category == "punch_in":
        # A punch is meant to feel abrupt, so it arrives quickly and holds.
        duration = min(duration, 0.8)
        easing = "ease_out"
    else:
        easing = "ease_both"

    return {
        "op": "animate",
        "clip": placement.selector(),
        "component": "Motion",
        "property": "Scale",
        "from": 100.0,
        "to": scale,
        "start": round(start, 3),
        "duration": round(duration, 3),
        "easing": easing,
        "relative_to": "sequence",
        "note": f"{entry.category} -> {scale:g}% ({entry.reason[:60]}) "
                f"[{entry.recommendation_id}]",
    }


def _zoom_refusal(
    entry: EditRecommendation,
    placement: ClipPlacement,
    segments_by_id: dict,
) -> str:
    """Why this zoom must not be applied automatically, or ""."""
    if placement.protected:
        return (
            "The clip is a protected hold; a zoom would edit a moment the "
            "pacing layer said to leave raw."
        )
    if placement.speed != 1.0:
        return (
            f"The clip is retimed to {placement.speed:g}x; zooming a sped-up "
            "clip compounds two edits on the same footage."
        )
    if placement.sequence_duration < MIN_ZOOM_SECONDS:
        return (
            f"The clip is only {placement.sequence_duration:.1f}s on the "
            f"timeline; a zoom under {MIN_ZOOM_SECONDS}s reads as a glitch."
        )

    # Re-check the gameplay-hiding rules against the segments this clip covers.
    # The safety pass already did this, but the cut may have merged segments,
    # so the clip can now span footage the original recommendation never saw.
    for segment_id in placement.segment_ids:
        segment = segments_by_id.get(segment_id)
        if segment is None:
            continue
        if any(event.ui.any_screen_open for event in segment.events):
            return (
                "A full-screen UI is open somewhere in this clip; zooming "
                "would hide what the viewer is reading."
            )
        if any(event.ui.low_health for event in segment.events):
            return (
                "Low health is visible in this clip and is why the moment is "
                "tense; a zoom risks cropping the HUD out of frame."
            )
    return ""


def _zoom_marker(
    plan: RoughCutPlan, entry: EditRecommendation, reason: str
) -> SequenceMarker:
    """A marker standing in for a zoom that was not applied."""
    at = map_to_sequence(plan.placements, entry.asset_id, entry.start) or 0.0
    return SequenceMarker(
        time=at,
        name="ZOOM?",
        comment=f"{entry.category} was not applied: {reason} | {entry.reason}",
        recommendation_id=entry.recommendation_id,
        category=entry.category,
    )


def _placement_for(
    plan: RoughCutPlan, entry: EditRecommendation
) -> Optional[ClipPlacement]:
    for placement in plan.placements:
        if placement.asset_id != entry.asset_id:
            continue
        if placement.source_in <= entry.start < placement.source_out:
            return placement
    return None


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def _markers(
    plan: RoughCutPlan, recommendations: RecommendationSet
) -> list[SequenceMarker]:
    """A marker per accepted annotation recommendation, at its cut position."""
    markers: list[SequenceMarker] = []

    for entry in recommendations.recommendations:
        if entry.category in ASSEMBLY_CATEGORIES:
            # Realised by the assembly itself, not by an operation.
            continue
        if entry.category in ZOOM_CATEGORIES:
            continue
        if entry.category not in MARKER_CATEGORIES:
            plan.unconverted.append(Unconverted(
                entry.recommendation_id, entry.category, entry.start, entry.end,
                "No rough-cut conversion is defined for this category.",
            ))
            continue
        if entry.status != "accepted":
            continue

        at = map_to_sequence(plan.placements, entry.asset_id, entry.start)
        if at is None:
            plan.unconverted.append(Unconverted(
                entry.recommendation_id, entry.category, entry.start, entry.end,
                "The moment this refers to was cut out of the rough cut.",
            ))
            continue

        name, kind = MARKER_CATEGORIES[entry.category]
        end = map_to_sequence(plan.placements, entry.asset_id, entry.end)
        markers.append(SequenceMarker(
            time=at,
            name=name,
            comment=_marker_comment(entry),
            kind=kind,
            duration=max(0.0, (end - at)) if end and end > at else 0.0,
            recommendation_id=entry.recommendation_id,
            category=entry.category,
        ))

    return markers


def _marker_comment(entry: EditRecommendation) -> str:
    parts = [entry.reason]
    if entry.evidence.channels:
        parts.append("evidence: " + "+".join(entry.evidence.channels))
    if entry.evidence.audio_types:
        parts.append("audio: " + ", ".join(entry.evidence.audio_types[:3]))
    if entry.notes:
        parts.append(entry.notes)
    parts.append(f"priority {entry.priority:.2f} [{entry.recommendation_id}]")
    return " | ".join(parts)[:500]


def _marker_ops(plan: RoughCutPlan) -> list[dict]:
    ops: list[dict] = []
    for marker in plan.markers:
        op: dict = {
            "op": "marker.add",
            "time": round(marker.time, 3),
            "name": marker.name,
            "comment": marker.comment,
            "type": marker.kind,
            "note": f"{marker.category} marker",
        }
        if marker.duration >= 0.25:
            op["duration"] = round(marker.duration, 3)
        ops.append(op)
    return ops
