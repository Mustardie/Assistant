"""The visual emphasis layer.

Everything here scales or flags the picture, which makes it the layer most able
to do damage — a punch-in that crops the health bar off the frame ruins the
exact moment it was trying to sell. So the rules are stated as **refusals**,
and each one names the thing it is protecting:

* **A protected hold is not zoomed.** The pacing layer decided that moment
  reads best raw. A style may override this, but only with high confidence and
  only when it says so explicitly.
* **A retimed clip is not zoomed.** Two edits compounding on the same footage
  is how "styled" becomes "busy".
* **A clip with a full-screen UI or visible low health is not zoomed.** The
  viewer is reading something; scaling it up pushes it off the edge.
* **A clip the critic already complained about is not zoomed.** Session 4's
  findings are carried in as blocked ranges, so a moment that was flagged for a
  bad crop does not get a second one on top.
* **Zooms do not stack.** Two scale changes inside the style's stack spacing
  are one too many, and the weaker one is deferred.
* **The style's ceiling is absolute.** ``max_zoom_scale`` is applied after
  every other calculation, so no combination of inputs can produce a stronger
  zoom than the preset permits — and a style with a ceiling of 100% emits no
  zooms at all.

The markers this layer emits (reveal, danger, funny, callout) are *not* subject
to those rules: they change nothing and an editor is well served by plenty of
them. That asymmetry — cheap annotation, expensive picture change — runs
through the whole session.
"""
from __future__ import annotations

from typing import Optional, Sequence

from editing.recommend.schema import EditRecommendation, RecommendationSet
from editing.roughcut.schema import ClipPlacement, RoughCutPlan
from editing.roughcut.select import map_to_sequence
from editing.schema import StructureTimeline, TimelineSegment
from editing.style.presets import StylePreset
from editing.style.schema import LayerEvidence, LayerItem, item_id_for

#: A zoom shorter than this reads as a glitch rather than as emphasis.
MIN_ZOOM_SECONDS = 1.2

#: A punch arrives fast and holds; a push moves across the whole beat.
PUNCH_SECONDS = 0.7
PUSH_MIN_SECONDS = 2.0

#: Recommendation categories this layer realises.
ZOOM_CATEGORIES = {"punch_in": "punch_in", "slow_push_in": "slow_push_in"}

#: Importance -> the marker kind that flags it. Markers are how a strong moment
#: gets acknowledged in a style that will not zoom it.
IMPORTANCE_MARKERS = {
    "reveal": ("reveal_marker", "anticipation"),
    "danger": ("danger_marker", "tension"),
    "funny": ("funny_marker", "comedy"),
    "payoff": ("visual_callout", "payoff"),
}

#: HUD states that make any scale change unsafe.
PROTECTED_UI = ("inventory_open", "crafting_open", "chest_open", "map_open",
                "death_screen")


def build_emphasis(
    timeline: StructureTimeline,
    roughcut: RoughCutPlan,
    style: StylePreset,
    *,
    recommendations: Optional[RecommendationSet] = None,
    blocked_ranges: Sequence[tuple] = (),
) -> list[LayerItem]:
    """Emphasis candidates: zooms where safe, markers where not."""
    recommendations = recommendations or RecommendationSet()
    segments = {segment.segment_id: segment for segment in timeline.segments}
    items: list[LayerItem] = []

    for entry in recommendations.recommendations:
        if entry.category not in ZOOM_CATEGORIES:
            continue
        item = _zoom_item(entry, roughcut, style, segments, blocked_ranges)
        if item is not None:
            items.append(item)

    items.extend(_moment_markers(timeline, roughcut, style))
    items.sort(key=lambda item: item.start)
    return items


# ---------------------------------------------------------------------------
# Zooms
# ---------------------------------------------------------------------------

def _zoom_item(
    entry: EditRecommendation,
    roughcut: RoughCutPlan,
    style: StylePreset,
    segments: dict,
    blocked_ranges: Sequence[tuple],
) -> Optional[LayerItem]:
    kind = ZOOM_CATEGORIES[entry.category]
    start = map_to_sequence(roughcut.placements, entry.asset_id, entry.start)
    if start is None:
        return None
    placement = roughcut.placement_at(start)
    end = map_to_sequence(roughcut.placements, entry.asset_id, entry.end)

    item = LayerItem(
        item_id=item_id_for(kind, start, entry.recommendation_id),
        layer="emphasis",
        kind=kind,
        recommendation_id=entry.recommendation_id,
        placement_id=placement.placement_id if placement else "",
        start=start,
        end=end if (end and end > start) else start + PUNCH_SECONDS,
        source_start=entry.start,
        source_end=entry.end,
        asset_id=entry.asset_id,
        style=style.name,
        reason=entry.reason or f"{entry.category} proposed here",
        effect=entry.effects[0] if entry.effects else "impact",
        intensity=entry.intensity,
        priority=entry.priority,
        risks=list(entry.risks),
        evidence=LayerEvidence(
            visual_event_ids=list(entry.evidence.visual_event_ids),
            transcript_quotes=list(entry.evidence.transcript_quotes),
            audio_event_ids=list(entry.evidence.audio_event_ids),
            audio_types=list(entry.evidence.audio_types),
            segment_ids=list(placement.segment_ids) if placement else [],
            summary=entry.evidence.summary,
        ),
    )

    refusal = _zoom_refusal(
        entry, item, placement, style, segments, blocked_ranges
    )
    if refusal:
        _as_marker(item, style, refusal)
        return item

    scale = _scale_for(kind, entry, style)
    duration = _zoom_duration(kind, item, placement)
    item.payload = {
        "scale": scale,
        "from": 100.0,
        "duration": duration,
        "ceiling": style.max_zoom_scale if kind == "punch_in"
                   else style.max_push_scale,
    }
    item.premiere_ops = [{
        "op": "animate",
        "clip": placement.selector(),
        "component": "Motion",
        "property": "Scale",
        "from": 100.0,
        "to": round(scale, 3),
        "start": round(item.start, 3),
        "duration": round(duration, 3),
        "easing": "ease_out" if kind == "punch_in" else "ease_both",
        "relative_to": "sequence",
        "note": f"{kind} -> {scale:g}% [{style.name}] {entry.reason[:50]} "
                f"[{item.item_id}]",
    }]
    return item


