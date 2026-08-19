"""Running the layers in order.

Thin on purpose: the judgement lives in ``layers``, and this module's job is to
sequence them, keep track of which layer proposed what, and make sure the
safety pass sees everything before anything is called final.

The planner is deterministic and free — no model call. That is a deliberate
choice for this session: a recommendation you can trace to a rule is one you can
argue with and tune, and the evidence is already structured by the time it gets
here. A model pass can be added later on top of these recommendations without
changing the schema.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Sequence

from editing.recommend import layers as layer_module
from editing.recommend.schema import EditRecommendation, RecommendationSet
from editing.schema import StructureTimeline, TimelineSegment


@dataclass
class PlannerOptions:
    """Knobs for how aggressive the planner is allowed to be."""

    style: str = "cinematic_minecraft"
    #: Seconds of footage per permitted active edit. Higher = calmer edit.
    budget_seconds: float = layer_module.ACTIVE_EDIT_BUDGET_SECONDS
    #: Minimum spacing between two active edits of the same category.
    min_repeat_gap: float = layer_module.MIN_REPEAT_GAP
    #: Skip the safety pass. For inspecting what the layers proposed before
    #: anything was pruned -- never for producing a real plan.
    skip_safety: bool = False
    #: Only consider segments at or above this usefulness score.
    min_segment_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "style": self.style,
            "budget_seconds": self.budget_seconds,
            "min_repeat_gap": self.min_repeat_gap,
            "skip_safety": self.skip_safety,
            "min_segment_score": self.min_segment_score,
        }


def plan_recommendations(
    timeline: StructureTimeline,
    *,
    options: Optional[PlannerOptions] = None,
) -> RecommendationSet:
    """Run every layer over a structure timeline and return the result."""
    options = options or PlannerOptions()
    segments = [
        segment for segment in timeline.segments
        if segment.usefulness >= options.min_segment_score
    ]

    warnings: list[str] = []
    if not segments:
        warnings.append(
            "No timeline segments to plan from. Run `analyze` and `timeline` first."
        )
        return RecommendationSet(
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            style=options.style,
            warnings=warnings,
        )

    if not any(segment.audio_events for segment in segments):
        warnings.append(
            "No audio events on this timeline, so audio-driven recommendations "
            "cannot be made. Run `audio` to add them."
        )
    if not any(segment.has_speech for segment in segments):
        warnings.append(
            "No transcript on this timeline, so narration/visual contrast "
            "cannot be detected."
        )

    collected: list[EditRecommendation] = []
    counts: dict = {}

    for name, run in layer_module.PROPOSING_LAYERS:
        # The visual layer reads what pacing already decided, so it can respect
        # a hold instead of arguing with it.
        produced = (
            run(segments, collected) if name == "visual" else run(segments)
        )
        counts[name] = len(produced)
        collected.extend(produced)

    collected = _dedupe(collected)

    if not options.skip_safety:
        collected = layer_module.layer_safety(
            collected, segments,
            budget_seconds=options.budget_seconds,
            min_repeat_gap=options.min_repeat_gap,
        )
        counts["safety_rejected"] = sum(
            1 for entry in collected if entry.status == "rejected"
        )
        counts["safety_downgraded"] = sum(
            1 for entry in collected if entry.status == "downgraded"
        )
        # An edit pushed all the way down lands on "hold", not "downgraded".
        # Counting only the latter would report a busy safety pass as idle.
        counts["safety_held"] = sum(
            1 for entry in collected if entry.status == "hold" and entry.status_reason
        )
    else:
        warnings.append(
            "Safety pass skipped -- these recommendations have NOT been checked "
            "for over-editing and must not be used to build a plan."
        )

    collected.sort(key=lambda entry: (entry.asset_id, entry.start, -entry.priority))

    return RecommendationSet(
        recommendations=collected,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        style=options.style,
        layer_counts=counts,
        warnings=warnings,
    )


def _dedupe(entries: Sequence[EditRecommendation]) -> list[EditRecommendation]:
    """Keep the highest-priority proposal per (segment, category).

    Two layers can legitimately reach the same conclusion -- polish and visual
    both have reasons to flag an open inventory. Keeping both would double-count
    it in the density budget and clutter the report.
    """
    best: dict = {}
    for entry in entries:
        key = (entry.asset_id, round(entry.start, 3), round(entry.end, 3),
               entry.category)
        current = best.get(key)
        if current is None or entry.priority > current.priority:
            best[key] = entry
    return list(best.values())


def summarise_segments(segments: Sequence[TimelineSegment]) -> dict:
    """What the planner was working from, for the report header."""
    return {
        "segments": len(segments),
        "with_speech": sum(1 for s in segments if s.has_speech),
        "with_audio": sum(1 for s in segments if s.audio_events),
        "dead_air": sum(1 for s in segments if s.is_dead_air),
        "covered_seconds": round(sum(s.duration for s in segments), 2),
    }
