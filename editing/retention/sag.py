"""Compressing the stretches the retention planner called weak.

Session 8 marks thirteen kinds of risk zone. Seven of them describe footage
that is *too long for what it contains*, and this module is what does something
about them. The other six describe problems compression cannot fix -- a
confusing transition does not get better by being shorter -- and become
markers.

## Cut, or speed up?

The question this module exists to answer, and the rule is about what the
footage is *for*:

* **Somebody is talking** -> neither. Sped-up dialogue is unusable and cutting
  mid-sentence is worse. It becomes a marker.
* **The action visually matters** -- a build going up, a tunnel getting
  longer, a base changing -> **speed up**. The viewer needs to see that it
  happened; they do not need to watch it happen.
* **Nothing changes** -- standing still, the same swing forty times, a menu
  -> **cut**. There is nothing to see, so there is nothing to preserve.

## Keeping enough

A compressed stretch keeps ``keep_context_seconds`` at each end. Cutting a
grind to nothing makes the episode jump from "heading down" to "at the bottom"
with no travel, which reads as a missing scene rather than a tight edit.

And there is a ceiling: ``max_compression_share`` caps how much of the base cut
this pass may remove in total. A retention pass that removes 80% of an episode
has not compressed a sag; it has deleted the video, and the ceiling is what
stops a run of high-severity risks doing that one zone at a time.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.retention.resolve import Resolver, any_overlap, total_seconds
from editing.retention.schema import (
    COMPRESSIBLE_RISKS, RetentionCutConfig, RetentionCutDecision,
    SagCompressionPlan, SourceSpan, decision_id_for,
)

logger = logging.getLogger("nova.editing.retention.sag")

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

#: Actions that mean the picture is changing in a way the viewer needs to see
#: happen, even quickly. These get sped up rather than cut.
PROGRESS_ACTIONS = frozenset({
    "building", "mining", "digging", "crafting", "farming", "exploring",
    "travelling", "walking", "running", "climbing", "swimming",
})

#: Risk types that are about a *story* problem rather than a length problem.
#: Shortening does not fix any of them, so they become markers.
NOT_A_LENGTH_PROBLEM = frozenset({
    "no_clear_objective", "confusing_transition", "unresolved_setup",
    "weak_hook", "anticlimax", "unclear_ending",
})


def compress(
    risks: Sequence,
    resolver: Resolver,
    config: RetentionCutConfig,
    protected: Sequence[SourceSpan],
    *,
    base_seconds: float = 0.0,
) -> tuple:
    """Decide what to do about every risk zone.

    Returns ``(SagCompressionPlan, decisions)``. Every zone produces exactly
    one decision, including the ones that produce a marker, so a report can
    account for all of them.
    """
    plan = SagCompressionPlan()
    decisions: list[RetentionCutDecision] = []

    if not config.compress_sag:
        plan.warnings.append(
            "Sag compression is switched off for this cut, so every risk zone "
            "below is a marker only."
        )

    floor = _SEVERITY_ORDER.get(config.min_risk_severity, 1)
    budget = (base_seconds * config.max_compression_share
              if base_seconds > 0 else 0.0)
    spent = 0.0

    # Worst first, so the budget is spent on the zones that most need it.
    ordered = sorted(
        risks,
        key=lambda risk: (_SEVERITY_ORDER.get(
            getattr(risk, "severity", "low"), 0),
            float(getattr(risk, "score", 0.0))),
        reverse=True,
    )

    for risk in ordered:
        decision, removed, sped = _one(
            risk, resolver, config, protected, floor, budget, spent)
        decisions.append(decision)
        plan.zones.append({
            "risk_id": str(getattr(risk, "item_id", "")),
            "risk": str(getattr(risk, "risk", "")),
            "severity": str(getattr(risk, "severity", "low")),
            "start": round(float(getattr(risk, "start", 0.0)), 2),
            "end": round(float(getattr(risk, "end", 0.0)), 2),
            "action": decision.action,
            "accepted": decision.accepted,
            "why": decision.reason or decision.rejected_reason,
        })

        if decision.accepted and decision.changes_footage:
            plan.zones_compressed += 1
            plan.seconds_removed += removed
            plan.seconds_sped_up += sped
            spent += removed
        elif decision.action == "marker_only":
            plan.zones_marked_only += 1
        else:
            plan.zones_refused += 1

    plan.decisions = decisions
    if budget > 0 and spent >= budget - 0.01:
        plan.warnings.append(
            f"Compression stopped at the {config.max_compression_share:.0%} "
            f"ceiling ({budget:.0f}s of the base cut). Later risk zones were "
            "marked rather than compressed."
        )
    return plan, decisions


def _one(risk, resolver: Resolver, config: RetentionCutConfig,
         protected: Sequence[SourceSpan], floor: int,
         budget: float, spent: float) -> tuple:
    """One risk zone. Returns ``(decision, seconds removed, seconds sped)``."""
    item_id = str(getattr(risk, "item_id", ""))
    kind = str(getattr(risk, "risk", ""))
    severity = str(getattr(risk, "severity", "low"))
    start = float(getattr(risk, "start", 0.0))
    end = float(getattr(risk, "end", 0.0))
    confidence = float(getattr(risk, "confidence", 0.0))

    decision = RetentionCutDecision(
        decision_id=decision_id_for("sag", item_id, start),
        action="marker_only",
        source_type="risk",
        source_id=item_id,
        episode_start=start,
        episode_end=end,
        confidence=confidence,
        priority=float(getattr(risk, "score", 0.0)),
        evidence=[item_id],
        viewer_effect="removes_a_dull_stretch",
    )

    def refuse(code: str, why: str) -> tuple:
        decision.action = "marker_only" if code in (
            "disabled", "low_confidence", "speech_present") else "reject"
        decision.reject_code = code
        decision.rejected_reason = why
        decision.reason = (
            f"{kind} at {start:.0f}s: {why}"
        )
        return decision, 0.0, 0.0

    if not config.compress_sag:
        return refuse("disabled", "sag compression is switched off")

    if kind in NOT_A_LENGTH_PROBLEM:
        return refuse(
            "unknown",
            f"'{kind}' is a story problem, not a length one -- shortening it "
            "would not fix anything")

    if kind not in COMPRESSIBLE_RISKS:
        return refuse("unknown",
                      f"nothing here knows how to compress '{kind}'")

    if _SEVERITY_ORDER.get(severity, 0) < floor:
        return refuse(
            "low_confidence",
            f"severity is '{severity}', under the "
            f"'{config.min_risk_severity}' needed to change footage")

    if confidence < config.min_confidence:
        return refuse(
            "low_confidence",
            f"confidence {confidence:.2f} is under the "
            f"{config.min_confidence:.2f} needed to change footage")

    spans = resolver.resolve_item(risk)
    if not spans:
        return refuse("unresolvable",
                      "this stretch is not in the cut being edited")

    # Protection wins, always. It claimed this footage first.
    for span in spans:
        if any_overlap(protected, span.asset_id, span.start, span.end):
            return refuse(
                "protected_range",
                "this overlaps a setup or payoff that is protected, and "
                "protection is applied before compression")

    if resolver.has_speech(start, end):
        return refuse(
            "speech_present",
            "somebody is talking over this. Sped-up dialogue is unusable and "
            "cutting mid-sentence is worse")

    if budget > 0 and spent >= budget:
        return refuse(
            "over_compression",
            f"the {config.max_compression_share:.0%} compression ceiling is "
            "already spent")

    trimmed = _keep_context(spans, config.keep_context_seconds)
    if not trimmed:
        return refuse(
            "too_short",
            f"after keeping {config.keep_context_seconds:.0f}s of context at "
            "each end there is nothing left to compress")

    # Cut or speed up? What the footage is *for* decides.
    actions = set(resolver.actions(start, end))
    visual_progress = bool(actions & PROGRESS_ACTIONS)
    seconds = total_seconds(trimmed)

    if budget > 0 and spent + seconds > budget:
        # Do what fits rather than nothing: a partly-compressed sag is still
        # shorter than an uncompressed one.
        decision.safety_notes.append(
            "trimmed to fit inside the compression ceiling")
        trimmed = _fit(trimmed, max(0.0, budget - spent))
        seconds = total_seconds(trimmed)
        if seconds <= 0.1:
            return refuse("over_compression",
                          "the compression ceiling left no room for this zone")

    decision.spans = trimmed
    decision.accepted = True

    if visual_progress:
        decision.action = "speed_up"
        decision.speed = config.grind_speed
        removed = seconds - (seconds / config.grind_speed)
        decision.reason = (
            f"{kind} at {start:.0f}s: the picture is changing "
            f"({', '.join(sorted(actions & PROGRESS_ACTIONS))[:60]}), so this "
            f"runs at {config.grind_speed:g}x rather than being cut -- a "
            "viewer needs to see it happened, not watch it happen."
        )
        return decision, removed, seconds

    decision.action = "cut"
    decision.reason = (
        f"{kind} at {start:.0f}s: nothing is happening and nobody is talking, "
        f"so {seconds:.0f}s comes out. "
        f"{config.keep_context_seconds:.0f}s is kept at each end so the cut "
        "does not read as a missing scene."
    )
    return decision, seconds, 0.0


def _keep_context(spans: Sequence[SourceSpan], keep: float
                  ) -> list[SourceSpan]:
    """Shrink each span so some context survives at both ends.

    Applied per span rather than across the whole zone: a risk covering three
    separate clips should keep the head and tail of each, because each of them
    is a place the cut lands.
    """
    out: list[SourceSpan] = []
    for span in spans:
        start = span.start + keep
        end = span.end - keep
        if end - start <= 0.2:
            continue
        out.append(SourceSpan(
            asset_id=span.asset_id,
            source_file=span.source_file,
            start=round(start, 3),
            end=round(end, 3),
            segment_ids=list(span.segment_ids),
            placement_ids=list(span.placement_ids),
        ))
    return out


def _fit(spans: Sequence[SourceSpan], budget: float) -> list[SourceSpan]:
    """Take spans until the budget runs out, trimming the last one."""
    out: list[SourceSpan] = []
    remaining = budget
    for span in spans:
        if remaining <= 0.1:
            break
        if span.duration <= remaining:
            out.append(span)
            remaining -= span.duration
            continue
        out.append(SourceSpan(
            asset_id=span.asset_id,
            source_file=span.source_file,
            start=span.start,
            end=round(span.start + remaining, 3),
            segment_ids=list(span.segment_ids),
            placement_ids=list(span.placement_ids),
        ))
        remaining = 0.0
    return out