def _zoom_refusal(
    entry: EditRecommendation,
    item: LayerItem,
    placement: Optional[ClipPlacement],
    style: StylePreset,
    segments: dict,
    blocked_ranges: Sequence[tuple],
) -> str:
    """Why this zoom must not be applied, or ""."""
    if not style.allows(item.kind):
        return f"the {style.name} style does not use {item.kind}."
    if not style.zooms_allowed:
        return (
            f"the {style.name} style does not scale the picture at all "
            f"(max_zoom_scale is {style.max_zoom_scale:g}%)."
        )
    if entry.status not in ("accepted", "downgraded"):
        return (
            f"the safety pass did not accept it: {entry.status_reason}"
            or "the safety pass did not accept it."
        )
    if placement is None:
        return "the footage this applies to is not in the rough cut."
    if placement.protected and not style.zoom_protected_clips:
        return (
            "the clip is a protected hold; the pacing layer said to leave this "
            "footage raw and this style does not override that."
        )
    if placement.protected and entry.priority < style.zoom_min_confidence:
        return (
            f"the clip is a protected hold and this style only overrides that "
            f"above {style.zoom_min_confidence:.0%} confidence; this scored "
            f"{entry.priority:.0%}."
        )
    if placement.speed != 1.0 and not style.zoom_retimed_clips:
        return (
            f"the clip is retimed to {placement.speed:g}x; zooming a sped-up "
            "clip compounds two edits on the same footage."
        )
    if placement.sequence_duration < MIN_ZOOM_SECONDS:
        return (
            f"the clip is only {placement.sequence_duration:.1f}s on the "
            f"timeline; a zoom under {MIN_ZOOM_SECONDS:g}s reads as a glitch."
        )

    hiding = _hides_gameplay(placement, segments)
    if hiding:
        return hiding

    for blocked in blocked_ranges:
        low, high = float(blocked[0]), float(blocked[1])
        reason = blocked[2] if len(blocked) > 2 else "the critic flagged it"
        if item.end > low and item.start < high:
            if "critic_flagged" not in item.risks:
                item.risks.append("critic_flagged")
            return f"the critic already flagged this moment: {reason}"
    return ""


def _hides_gameplay(placement: ClipPlacement, segments: dict) -> str:
    """Re-check the HUD rules against every segment this clip covers.

    The safety pass checked the *recommendation's* segment. The cut may have
    merged segments since, so the clip can now span footage the original
    recommendation never saw.

    Segments are found by **time overlap as well as by ID**. Matching on IDs
    alone means that if a placement's ``segment_ids`` do not resolve -- a
    hand-built plan, a timeline rebuilt since the cut -- this check silently
    finds nothing and the zoom is allowed. A safety check that degrades towards
    "permitted" when its lookup fails is worse than no check, so the overlap
    pass is the one that actually decides.
    """
    for segment in _segments_covering(placement, segments):
        for event in segment.events:
            if any(getattr(event.ui, name, False) for name in PROTECTED_UI):
                return (
                    "a full-screen UI is open somewhere in this clip; zooming "
                    "would hide what the viewer is reading."
                )
            if event.ui.low_health:
                return (
                    "low health is visible in this clip and is why the moment "
                    "is tense; a zoom risks cropping the HUD out of frame."
                )
    return ""


def _segments_covering(placement: ClipPlacement, segments: dict) -> list:
    """Every timeline segment this clip's source range touches."""
    found = []
    seen: set = set()
    for segment_id in placement.segment_ids:
        segment = segments.get(segment_id)
        if segment is not None and segment.segment_id not in seen:
            seen.add(segment.segment_id)
            found.append(segment)
    for segment in segments.values():
        if segment.segment_id in seen:
            continue
        if segment.asset_id != placement.asset_id:
            continue
        if segment.end > placement.source_in and segment.start < placement.source_out:
            seen.add(segment.segment_id)
            found.append(segment)
    return found


