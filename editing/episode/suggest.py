"""Turning findings into things a later pass could do.

This is the layer's actual output. A ``RetentionSuggestion`` carries no
Premiere operation and never will -- it names a range, a type, a reason, and
which pass would have to build the operation. Sessions 3, 5 and 6 already know
how to turn intent into ops, and duplicating that here would mean two places
that can put a caption on a timeline.

Two properties are worth stating because they are enforced rather than
intended:

**Nothing here claims analytics.** Every reason is a statement about the edit
-- "seventy seconds of the same action", "asked at 20s and never answered" --
and never about what viewers will do. ``schema.contains_claim`` exists so a
test can assert that across every generated string rather than trusting review.

**Every suggestion has a marker fallback, including the safe ones.** Refusing
to act is only useful if it still leaves something on the timeline for the
person who has to decide. A suggestion that cannot be applied automatically is
not a suggestion that vanishes.
"""
from __future__ import annotations

from typing import Optional

from editing.episode import risks as risks_module
from editing.episode.schema import (
    DOWNSTREAM_FOR, EpisodeEvidence, EpisodeMemory, MARKER_SUGGESTIONS,
    MIN_EDIT_CONFIDENCE, RetentionSuggestion, capped, new_id,
)
from editing.episode.track import EpisodeTrack

#: What each suggestion type is trying to do to the viewer.
EFFECT_FOR = {
    "keep_setup": "clarity",
    "shorten_boring": "pacing",
    "speed_up_grind": "pacing",
    "add_callback_caption": "payoff",
    "add_teaser_marker": "anticipation",
    "add_card": "clarity",
    "add_music_rise_marker": "tension",
    "hold_silence_for_comedy": "comedy",
    "clarify_objective": "clarity",
    "add_goal_marker": "curiosity",
    "mark_climax": "impact",
    "mark_ending_payoff": "payoff",
    "needs_human_review": "unknown",
}

#: A callback this far after what it refers to is worth captioning: the viewer
#: has had time to forget, which is what makes the reference land.
CALLBACK_CAPTION_GAP = 90.0

#: Two suggestions of the same type this close together are one suggestion.
DEDUPE_WINDOW = 5.0


def _auto_safe(
    suggestion_type: str, confidence: float, *, risk: Optional[str] = None
) -> bool:
    """Whether a pass may act on this without a human.

    Delegates to the risk rule when the suggestion came from a risk, so there
    is exactly one place that decides a timing change is allowed. Suggestions
    with no risk behind them can only ever be markers.
    """
    if risk is not None:
        return risks_module.is_auto_safe(risk, suggestion_type, confidence)
    return (
        suggestion_type in MARKER_SUGGESTIONS
        and confidence >= MIN_EDIT_CONFIDENCE
    )


def _make(
    suggestion_type: str,
    start: float,
    end: float,
    *,
    reason: str,
    confidence: float,
    priority: float,
    evidence: EpisodeEvidence,
    marker: str,
    risk: Optional[str] = None,
    beat_ids: Optional[list] = None,
    open_loop_ids: Optional[list] = None,
    risk_ids: Optional[list] = None,
    setup_ids: Optional[list] = None,
    payoff_ids: Optional[list] = None,
    why: str = "",
) -> RetentionSuggestion:
    suggestion = RetentionSuggestion(
        item_id=new_id("sugg", suggestion_type, round(start, 2), round(end, 2)),
        start=start,
        end=end,
        type=suggestion_type,
        reason=reason,
        viewer_effect=EFFECT_FOR.get(suggestion_type, "unknown"),
        priority=max(0.0, min(1.0, priority)),
        marker_fallback=marker,
        downstream=DOWNSTREAM_FOR.get(suggestion_type, "human"),
        beat_ids=list(beat_ids or []),
        open_loop_ids=list(open_loop_ids or []),
        risk_ids=list(risk_ids or []),
        setup_ids=list(setup_ids or []),
        payoff_ids=list(payoff_ids or []),
        evidence=evidence,
        confidence=capped(confidence, evidence.channels),
        why=why or reason,
    )
    suggestion.auto_safe = _auto_safe(
        suggestion_type, suggestion.confidence, risk=risk)
    suggestion.affects_edit = suggestion.auto_safe
    suggestion.settle()
    # ``settle`` is the authority on whether anything may act on this, so the
    # two flags are reconciled afterwards rather than being allowed to disagree.
    suggestion.auto_safe = suggestion.affects_edit
    suggestion.needs_human_review = not suggestion.auto_safe
    return suggestion


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def from_risks(risk_zones: list) -> list[RetentionSuggestion]:
    """One suggestion per risk, carrying the risk's own fix and safety call."""
    out: list[RetentionSuggestion] = []
    for zone in risk_zones:
        if zone.confidence <= 0.0 and zone.duration <= 0.0:
            continue  # a failed detector's placeholder; nothing to act on
        urgency = 1.0 if zone.risk in (
            "weak_hook", "no_clear_objective") else 0.85
        out.append(_make(
            zone.suggested_fix, zone.start, zone.end,
            reason=zone.why,
            confidence=zone.confidence,
            priority=min(1.0, zone.score * urgency),
            evidence=zone.evidence,
            marker=zone.marker_fallback,
            risk=zone.risk,
            risk_ids=[zone.item_id],
            beat_ids=list(zone.beat_ids),
            why=f"answers the {zone.risk.replace('_', ' ')} at "
                f"{zone.start:.0f}s",
        ))
    return out


