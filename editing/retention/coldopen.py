"""Choosing what the episode opens on.

The single highest-leverage edit in the whole system, and the one a
chronological clip cutter can never make: the best thing in the episode is
usually nine minutes in, and a viewer who leaves at fifteen seconds never sees
it.

## What makes a cold open

Session 8's ``HookCandidate`` already ranks moments by hook-ness -- danger,
mystery, failure, comedy, reveal -- with a score, a viewer question, the
seconds of prior context it needs, and where its payoff is. This module does
not re-rank them. It applies the rules that turn "the strongest moment" into
"a usable opening", and every one of them can veto:

* **Long enough to land, short enough to be a tease.** 5-20 seconds.
* **Not boring.** A hook over walking, sorting or a menu is refused however
  well it scored. This is the most common failure and the cheapest to prevent.
* **Standing on its own.** A moment nobody can follow without the ten minutes
  before it is not a hook; it is the middle of a scene. Judged from whether
  the footage itself carries speech or a strong label -- *not* from
  ``HookCandidate.setup_seconds``, which despite its name and docstring is
  set by Session 8 to the beat's position in the episode. Reading it as
  "context needed" rejects every hook past the first few seconds, which is
  every hook worth having.
* **Not the ending.** Opening on the last thirty seconds spoils the episode
  to save fifteen seconds of patience.
* **Has somewhere to go.** A hook that opens a question the episode never
  answers is a lie told to a viewer, and it costs more than it buys.

## The duplication problem

A cold open lifted from minute nine is *the same footage* as minute nine. Left
in both places it plays twice, which reads as a teaser when a channel does it
deliberately and as a bug when it does not. So the default removes the
original, and the alternatives -- shorten it, or keep it -- are explicit
choices recorded on the plan.

The exception is a hook that overlaps the climax or a payoff: removing that
would take the peak out of the episode to put it at the front. There, the
original is *shortened* rather than removed, whatever the policy says, and the
plan records that the policy was overridden.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.retention.resolve import Resolver, total_seconds
from editing.retention.schema import (
    BORING_OPENERS, OPENABLE_HOOKS, ColdOpenPlan, RetentionCutConfig,
    RetentionCutDecision, decision_id_for,
)

logger = logging.getLogger("nova.editing.retention.coldopen")

#: A hook this far into the episode is normally a spoiler. Configurable.
DEFAULT_SPOILER_POSITION = 0.9

#: Importance labels that make a range worth opening on regardless of the
#: actions the vision model listed. A creeper explosion is a cold open even if
#: the model also said "walking".
STRONG_IMPORTANCE = frozenset({"payoff", "reveal", "danger", "funny"})


def choose(
    hooks: Sequence,
    resolver: Resolver,
    config: RetentionCutConfig,
    *,
    climax=None,
    protected_ranges: Optional[Sequence] = None,
) -> tuple:
    """Pick an opening. Returns ``(ColdOpenPlan, decision)``.

    ``decision`` is ``None`` when no hook survived, and the plan then carries
    ``fallback_reason`` -- which is the field somebody reads when they want to
    know why their episode still opens on walking.
    """
    plan = ColdOpenPlan(duplicate_policy=config.duplicate_policy)

    if not config.cold_open:
        plan.fallback_reason = (
            "Cold opens are switched off for this cut (--no-cold-open)."
        )
        return plan, None

    ranked = sorted(
        hooks, key=lambda hook: (getattr(hook, "score", 0.0),
                                 getattr(hook, "confidence", 0.0)),
        reverse=True,
    )
    if not ranked:
        plan.fallback_reason = (
            "The retention planner found no hook candidates, so the episode "
            "opens where it always did. Hooks come from danger, reactions, "
            "reveals and questions in what was said -- footage with none of "
            "those has nothing to open on."
        )
        return plan, None

    for hook in ranked:
        verdict = _judge(hook, resolver, config)
        if verdict is not None:
            plan.rejected.append({
                "hook_id": getattr(hook, "item_id", ""),
                "hook_type": getattr(hook, "hook_type", "unknown"),
                "start": round(getattr(hook, "start", 0.0), 2),
                "end": round(getattr(hook, "end", 0.0), 2),
                "score": round(getattr(hook, "score", 0.0), 2),
                "code": verdict[0],
                "why": verdict[1],
            })
            continue
        return _accept(hook, resolver, config, plan, climax,
                       protected_ranges or ())

    plan.fallback_reason = (
        f"All {len(ranked)} hook candidate(s) were refused. The reasons are "
        "listed above; the usual one is that the strongest moment opens on "
        "something a viewer has no reason to care about yet."
    )
    return plan, None


def _judge(hook, resolver: Resolver, config: RetentionCutConfig
           ) -> Optional[tuple]:
    """``None`` if this hook can open the episode, or ``(code, why)``."""
    start = float(getattr(hook, "start", 0.0))
    end = float(getattr(hook, "end", 0.0))
    score = float(getattr(hook, "score", 0.0))
    hook_type = str(getattr(hook, "hook_type", "unknown"))

    if score < config.min_hook_score:
        return ("low_confidence",
                f"scored {score:.2f}, under the {config.min_hook_score:.2f} "
                "a hook needs to be worth opening on")

    if hook_type not in OPENABLE_HOOKS:
        return ("hook_is_boring",
                f"'{hook_type}' is not something that makes a viewer stay. "
                "Openings come from danger, mystery, failure, comedy, a "
                "reveal or a challenge")

    duration = max(0.0, end - start)
    if duration < config.min_cold_open_seconds:
        return ("too_short",
                f"{duration:.1f}s is under the "
                f"{config.min_cold_open_seconds:.0f}s a moment needs to land")

    position = resolver.position(start)
    if position >= config.hook_spoiler_position:
        return ("hook_spoils_ending",
                f"sits {position:.0%} of the way in, so opening on it gives "
                "away the ending to save fifteen seconds of patience")

    # What is actually on screen. A "danger" label over eight seconds of
    # walking is the failure this check exists for.
    importances = resolver.importances(start, end)
    actions = resolver.actions(start, end)
    interesting = importances & STRONG_IMPORTANCE
    if actions and not interesting:
        dull = [action for action in actions if action in BORING_OPENERS]
        if dull and len(dull) == len(actions):
            return ("hook_is_boring",
                    f"the footage is {', '.join(dull[:3])} and nothing else. "
                    "Never open on walking, sorting or a menu")

    # Does the moment carry itself? A viewer arriving cold has the picture and
    # whatever is said over it, and nothing else. One of the two has to be
    # doing something, or the opening is a stranger doing something
    # unexplained.
    if not interesting and not resolver.has_speech(start, end):
        return ("hook_needs_context",
                "nothing is said over this and the footage is not labelled as "
                "danger, a reveal or a payoff -- a viewer arriving cold would "
                "have no idea what they are looking at")

    # Already the opening. Moving the first few seconds to the front is not a
    # cold open, it is a no-op with extra steps.
    if position <= 0.02 and start <= config.max_cold_open_seconds:
        return ("hook_is_boring",
                "this is already where the episode starts, so making it the "
                "cold open would change nothing")

    if not resolver.spans(start, end):
        return ("unresolvable",
                "this moment is not in the cut being edited -- the episode "
                "memory was built from different footage")

    return None


def _accept(hook, resolver: Resolver, config: RetentionCutConfig,
            plan: ColdOpenPlan, climax, protected: Sequence = ()) -> tuple:
    """Fill in the plan for a hook that passed every rule."""
    start = float(getattr(hook, "start", 0.0))
    end = float(getattr(hook, "end", 0.0))

    # Trim to the ceiling from the *end*: a hook's opening beat is what makes
    # it read, and the moment after it has usually already resolved.
    duration = min(max(0.0, end - start), config.max_cold_open_seconds)
    trimmed_end = start + duration
    spans = resolver.spans(start, trimmed_end)
    if not spans:
        spans = resolver.resolve_item(hook)

    payoff_at = getattr(hook, "payoff_at", None)
    plan.chosen = True
    plan.hook_id = str(getattr(hook, "item_id", ""))
    plan.hook_type = str(getattr(hook, "hook_type", "unknown"))
    plan.score = float(getattr(hook, "score", 0.0))
    plan.confidence = float(getattr(hook, "confidence", 0.0))
    plan.original_start = start
    plan.original_end = end
    plan.spans = spans
    plan.duration = round(total_seconds(spans), 3)
    plan.viewer_question = str(getattr(hook, "viewer_question", ""))[:400]
    plan.suggested_text = str(getattr(hook, "suggested_text", ""))[:400]
    plan.text_source = str(getattr(hook, "text_source", "none"))
    plan.payoff_at = float(payoff_at) if payoff_at is not None else None
    plan.payoff_id = str(getattr(hook, "payoff_id", ""))
    plan.risks = list(getattr(hook, "risks", []) or [])[:20]

    if trimmed_end < end - 0.05:
        plan.warnings.append(
            f"The hook ran {end - start:.0f}s and was trimmed to "
            f"{duration:.0f}s to fit the cold-open ceiling."
        )
    if plan.payoff_at is None:
        plan.warnings.append(
            "This hook opens a question the episode never answers. That is a "
            "promise to a viewer the video does not keep -- consider a "
            "different opening, or answering it."
        )

    _decide_duplicate(plan, resolver, config, climax, protected)

    decision = RetentionCutDecision(
        decision_id=decision_id_for("cold_open", plan.hook_id, start),
        action="cold_open",
        source_type="hook",
        source_id=plan.hook_id,
        episode_start=start,
        episode_end=trimmed_end,
        spans=spans,
        confidence=plan.confidence,
        priority=1.0,
        reason=(
            f"Opens on the {plan.hook_type} at {start:.0f}s"
            + (f": {plan.viewer_question}" if plan.viewer_question else "")
        ),
        evidence=[plan.hook_id] + [
            span.segment_ids[0] for span in spans if span.segment_ids][:4],
        viewer_effect="opens_a_question",
    )
    return plan, decision


def _decide_duplicate(plan: ColdOpenPlan, resolver: Resolver,
                      config: RetentionCutConfig, climax,
                      protected: Sequence = ()) -> None:
    """What happens to the footage the opening was lifted from.

    The override is the interesting case, and there are two of them. Removing
    a hook that *is* the climax would take the peak out of the episode to put
    it at the front, trading a good opening for no ending. And a hook sitting
    on protected footage cannot be removed at all -- protection is applied
    first and means it.

    Both resolve the same way: the original is *shortened* rather than
    removed. The moment still plays in place, starting after the part that was
    teased, and the plan records that the policy was overridden rather than
    silently obeying something it could not do.
    """
    from editing.retention.resolve import any_overlap

    overlaps_peak = False
    if climax is not None:
        peak_start = float(getattr(climax, "start", 0.0))
        peak_end = float(getattr(climax, "end", 0.0))
        overlaps_peak = max(
            0.0,
            min(plan.original_end, peak_end) - max(plan.original_start,
                                                   peak_start),
        ) > 0.5

    overlaps_protected = any(
        any_overlap(protected, span.asset_id, span.start, span.end)
        for span in plan.spans
    )

    if overlaps_peak and plan.duplicate_policy == "remove":
        plan.duplicate_policy = "shorten"
        plan.warnings.append(
            "This hook is the peak of the episode, so the original was "
            "shortened rather than removed -- taking it out entirely would "
            "have moved the ending to the front and left nothing to build to."
        )
    elif overlaps_protected and plan.duplicate_policy == "remove":
        plan.duplicate_policy = "shorten"
        plan.warnings.append(
            "This hook sits on protected footage (a setup or payoff), so the "
            "original was shortened rather than removed. The moment still "
            "plays in place, starting after the part used as the opening."
        )

    if config.allow_duplicate_footage and plan.duplicate_policy == "remove":
        plan.duplicate_policy = "keep"
        plan.warnings.append(
            "Duplicate footage is allowed, so the opening also plays again in "
            "its original place."
        )

    if plan.duplicate_policy == "remove":
        plan.original_removed = True
    elif plan.duplicate_policy == "shorten":
        plan.original_shortened_to = round(
            max(1.0, (plan.original_end - plan.original_start) * 0.4), 2)
    else:
        plan.original_removed = False


def teaser_decision(plan: ColdOpenPlan) -> Optional[RetentionCutDecision]:
    """The decision that removes or shortens the original occurrence.

    A separate decision from the cold open itself, on purpose: they are two
    changes to the cut with two different justifications, and a person
    disagreeing with the second should not have to lose the first.
    """
    if not plan.chosen or plan.duplicate_policy == "keep":
        return None

    action = "cut" if plan.duplicate_policy == "remove" else "shorten"
    return RetentionCutDecision(
        decision_id=decision_id_for(
            f"teaser_{action}", plan.hook_id, plan.original_start),
        action=action,
        source_type="hook",
        source_id=plan.hook_id,
        episode_start=plan.original_start,
        episode_end=plan.original_end,
        spans=list(plan.spans),
        confidence=plan.confidence,
        priority=0.9,
        reason=(
            "Removes the footage the cold open was lifted from, so it does "
            "not play twice."
            if action == "cut" else
            f"Shortens the original to about "
            f"{plan.original_shortened_to:.0f}s, so the moment still lands in "
            "place without repeating the opening in full."
        ),
        evidence=[plan.hook_id],
        viewer_effect="keeps_momentum",
    )
