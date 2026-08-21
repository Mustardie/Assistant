"""Assembling the retention plan from the memory.

Thin by design. The detectors, selectors and suggestion builders each own their
own judgement; this module runs them in the right order, collects the results
into one ``EpisodeRetentionPlan``, and records what the plan could not see.

The plan is built *from* a memory and never rebuilds one. That separation is
what lets a person disagree with a suggestion, go back to the beat it came
from, and change their mind about the suggestion without the story underneath
it moving.
"""
from __future__ import annotations

import time
from typing import Optional

from editing.episode import hooks as hooks_module
from editing.episode import risks as risks_module
from editing.episode import suggest as suggest_module
from editing.episode import track as track_module
from editing.episode.schema import (
    EpisodeMemory, EpisodeRetentionPlan, MIN_EDIT_CONFIDENCE,
)
from editing.episode.track import EpisodeTrack
from editing.schema import StructureTimeline


def build(
    memory: EpisodeMemory,
    *,
    timeline: Optional[StructureTimeline] = None,
    roughcut=None,
    track: Optional[EpisodeTrack] = None,
    hook_limit: int = hooks_module.HOOK_LIMIT,
) -> EpisodeRetentionPlan:
    """Risks, hooks, a climax, an ending and the suggestions that follow.

    The track is rebuilt from the timeline when one is not passed in, because
    several detectors need the slots underneath the beats -- measured silence,
    motion, and what was actually said across a cut do not survive into the
    merged beat list. Passing the track in avoids doing that work twice.
    """
    if track is None:
        if timeline is None:
            raise ValueError(
                "build() needs either a track or the timeline to rebuild one "
                "from; the risk detectors read slots, not just beats"
            )
        track = track_module.build(timeline, roughcut)

    plan = EpisodeRetentionPlan(
        episode_id=memory.episode_id,
        name=memory.name,
        sequence_name=memory.sequence_name,
        timebase=memory.timebase,
        duration=memory.duration,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    if memory.is_empty:
        plan.warnings.append(
            "the episode memory is empty, so there is nothing to plan against; "
            "build the memory first and check its warnings"
        )
        return plan

    if memory.timebase == "timeline":
        plan.warnings.append(
            "these times are the synthetic timeline ordering, not sequence "
            "time: no Premiere sequence looks like this, so a consumer has to "
            "go through segment_ids rather than using the numbers directly"
        )

    plan.risks = risks_module.detect(memory, track)
    plan.hooks = hooks_module.find_hooks(memory, track, limit=hook_limit)
    plan.climax, plan.climax_alternatives = hooks_module.find_climax(
        memory, track)
    plan.ending, plan.ending_alternatives = hooks_module.find_ending(
        memory, track)
    plan.midpoint_reset = suggest_module.midpoint_reset(
        memory, track, hooks_module.midpoint_moment(memory, track))
    plan.suggestions = suggest_module.build(
        memory, track, plan.risks,
        climax=plan.climax, ending=plan.ending,
    )
    if plan.midpoint_reset is not None:
        plan.suggestions = suggest_module.dedupe(
            plan.suggestions + [plan.midpoint_reset]
        )

    _add_warnings(plan, memory, track)
    return plan


def _add_warnings(
    plan: EpisodeRetentionPlan, memory: EpisodeMemory, track: EpisodeTrack
) -> None:
    """Say what the plan could not see, in the plan itself.

    A reader who does not know the critic never ran, or that there was no
    transcript, will read a short risk list as "the episode is fine" rather
    than "half the detectors had nothing to work with".
    """
    if not plan.hooks:
        plan.warnings.append(
            "no hook candidate cleared the floor; the episode has no moment "
            "that carries stakes a stranger could read with no context"
        )
    if plan.climax is None and plan.climax_alternatives:
        plan.warnings.append(
            "no single moment stands out as the peak -- the top candidates "
            "are within "
            f"{hooks_module.MIN_CLIMAX_MARGIN:.2f} of each other, so they are "
            "listed as alternatives rather than one being picked"
        )
    if plan.ending is None:
        plan.warnings.append(
            "nothing near the end reads as a resolution, payoff or sign-off"
        )
    if not track.has_speech:
        plan.warnings.append(
            "no transcript: the objective, open-loop and callback detectors "
            "had nothing to read, so their risks are absent rather than clear"
        )
    if not track.has_motion:
        plan.warnings.append(
            "motion was not probed, so low_visual_change did not run"
        )
    auto = len(plan.auto_safe_suggestions)
    if plan.suggestions and auto == 0:
        plan.warnings.append(
            f"none of the {len(plan.suggestions)} suggestions cleared the "
            f"{MIN_EDIT_CONFIDENCE:.2f} confidence needed to be applied "
            "automatically; every one of them is a marker for a person"
        )


def summarise(plan: EpisodeRetentionPlan) -> dict:
    """The numbers a caller wants for a one-line status."""
    stats = plan.stats()
    return {
        "risks": stats["risks"],
        "high_severity": stats["high_severity"],
        "hooks": stats["hooks"],
        "has_climax": stats["has_climax"],
        "has_ending": stats["has_ending"],
        "suggestions": stats["suggestions"],
        "auto_safe": stats["auto_safe"],
        "marker_only": stats["marker_only"],
    }