def from_callbacks(memory: EpisodeMemory) -> list[RetentionSuggestion]:
    """A caption where the episode refers back to something far enough behind."""
    out: list[RetentionSuggestion] = []
    for callback in memory.callbacks:
        if callback.gap_seconds < CALLBACK_CAPTION_GAP:
            continue
        out.append(_make(
            "add_callback_caption", callback.start, callback.end,
            reason=(
                f"refers back to {callback.refers_to_time:.0f}s, "
                f"{callback.gap_seconds / 60.0:.1f} minutes earlier"
            ),
            confidence=callback.confidence,
            priority=min(0.9, 0.35 + callback.confidence * 0.5),
            evidence=callback.evidence,
            marker=callback.suggested_text or f"callback: {callback.label}",
            why=callback.why,
        ))
    return out


def from_loops(memory: EpisodeMemory) -> list[RetentionSuggestion]:
    """Protect the setups that pay off; tease the ones still hanging."""
    out: list[RetentionSuggestion] = []
    for loop in memory.open_loops:
        if loop.status in ("resolved", "possibly_resolved"):
            out.append(_make(
                "keep_setup", loop.start, loop.end,
                reason=(
                    f"this question is answered at {loop.resolved_at:.0f}s, "
                    "so cutting it would leave the answer with nothing to "
                    "answer"
                ),
                confidence=loop.confidence,
                priority=min(0.9, 0.45 + loop.confidence * 0.4),
                evidence=loop.evidence,
                marker="setup: " + loop.question[:48],
                open_loop_ids=[loop.item_id],
                why=loop.why_viewer_cares,
            ))
        elif loop.candidate_payoffs:
            out.append(_make(
                "add_teaser_marker", loop.start, loop.end,
                reason=(
                    "this question has possible answers later that nothing "
                    "confirms; a marker at each is cheaper than watching the "
                    "whole middle again"
                ),
                confidence=loop.confidence,
                priority=0.40,
                evidence=loop.evidence,
                marker="open: " + loop.question[:48],
                open_loop_ids=[loop.item_id],
                why=loop.why_viewer_cares,
            ))
    return out


def from_payoffs(memory: EpisodeMemory) -> list[RetentionSuggestion]:
    """A caption on a payoff whose setup is far enough back to be forgotten."""
    out: list[RetentionSuggestion] = []
    for payoff in memory.payoffs:
        if payoff.gap_seconds < CALLBACK_CAPTION_GAP:
            continue
        out.append(_make(
            "add_callback_caption", payoff.start, payoff.end,
            reason=(
                f"lands a setup from {payoff.gap_seconds / 60.0:.1f} minutes "
                f"earlier; {payoff.match_reason}"
            ),
            confidence=payoff.confidence,
            priority=min(0.9, 0.40 + payoff.confidence * 0.5),
            evidence=payoff.evidence,
            marker="payoff: " + payoff.text[:48],
            setup_ids=[payoff.setup_id] if payoff.setup_id else [],
            payoff_ids=[payoff.item_id],
            why=payoff.why,
        ))
    return out


def from_objective(memory: EpisodeMemory) -> list[RetentionSuggestion]:
    """Keep the moment the goal is stated, and card it if it is buried."""
    objective = memory.main_objective
    if objective is None or objective.status == "implied":
        return []
    return [_make(
        "keep_setup", objective.start, objective.end,
        reason=(
            "this is where the episode says what it is for; everything after "
            "it depends on the viewer having heard it"
        ),
        confidence=objective.confidence,
        priority=min(1.0, 0.55 + objective.confidence * 0.4),
        evidence=objective.evidence,
        marker="objective: " + objective.text[:48],
        why=objective.why,
    )]


