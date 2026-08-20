"""Choosing which moments of a rough cut deserve a second look.

Session 3 exported one representative frame per clip. That is the right answer
for "show me the cut", and the wrong one for "find what is broken in it" --
the mistakes an automatic assembly makes cluster at specific places:

* **cut points**, where a beat can be clipped off either end,
* **markers**, where the plan asserted something about the picture,
* **zooms**, which are the only thing here that can crop the HUD out of frame,
* **speed changes**, where footage no longer reads the way it was analysed,
* **text, caption and callout placeholders**, which claim screen space,
* **high-priority moments**, where a defect costs the most,
* and **a spread of sanity probes** through long stretches, because a critic
  that only ever looks where problems are expected will confirm the plan
  rather than test it.

So this module plans frames by *rule*, deduplicates them, and attaches the
context a single still cannot carry: what was said, what was heard, what the
vision layer saw, and what the cut actually did at that moment. Extraction
itself stays in ``editing.roughcut.review`` -- this decides where to look, that
does the looking.

Everything here is pure. No FFmpeg, no model, no Premiere: given a plan and a
timeline, the frame list is deterministic, which is what makes it testable and
what makes the critic's cache keys stable across runs.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from editing.recommend.schema import RecommendationSet
from editing.roughcut.schema import ClipPlacement, RoughCutPlan
from editing.schema import IMPORTANCE_WEIGHT, StructureTimeline

#: Marker categories that reserve screen space, and the edit kind they stand
#: for. These are placeholders -- no graphic exists yet -- but the critic still
#: needs to know one was planned here, because "is there room for text in this
#: frame" is a question only a picture can answer.
TEXT_CATEGORIES = {
    "text_overlay": "text",
    "caption_emphasis": "caption",
    "visual_callout": "callout",
}

#: Session 3 stamps the recommendation ID into an operation's ``note`` as a
#: trailing ``[r_...]``. The catalog rejects unknown parameters, so the note is
#: the only place a non-catalog field can travel -- reading it back here is
#: what lets a revision name the recommendation it is revising.
_REC_ID_RE = re.compile(r"\[([A-Za-z0-9_\-]{3,64})\]\s*$")


def recommendation_id_in(note: str) -> str:
    match = _REC_ID_RE.search(str(note or ""))
    return match.group(1) if match else ""


#: Keep reasons worth extra scrutiny: the moments the whole cut exists for.
HIGH_VALUE_REASONS = frozenset({"payoff", "reveal", "danger", "funny"})

#: When two planned frames land closer than this on the sequence, they are the
#: same look at the same moment and one is dropped.
DEFAULT_MIN_GAP = 0.75

#: How far inside a cut to sample. Far enough past the edit to be a real frame
#: of the shot, close enough that a clipped beat is still visible.
DEFAULT_EDGE_OFFSET = 0.35

#: Stretches longer than this get sanity probes sprinkled through them.
DEFAULT_LONG_SECTION = 12.0

#: Which kind wins when two rules pick the same moment. A zoom that also
#: happens to be a clip start is a zoom -- that is the thing most likely to be
#: wrong, and the frame is labelled for whichever question matters most.
_KIND_PRIORITY = {
    "zoom": 90,
    "text_placeholder": 80,
    "marker": 70,
    "clip_start": 60,
    "clip_end": 60,
    "high_priority": 50,
    "speed_change": 40,
    "clip_sample": 30,
    "sanity": 10,
}


@dataclass
class CoverageOptions:
    """Which rules run, and how densely.

    Every default errs towards *fewer, better-chosen* frames. Each frame is a
    model call, and a critic pass over 400 near-identical stills is both slow
    and worse: a long run of "this looks fine" drowns the two findings that
    mattered.
    """

    #: Sample just inside the head and tail of each clip.
    cut_points: bool = True
    markers: bool = True
    zooms: bool = True
    speed_changes: bool = True
    text_placeholders: bool = True
    high_priority: bool = True
    #: Random-but-deterministic probes through long stretches.
    sanity: bool = True

    edge_offset: float = DEFAULT_EDGE_OFFSET
    min_gap: float = DEFAULT_MIN_GAP
    long_section_seconds: float = DEFAULT_LONG_SECTION
    #: Sanity probes per long stretch.
    sanity_per_section: int = 1
    #: Clips shorter than this get one frame, not a head and a tail.
    min_clip_for_edges: float = 1.6
    #: Hard ceiling on the whole pass. Trimmed lowest-priority-kind first.
    max_frames: int = 120
    #: Priority at or above which a moment counts as high-priority.
    priority_threshold: float = 0.7

    def to_dict(self) -> dict:
        return {
            "cut_points": self.cut_points,
            "markers": self.markers,
            "zooms": self.zooms,
            "speed_changes": self.speed_changes,
            "text_placeholders": self.text_placeholders,
            "high_priority": self.high_priority,
            "sanity": self.sanity,
            "edge_offset": self.edge_offset,
            "min_gap": self.min_gap,
            "long_section_seconds": self.long_section_seconds,
            "sanity_per_section": self.sanity_per_section,
            "max_frames": self.max_frames,
            "priority_threshold": self.priority_threshold,
        }


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class _Candidate:
    """A moment worth looking at, before deduplication."""

    sequence_time: float
    placement: ClipPlacement
    kind: str
    reason: str

    @property
    def rank(self) -> int:
        return _KIND_PRIORITY.get(self.kind, 0)


def plan_coverage_frames(
    plan: RoughCutPlan,
    *,
    timeline: Optional[StructureTimeline] = None,
    recommendations: Optional[RecommendationSet] = None,
    options: Optional[CoverageOptions] = None,
):
    """Every review frame this cut deserves, in sequence order.

    Returns ``ReviewFrame`` records with ``path`` still empty --
    ``roughcut.review.export_frames`` fills that in. Splitting it this way
    means the whole selection can be listed, diffed and asserted on without
    extracting a single JPEG.
    """
    from editing.roughcut.review import ReviewFrame  # local: avoids a cycle

    options = options or CoverageOptions()
    recommendations = recommendations or RecommendationSet()

    candidates: list[_Candidate] = []
    for placement in plan.placements:
        candidates.extend(_clip_candidates(placement, plan, options))
    candidates.extend(_marker_candidates(plan, options))
    candidates.extend(_zoom_candidates(plan, options))
    candidates.extend(
        _priority_candidates(plan, recommendations, options)
    )

    kept = _dedupe(candidates, min_gap=options.min_gap)
    kept = _cap(kept, options.max_frames)

    frames = []
    for index, candidate in enumerate(kept):
        placement = candidate.placement
        # Clamp before converting: a cut-point probe deliberately aims at a
        # boundary, and rounding can put it a hair outside the clip it belongs
        # to. Losing the frame there would drop exactly the frames this pass
        # exists to look at.
        clamped = min(
            max(candidate.sequence_time, placement.sequence_start),
            placement.sequence_end,
        )
        source_time = placement.sequence_to_source(clamped)
        if source_time is None:  # pragma: no cover - clamped is always inside
            source_time = placement.source_in
        inside = [
            marker.name for marker in plan.markers
            if placement.sequence_start <= marker.time < placement.sequence_end
        ]
        frames.append(ReviewFrame(
            frame_id=_frame_id(placement.placement_id, candidate.sequence_time,
                               candidate.kind),
            placement_id=placement.placement_id,
            path="",
            sequence_time=round(candidate.sequence_time, 3),
            source_time=round(source_time, 3),
            source_file=placement.source_file,
            asset_id=placement.asset_id,
            keep_reason=placement.keep_reason,
            speed=placement.speed,
            protected=placement.protected,
            recommendation_ids=list(placement.recommendation_ids),
            segment_ids=list(placement.segment_ids),
            marker_names=sorted(set(inside)),
            note=placement.notes,
            sequence_name=plan.sequence_name,
            frame_kind=candidate.kind,
            reason=candidate.reason,
            clip_duration=placement.sequence_duration,
        ))

    if timeline is not None or recommendations.recommendations:
        enrich(frames, plan, timeline=timeline, recommendations=recommendations)
    return frames


def _clip_candidates(
    placement: ClipPlacement, plan: RoughCutPlan, options: CoverageOptions
) -> list[_Candidate]:
    """Cut points, speed changes and sanity probes for one clip."""
    out: list[_Candidate] = []
    duration = placement.sequence_duration
    if duration <= 0:
        return out

    short = duration < options.min_clip_for_edges
    offset = min(options.edge_offset, duration / 3.0)

    if options.cut_points:
        if short:
            out.append(_Candidate(
                placement.sequence_midpoint, placement, "clip_sample",
                f"the clip is only {duration:.1f}s, so one frame covers it",
            ))
        else:
            out.append(_Candidate(
                placement.sequence_start + offset, placement, "clip_start",
                "just after the incoming cut",
            ))
            out.append(_Candidate(
                placement.sequence_end - offset, placement, "clip_end",
                "just before the outgoing cut",
            ))
    else:
        out.append(_Candidate(
            placement.sequence_midpoint, placement, "clip_sample",
            "representative frame for the clip",
        ))

    if options.speed_changes and placement.speed != 1.0:
        out.append(_Candidate(
            placement.sequence_midpoint, placement, "speed_change",
            f"this clip plays at {placement.speed:g}x",
        ))

    if options.sanity and duration >= options.long_section_seconds:
        out.extend(_sanity_candidates(placement, options))

    return out


def _sanity_candidates(
    placement: ClipPlacement, options: CoverageOptions
) -> list[_Candidate]:
    """Probes at pseudo-random points inside a long clip.

    Deterministic: the offsets come from a hash of the placement ID, so the
    same cut always samples the same moments. Random *placement* is the point
    (an evenly-spaced probe would keep landing on the same beat of a repetitive
    loop), but random *results* would make the critic pass unreproducible and
    its cache useless.
    """
    out: list[_Candidate] = []
    duration = placement.sequence_duration
    count = max(1, int(options.sanity_per_section))
    digest = hashlib.sha256(placement.placement_id.encode("utf-8")).digest()

    for index in range(count):
        # Confine each probe to its own slice, so two probes cannot collide.
        slice_start = duration * index / count
        slice_span = duration / count
        fraction = digest[index % len(digest)] / 255.0
        # Stay clear of both edges -- the cut-point probes own those.
        inset = 0.15 + 0.7 * fraction
        at = placement.sequence_start + slice_start + slice_span * inset
        out.append(_Candidate(
            at, placement, "sanity",
            f"sanity probe in a {duration:.0f}s stretch",
        ))
    return out


def _marker_candidates(
    plan: RoughCutPlan, options: CoverageOptions
) -> list[_Candidate]:
    """A frame at each marker, labelled by what the marker claims."""
    out: list[_Candidate] = []
    for marker in plan.markers:
        is_text = marker.category in TEXT_CATEGORIES
        if is_text and not options.text_placeholders:
            continue
        if not is_text and not options.markers:
            continue
        placement = plan.placement_at(marker.time)
        if placement is None:
            continue
        kind = "text_placeholder" if is_text else "marker"
        out.append(_Candidate(
            marker.time, placement, kind,
            f"at the {marker.name} marker"
            + (f" ({TEXT_CATEGORIES[marker.category]} placeholder)"
               if is_text else ""),
        ))
    return out


def _zoom_candidates(
    plan: RoughCutPlan, options: CoverageOptions
) -> list[_Candidate]:
    """A frame at the *end* of each zoom, where it is strongest.

    Sampling the middle of a push-in would show a scale nobody complained
    about. The question a critic is being asked here is "did this go too far",
    and that is only answerable at the point it went furthest.
    """
    if not options.zooms:
        return []
    out: list[_Candidate] = []
    for op in plan.ops:
        if op.get("op") != "animate":
            continue
        if str(op.get("property") or "").lower() != "scale":
            continue
        start = float(op.get("start") or 0.0)
        duration = float(op.get("duration") or 0.0)
        at = start + duration
        placement = plan.placement_at(at) or plan.placement_at(start)
        if placement is None:
            continue
        at = min(max(at, placement.sequence_start), placement.sequence_end - 0.01)
        out.append(_Candidate(
            at, placement, "zoom",
            f"end of a zoom to {op.get('to')}% "
            f"(from {op.get('from', 100)}%)",
        ))
    return out


def _priority_candidates(
    plan: RoughCutPlan,
    recommendations: RecommendationSet,
    options: CoverageOptions,
) -> list[_Candidate]:
    """A frame in the middle of each moment the cut exists for."""
    if not options.high_priority:
        return []
    priorities = {
        entry.recommendation_id: entry.priority
        for entry in recommendations.recommendations
    }
    out: list[_Candidate] = []
    for placement in plan.placements:
        best = max(
            (priorities.get(rid, 0.0) for rid in placement.recommendation_ids),
            default=0.0,
        )
        strong = placement.keep_reason in HIGH_VALUE_REASONS
        if not strong and best < options.priority_threshold:
            continue
        out.append(_Candidate(
            placement.sequence_midpoint, placement, "high_priority",
            f"{placement.keep_reason} moment"
            + (f", priority {best:.2f}" if best else ""),
        ))
    return out


def _dedupe(candidates: Sequence[_Candidate], *, min_gap: float) -> list[_Candidate]:
    """Collapse near-identical moments, keeping the most specific label.

    **Only within one clip.** Two candidates a fraction of a second apart on
    either side of a cut are not the same moment -- they are the last frame of
    one shot and the first frame of the next, from different source files, and
    they answer different questions. Collapsing across the cut would drop every
    incoming-cut frame in the whole review, since the two edge probes are
    always closer together than the gap threshold.
    """
    ordered = sorted(
        candidates, key=lambda c: (c.sequence_time, -c.rank, c.kind)
    )
    last_in_clip: dict = {}
    kept: list[_Candidate] = []
    for candidate in ordered:
        key = candidate.placement.placement_id
        previous = last_in_clip.get(key)
        if previous is not None and (
            candidate.sequence_time - previous.sequence_time
        ) < min_gap:
            # Same moment in the same shot. Keep whichever question matters
            # more; if the new one wins, it replaces the old in place.
            if candidate.rank > previous.rank:
                kept[kept.index(previous)] = candidate
                last_in_clip[key] = candidate
            continue
        kept.append(candidate)
        last_in_clip[key] = candidate
    return kept


def _cap(candidates: list[_Candidate], limit: int) -> list[_Candidate]:
    """Trim to ``limit``, dropping the least informative kinds first."""
    if limit <= 0 or len(candidates) <= limit:
        return candidates
    ranked = sorted(
        enumerate(candidates), key=lambda pair: (-pair[1].rank, pair[0])
    )
    keep = {index for index, _ in ranked[:limit]}
    return [c for i, c in enumerate(candidates) if i in keep]


def _frame_id(placement_id: str, sequence_time: float, kind: str) -> str:
    stem = placement_id[2:] if placement_id.startswith("p_") else placement_id
    return f"rf_{stem}_{kind}_{int(round(sequence_time * 100)):07d}"


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def enrich(
    frames,
    plan: RoughCutPlan,
    *,
    timeline: Optional[StructureTimeline] = None,
    recommendations: Optional[RecommendationSet] = None,
    window: float = 1.5,
):
    """Attach what was said, heard, seen and done at each frame's moment.

    Mutates and returns ``frames``. Separate from planning so a frame list read
    back from an old manifest can be re-contextualised against a rebuilt
    timeline without re-extracting anything.
    """
    recommendations = recommendations or RecommendationSet()
    segments_by_asset: dict = {}
    if timeline is not None:
        for segment in timeline.segments:
            segments_by_asset.setdefault(segment.asset_id, []).append(segment)

    by_id = {
        entry.recommendation_id: entry
        for entry in recommendations.recommendations
    }
    placements = {p.placement_id: p for p in plan.placements}

    for frame in frames:
        placement = placements.get(frame.placement_id)
        _apply_segment_context(
            frame, segments_by_asset.get(frame.asset_id, []), window=window
        )
        _apply_edit_context(frame, plan, placement)
        _apply_priority(frame, by_id)
    return frames


def _apply_segment_context(frame, segments, *, window: float) -> None:
    """Everything the timeline knows about this moment in the source file."""
    lo = frame.source_time - window
    hi = frame.source_time + window
    nearby = [
        segment for segment in segments
        if segment.end > lo and segment.start < hi
    ]
    if not nearby:
        return

    said: list[str] = []
    audio: list[dict] = []
    visual_ids: list[str] = []
    actions: list[str] = []
    entities: list[str] = []
    threats: list[str] = []
    ui_flags: list[str] = []
    environment = ""
    importance = ""

    for segment in nearby:
        if segment.said and segment.said not in said:
            said.append(segment.said)
        for event in segment.audio_events:
            if event.end <= lo or event.start >= hi:
                continue
            audio.append({
                "type": event.type,
                "start": round(event.start, 3),
                "end": round(event.end, 3),
                "confidence": round(event.confidence, 3),
                "detection": event.detection,
            })
        for event in segment.events:
            if event.end <= lo or event.start >= hi:
                continue
            if event.event_id not in visual_ids:
                visual_ids.append(event.event_id)
            environment = environment or event.environment
            importance = importance or event.importance
            for action in event.actions:
                if action not in actions:
                    actions.append(action)
            for entity in event.entities:
                if entity not in entities:
                    entities.append(entity)
            for threat in event.threats:
                if threat not in threats:
                    threats.append(threat)
            for flag in _ui_flags(event.ui):
                if flag not in ui_flags:
                    ui_flags.append(flag)

    frame.transcript = " ".join(said)[:600]
    frame.audio_events = audio[:20]
    frame.audio_types = sorted({entry["type"] for entry in audio})
    frame.visual_event_ids = visual_ids[:20]
    frame.environment = environment
    frame.actions = actions[:10]
    frame.entities = entities[:15]
    frame.threats = threats[:10]
    frame.importance = importance or max(
        (segment.importance for segment in nearby),
        key=lambda level: IMPORTANCE_WEIGHT.get(level, 0.0),
        default="",
    )
    frame.ui_flags = ui_flags


def _ui_flags(ui) -> list[str]:
    """The HUD state, as the flags that are actually true."""
    flags = []
    for name in (
        "inventory_open", "crafting_open", "chest_open", "death_screen",
        "achievement_toast", "low_health", "chat_open", "map_open",
    ):
        if getattr(ui, name, False):
            flags.append(name)
    if getattr(ui, "coordinates", ""):
        flags.append("debug_screen")
    return flags


def _apply_edit_context(frame, plan: RoughCutPlan, placement) -> None:
    """What the rough cut does to the picture at this exact moment."""
    edits: list[dict] = []

    if placement is not None and placement.speed != 1.0:
        edits.append({
            "kind": "speed",
            "rate": placement.speed,
            "detail": f"this clip plays at {placement.speed:g}x",
        })
    if placement is not None and placement.protected:
        edits.append({
            "kind": "protected",
            "detail": "a hold: the planner said to leave this footage alone",
        })

    for op in plan.ops:
        if op.get("op") != "animate":
            continue
        if str(op.get("property") or "").lower() != "scale":
            continue
        start = float(op.get("start") or 0.0)
        duration = float(op.get("duration") or 0.0)
        if not (start - 0.25 <= frame.sequence_time <= start + duration + 0.25):
            continue
        edits.append({
            "kind": "zoom",
            "from": op.get("from", 100.0),
            "to": op.get("to"),
            "start": round(start, 3),
            "duration": round(duration, 3),
            "detail": f"a zoom to {op.get('to')}% ends here",
            "note": str(op.get("note") or "")[:200],
            "recommendation_id": recommendation_id_in(op.get("note")),
        })

    for marker in plan.markers:
        if abs(marker.time - frame.sequence_time) > 1.0:
            continue
        kind = TEXT_CATEGORIES.get(marker.category, "marker")
        edits.append({
            "kind": kind,
            "name": marker.name,
            "at": round(marker.time, 3),
            "detail": marker.comment[:200],
            "recommendation_id": marker.recommendation_id,
            "category": marker.category,
        })

    frame.applied_edits = edits[:20]


def _apply_priority(frame, by_id: dict) -> None:
    best = 0.0
    for rid in frame.recommendation_ids:
        entry = by_id.get(rid)
        if entry is not None:
            best = max(best, entry.priority)
    frame.priority = round(best, 3)
