"""Deliberate transitions.

``transition.apply`` has been in the catalog and unused. The reason to be
careful about connecting it is that transitions are the single easiest thing in
an editor to overdo: a dissolve on every cut is the visual signature of a
system that has a feature and wants to show it.

So the rule here is that a transition needs an *argument*, not an opportunity.
There are exactly three arguments this pass accepts:

* **a scene change** -- the clips either side come from different source files,
  or the cut sits on a boundary the structure layer marked as a scene change.
  A dissolve there means "time passed"; a hard cut there means "these are the
  same moment", which would be a lie.
* **a time jump** -- the two ranges come from the same source but are far apart
  in it. Same reasoning.
* **the ends of the episode** -- a fade from and to black.

Everything else gets a hard cut, and the refusal is recorded so a report can
say "nineteen cuts were considered and three earned a transition".
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.conform.schema import TransitionDecision, decision_id_for

logger = logging.getLogger("nova.editing.conform.transitions")

#: Gap in the *source* file, in seconds, past which two adjacent ranges are a
#: time jump rather than a continuous shot.
TIME_JUMP_SECONDS = 20.0

#: Length of an ordinary dissolve. Long enough to read as deliberate, short
#: enough not to eat the material either side.
DISSOLVE_SECONDS = 0.5

#: Length of the fades at the two ends of the episode.
OPENING_FADE = 0.75
CLOSING_FADE = 1.0

#: A clip shorter than twice the transition cannot carry one: the dissolve
#: would consume the whole shot.
MIN_CLIP_FOR_TRANSITION = 1.5

#: The transitions this pass will ask for, by name. These are Premiere's own
#: effect names; the host looks them up and reports honestly if a build does
#: not have one.
DISSOLVE = "Cross Dissolve"
DIP_TO_BLACK = "Dip to Black"


def decide(
    placements: Sequence,
    *,
    track: str = "V1",
    max_transitions: int = 6,
    fade_ends: bool = True,
    scene_changes: Optional[Sequence[float]] = None,
) -> list[TransitionDecision]:
    """One decision per cut, most of them refusals.

    ``placements`` are the rough cut's :class:`ClipPlacement` objects, in
    playback order. Only ``asset_id``, ``source_in``/``source_out`` and
    ``sequence_start``/``sequence_end`` are read.
    """
    decisions: list[TransitionDecision] = []
    boundaries = [float(t) for t in (scene_changes or ())]
    clips = list(placements)
    if not clips:
        return decisions

    def near_boundary(at: float) -> bool:
        return any(abs(at - b) <= 1.0 for b in boundaries)

    # -- the two ends -----------------------------------------------------
    if fade_ends:
        first = clips[0]
        decisions.append(TransitionDecision(
            decision_id=decision_id_for("transition", 0.0, "open"),
            applied=_long_enough(first, OPENING_FADE),
            at=float(getattr(first, "sequence_start", 0.0)),
            clip_index=0, track=track, edge="in",
            transition=DIP_TO_BLACK, duration=OPENING_FADE,
            reason="the episode opens out of black",
            reject_reason="" if _long_enough(first, OPENING_FADE)
                          else "clip_too_short",
            evidence=["first clip of the cut"],
        ))

    # -- the cuts between clips -------------------------------------------
    for index, (before, after) in enumerate(zip(clips, clips[1:])):
        at = float(getattr(before, "sequence_end", 0.0))
        same_source = (getattr(before, "asset_id", None)
                       == getattr(after, "asset_id", None))
        source_gap = abs(
            float(getattr(after, "source_in", 0.0))
            - float(getattr(before, "source_out", 0.0))
        )

        decision = TransitionDecision(
            decision_id=decision_id_for("transition", at, f"cut{index}"),
            at=at, clip_index=index, track=track, edge="out",
            transition=DISSOLVE, duration=DISSOLVE_SECONDS,
        )

        if not same_source:
            decision.reason = (
                "the shot either side of this cut comes from a different "
                "recording, so the cut is a scene change"
            )
            decision.evidence.append("adjacent clips have different sources")
        elif source_gap >= TIME_JUMP_SECONDS:
            decision.reason = (
                f"{source_gap:.0f}s of the recording was cut out here, so the "
                "two shots are not the same moment"
            )
            decision.evidence.append(
                f"source gap {source_gap:.1f}s >= {TIME_JUMP_SECONDS:.0f}s"
            )
        elif near_boundary(at):
            decision.reason = (
                "the structure layer marked a scene change at this point"
            )
            decision.evidence.append("on a marked scene boundary")
        else:
            decision.reject_reason = "ordinary_cut"
            decision.reason = (
                "a continuous shot cut to itself; a hard cut is correct here"
            )
            decisions.append(decision)
            continue

        if not (_long_enough(before, DISSOLVE_SECONDS)
                and _long_enough(after, DISSOLVE_SECONDS)):
            decision.reject_reason = "clip_too_short"
            decision.reason += (
                f" -- but a clip either side is under "
                f"{MIN_CLIP_FOR_TRANSITION:.1f}s, so a dissolve would eat it"
            )
            decisions.append(decision)
            continue

        decision.applied = True
        decisions.append(decision)

    if fade_ends:
        last = clips[-1]
        decisions.append(TransitionDecision(
            decision_id=decision_id_for("transition",
                                        float(getattr(last, "sequence_end", 0.0)), "close"),
            applied=_long_enough(last, CLOSING_FADE),
            at=float(getattr(last, "sequence_end", 0.0)),
            clip_index=len(clips) - 1, track=track, edge="out",
            transition=DIP_TO_BLACK, duration=CLOSING_FADE,
            reason="the episode ends into black",
            reject_reason="" if _long_enough(last, CLOSING_FADE)
                          else "clip_too_short",
            evidence=["last clip of the cut"],
        ))

    # -- the ceiling ------------------------------------------------------
    # Applied to the *middle* transitions only: the two end fades are
    # structural rather than decorative, and cutting them to satisfy a budget
    # would leave the episode starting on a hard frame.
    middles = [d for d in decisions if d.applied and d.transition == DISSOLVE]
    if len(middles) > max_transitions:
        for extra in middles[max_transitions:]:
            extra.applied = False
            extra.reject_reason = "density_limit"
            extra.reason += (
                f" -- refused: already at the ceiling of {max_transitions} "
                "transitions for this episode"
            )
    return decisions


def _long_enough(placement, needed: float) -> bool:
    duration = float(getattr(placement, "sequence_duration", 0.0)) or (
        float(getattr(placement, "sequence_end", 0.0))
        - float(getattr(placement, "sequence_start", 0.0))
    )
    return duration >= max(MIN_CLIP_FOR_TRANSITION, needed * 2.0)


def transition_ops(decisions: Sequence[TransitionDecision]) -> list[dict]:
    """The accepted transitions, as catalog operations."""
    ops: list[dict] = []
    for decision in decisions:
        if not decision.applied:
            continue
        ops.append({
            "op": "transition.apply",
            "clip": {"track": decision.track, "index": decision.clip_index},
            "transition": decision.transition,
            "edge": decision.edge,
            "duration": round(decision.duration, 3),
            "note": decision.reason[:200],
        })
    return ops