def from_climax(memory: EpisodeMemory, climax) -> list[RetentionSuggestion]:
    """Mark the peak, and mark the rise into it."""
    if climax is None:
        return []
    out = [_make(
        "mark_climax", climax.start, climax.end,
        reason=(
            f"the strongest moment in the episode, {climax.margin:.2f} ahead "
            "of the next candidate"
        ),
        confidence=climax.confidence,
        priority=min(1.0, 0.6 + climax.score * 0.4),
        evidence=climax.evidence,
        marker="climax",
        beat_ids=list(climax.beat_ids),
        open_loop_ids=list(climax.resolves_loop_ids),
        why=climax.why,
    )]

    rise = [
        beat for beat in memory.beats
        if beat.kind in ("escalation", "danger")
        and beat.end <= climax.start
        and climax.start - beat.end <= 90.0
    ]
    if rise:
        first = rise[0]
        out.append(_make(
            "add_music_rise_marker", first.start, climax.start,
            reason=(
                f"the {first.kind} at {first.start:.0f}s runs into the peak "
                f"at {climax.start:.0f}s"
            ),
            confidence=min(first.confidence, climax.confidence),
            priority=0.5,
            evidence=first.evidence.merged(climax.evidence),
            marker="music rise into the peak",
            beat_ids=[first.item_id],
            why="a rise has somewhere to arrive",
        ))
    return out


def from_ending(memory: EpisodeMemory, ending) -> list[RetentionSuggestion]:
    if ending is None:
        return []
    return [_make(
        "mark_ending_payoff", ending.start, ending.end,
        reason=(
            "closes the episode's stated objective"
            if ending.closes_main_objective else
            f"the strongest {ending.kind} near the end"
        ),
        confidence=ending.confidence,
        priority=min(1.0, 0.5 + ending.score * 0.5),
        evidence=ending.evidence,
        marker="ending: " + (ending.suggested_text or ending.kind)[:48],
        open_loop_ids=list(ending.resolves_loop_ids),
        why=ending.why,
    )]


def from_jokes(memory: EpisodeMemory) -> list[RetentionSuggestion]:
    """Hold the silence after a laugh, where one was actually measured."""
    out: list[RetentionSuggestion] = []
    for beat in memory.beats:
        if beat.kind != "joke":
            continue
        if "audio" not in beat.evidence.channels:
            continue
        out.append(_make(
            "hold_silence_for_comedy", beat.end, beat.end + 1.0,
            reason=(
                "a reaction was measured here; a beat of silence after it "
                "gives the joke somewhere to land"
            ),
            confidence=beat.confidence,
            priority=0.35,
            evidence=beat.evidence,
            marker="hold for the laugh",
            beat_ids=[beat.item_id],
            why=beat.why,
        ))
    return out


def midpoint_reset(
    memory: EpisodeMemory, track: EpisodeTrack, spot
) -> Optional[RetentionSuggestion]:
    """A goal restatement at the midpoint, when there is a goal to restate."""
    if spot is None:
        return None
    objective = memory.main_objective
    if objective is None:
        return None
    start, end, evidence, why = spot
    return _make(
        "add_goal_marker", start, end,
        reason=(
            f"restate the goal '{objective.text[:48]}' here; it was last said "
            f"at {objective.start:.0f}s"
        ),
        confidence=objective.confidence,
        priority=0.45,
        evidence=evidence.merged(objective.evidence),
        marker="midpoint: restate the goal",
        why=why,
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def dedupe(suggestions: list) -> list:
    """One suggestion per type per moment; the higher priority wins.

    A grind that is also dead air produces two shortenings of the same stretch,
    and a downstream pass acting on both would shorten it twice.
    """
    kept: list[RetentionSuggestion] = []
    for suggestion in sorted(suggestions, key=lambda item: -item.priority):
        clash = next(
            (
                item for item in kept
                if item.type == suggestion.type
                and abs(item.start - suggestion.start) <= DEDUPE_WINDOW
            ),
            None,
        )
        if clash is None:
            kept.append(suggestion)
            continue
        clash.evidence = clash.evidence.merged(suggestion.evidence)
        for field_name in ("beat_ids", "open_loop_ids", "risk_ids",
                           "setup_ids", "payoff_ids"):
            merged = list(getattr(clash, field_name))
            for value in getattr(suggestion, field_name):
                if value not in merged:
                    merged.append(value)
            setattr(clash, field_name, merged)
        clash.end = max(clash.end, suggestion.end)
    kept.sort(key=lambda item: (item.start, -item.priority))
    return kept


def build(
    memory: EpisodeMemory,
    track: EpisodeTrack,
    risk_zones: list,
    *,
    climax=None,
    ending=None,
) -> list[RetentionSuggestion]:
    """Every suggestion this episode produces, in episode order."""
    everything: list[RetentionSuggestion] = []
    everything.extend(from_risks(risk_zones))
    everything.extend(from_callbacks(memory))
    everything.extend(from_loops(memory))
    everything.extend(from_payoffs(memory))
    everything.extend(from_objective(memory))
    everything.extend(from_climax(memory, climax))
    everything.extend(from_ending(memory, ending))
    everything.extend(from_jokes(memory))
    return dedupe(everything)