def _scale_for(
    kind: str, entry: EditRecommendation, style: StylePreset
) -> float:
    """How far to zoom, capped by the style.

    Intensity picks a point inside the style's range rather than a fixed value,
    so a ``high`` punch in ``fast_funny`` is genuinely stronger than a ``low``
    one — but the ceiling is applied last, so no intensity can exceed it.
    """
    ceiling = (
        style.max_zoom_scale if kind == "punch_in" else style.max_push_scale
    )
    span = max(0.0, ceiling - 100.0)
    fraction = {"low": 0.45, "medium": 0.7, "high": 1.0}.get(entry.intensity, 0.6)
    return round(min(ceiling, 100.0 + span * fraction), 3)


def _zoom_duration(
    kind: str, item: LayerItem, placement: Optional[ClipPlacement]
) -> float:
    room = placement.sequence_end - item.start if placement else item.duration
    if kind == "punch_in":
        # A punch is meant to feel abrupt: it arrives quickly and holds.
        return round(max(0.3, min(PUNCH_SECONDS, room)), 3)
    span = max(PUSH_MIN_SECONDS, item.duration)
    return round(max(MIN_ZOOM_SECONDS, min(span, room)), 3)


# ---------------------------------------------------------------------------
# Moment markers
# ---------------------------------------------------------------------------

def _moment_markers(
    timeline: StructureTimeline, roughcut: RoughCutPlan, style: StylePreset
) -> list[LayerItem]:
    """One marker per strong moment that survived the cut.

    These cost nothing and are how a restrained style still acknowledges a
    reveal or a scare: ``minimal_clean`` will not zoom the diamonds, but it will
    tell the editor exactly where they are.
    """
    items: list[LayerItem] = []
    seen: set = set()

    for segment in timeline.segments:
        pair = IMPORTANCE_MARKERS.get(segment.importance)
        if pair is None:
            continue
        kind, effect = pair
        if not style.allows(kind):
            continue

        start = map_to_sequence(
            roughcut.placements, segment.asset_id, segment.start
        )
        if start is None:
            continue
        # Two adjacent segments of the same importance are one moment.
        key = (kind, round(start, 1))
        if key in seen:
            continue
        seen.add(key)

        placement = roughcut.placement_at(start)
        detail = _marker_detail(segment)
        item = LayerItem(
            item_id=item_id_for(kind, start, segment.segment_id),
            layer="emphasis",
            kind=kind,
            placement_id=placement.placement_id if placement else "",
            start=start,
            end=start,
            source_start=segment.start,
            source_end=segment.end,
            asset_id=segment.asset_id,
            style=style.name,
            reason=f"the analysis pass rated this moment {segment.importance}"
                   + (f": {detail}" if detail else ""),
            effect=effect,
            priority=round(min(1.0, 0.5 + segment.usefulness * 0.4), 3),
            evidence=LayerEvidence(
                visual_event_ids=[e.event_id for e in segment.events][:5],
                audio_event_ids=[a.event_id for a in segment.audio_events][:5],
                audio_types=sorted(segment.audio_types()),
                transcript_quotes=[segment.said[:200]] if segment.said else [],
                segment_ids=[segment.segment_id],
                summary=segment.summary()[:200],
            ),
            payload={"placeholder": kind, "importance": segment.importance},
        )
        item.premiere_ops = [{
            "op": "marker.add",
            "time": round(start, 3),
            "name": style.marker_name(kind),
            "type": "comment",
            "comment": (
                f"{segment.importance.upper()}: {item.reason} | "
                + (f'said: "{segment.said[:80]}" | ' if segment.said else "")
                + f"[{item.item_id}]"
            )[:500],
            "note": f"{kind} [{style.name}]",
        }]
        items.append(item)
    return items


def _marker_detail(segment: TimelineSegment) -> str:
    parts = []
    threats = sorted({t for event in segment.events for t in event.threats})
    entities = sorted({e for event in segment.events for e in event.entities})
    if threats:
        parts.append("threats: " + ", ".join(threats[:3]))
    elif entities:
        parts.append("on screen: " + ", ".join(entities[:3]))
    if segment.audio_reaction is not None:
        parts.append(f"audio: {segment.audio_reaction.type}")
    return "; ".join(parts)


def _as_marker(item: LayerItem, style: StylePreset, reason: str) -> None:
    """A zoom that was refused still tells the editor what was intended.

    This is the difference between "the system declined to zoom here" and "the
    system did nothing here". The first is a decision an editor can overrule in
    two seconds; the second is invisible.
    """
    item.payload = {"placeholder": item.kind, "refused": reason}
    item.notes = (item.notes + " | " if item.notes else "") + reason
    if "placeholder_only" not in item.risks:
        item.risks.append("placeholder_only")
    item.premiere_ops = [{
        "op": "marker.add",
        "time": round(item.start, 3),
        "name": style.marker_name(item.kind) + "?",
        "type": "comment",
        "comment": (
            f"{item.kind} was NOT applied: {reason} | wanted because: "
            f"{item.reason} [{item.item_id}]"
        )[:500],
        "note": f"{item.kind} refused [{style.name}]",
    }]
