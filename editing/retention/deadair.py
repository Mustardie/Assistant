"""Killing silence that is doing nothing, and keeping silence that is.

The general selector already drops dead air: Session 3 refuses any segment
where measured silence covers most of it. That is the conservative rule, and it
leaves a lot behind -- 1.5 seconds of nothing between two sentences is not
"most of a segment", and forty of those across an episode is a minute of a
viewer waiting.

This pass is harder. It goes after silence the selector kept, and the whole
difficulty is that **some of that silence is the edit**.

## What silence can be for

The beat after a death is the joke. The pause before a reveal is the tension.
The gap while somebody realises what they have just done is the reaction. Cut
those and the episode becomes a list of things that happened.

So every stretch of silence is asked what it is *for*, from evidence the
earlier passes recorded:

* it follows a reaction, a scream or laughter -> ``aftermath``
* it sits just before a payoff or reveal -> ``tension``
* the surrounding footage is a death or failure -> ``comedy_pause``
* it bridges two different places -> ``transition``
* it sits inside a protected setup/payoff window -> ``setup_payoff_timing``

Silence with a purpose is held to ``max_purposeful_silence`` -- long enough to
land, capped so a four-second pause does not become its own problem. Silence
with no purpose is held to ``ordinary_silence_limit``, which is 0.6s on the
aggressive setting.

## What this never does

Trim into speech. The limit is applied to the *silent* part only, and the
trimmed range stops where the audio layer said the silence stops. Cutting
0.2 seconds off the front of a word to hit a target is how an edit starts
sounding clipped, and no retention argument is worth that.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.retention.resolve import Resolver, any_overlap, total_seconds
from editing.retention.schema import (
    DeadAirDecision, RetentionCutConfig, RetentionCutDecision, SourceSpan,
    decision_id_for,
)

logger = logging.getLogger("nova.editing.retention.deadair")

#: Audio events that count as silence worth looking at.
SILENT_TYPES = frozenset({"silence", "long_pause", "low_energy"})

#: Audio that means somebody just reacted to something.
REACTION_TYPES = frozenset({
    "sudden_reaction", "possible_laughter", "possible_scream",
    "loudness_spike",
})

#: Importance labels that make the silence around them purposeful.
PURPOSEFUL_IMPORTANCE = {
    "payoff": "tension",
    "reveal": "tension",
    "danger": "tension",
    "funny": "comedy_pause",
}

#: How far either side of a silence this looks for a reason to keep it.
CONTEXT_WINDOW = 4.0

#: Silence shorter than this is not worth a decision, at any setting. Speech
#: has gaps in it and always will.
FLOOR = 0.35


def sweep(
    resolver: Resolver,
    config: RetentionCutConfig,
    protected: Sequence[SourceSpan],
) -> tuple:
    """Every stretch of silence in the episode, judged.

    Returns ``(records, decisions)``. Silence that is kept produces a record
    and no decision -- there is nothing to do about it, and the record is what
    lets the report say "nine pauses were left alone because they were doing
    something".
    """
    records: list[DeadAirDecision] = []
    decisions: list[RetentionCutDecision] = []

    if not config.kill_dead_air:
        return records, decisions

    for start, end, kinds in _silences(resolver):
        record = _judge(start, end, kinds, resolver, config, protected)
        records.append(record)
        if not record.accepted:
            continue
        decision = RetentionCutDecision(
            decision_id=record.decision_id,
            action=record.action,
            source_type="dead_air",
            source_id=record.decision_id,
            episode_start=record.episode_start,
            episode_end=record.episode_end,
            spans=list(record.spans),
            confidence=record.confidence,
            priority=0.4,
            reason=record.reason,
            evidence=sorted(kinds)[:4],
            viewer_effect="keeps_momentum",
        )
        decisions.append(decision)
    return records, decisions


def _silences(resolver: Resolver) -> list[tuple]:
    """Every silent stretch on the episode clock, merged across slots.

    Read from the audio events the analysis recorded rather than re-detected:
    silence is a measurement Session 2 already made, and measuring it twice
    with two different thresholds is how two passes come to disagree about
    what is quiet.
    """
    found: list[tuple] = []
    for slot in getattr(resolver.track, "slots", ()):
        segment = getattr(slot, "segment", None)
        if segment is None:
            continue
        rate = float(getattr(slot, "speed", 1.0) or 1.0)
        for event in getattr(segment, "audio_events", []) or []:
            if event.type not in SILENT_TYPES:
                continue
            # Audio events are in source time; the episode clock is what every
            # other finding uses, so convert through the slot.
            head = max(0.0, event.start - slot.source_start) / (rate or 1.0)
            tail = max(0.0, event.end - slot.source_start) / (rate or 1.0)
            start = max(slot.start, slot.start + head)
            end = min(slot.end, slot.start + tail)
            if end - start >= FLOOR:
                found.append((round(start, 3), round(end, 3), {event.type}))

    return _merge(found)


def _merge(spans: list[tuple]) -> list[tuple]:
    ordered = sorted(spans)
    out: list[tuple] = []
    for start, end, kinds in ordered:
        if out and start <= out[-1][1] + 0.1:
            previous = out[-1]
            out[-1] = (previous[0], max(previous[1], end),
                       previous[2] | kinds)
            continue
        out.append((start, end, set(kinds)))
    return out


def _judge(start: float, end: float, kinds: set, resolver: Resolver,
           config: RetentionCutConfig,
           protected: Sequence[SourceSpan]) -> DeadAirDecision:
    """One stretch of silence: what is it for, and what happens to it."""
    duration = max(0.0, end - start)
    record = DeadAirDecision(
        decision_id=decision_id_for("dead_air", f"{start:.2f}", start),
        episode_start=start,
        episode_end=end,
        confidence=0.7,
    )

    purpose = _purpose(start, end, resolver, protected)
    record.purpose = purpose or ""
    limit = (config.max_purposeful_silence if purpose
             else config.ordinary_silence_limit)

    if duration <= limit:
        record.accepted = False
        record.action = "keep"
        record.seconds_kept = round(duration, 3)
        record.rejected_reason = (
            f"{duration:.1f}s of {purpose.replace('_', ' ')}, inside the "
            f"{limit:.1f}s this style holds for it"
            if purpose else
            f"{duration:.1f}s, inside the {limit:.1f}s ordinary silence limit"
        )
        return record

    if resolver.has_speech(start, end):
        record.accepted = False
        record.action = "keep"
        record.rejected_reason = (
            "somebody is talking across this; the gap is speech pacing, not "
            "dead air"
        )
        return record

    # Trim to the limit rather than cutting the whole thing: the first second
    # of a pause is usually the reaction to whatever just happened.
    keep = limit
    trim_start = start + keep
    spans = resolver.spans(trim_start, end)
    for span in spans:
        if any_overlap(protected, span.asset_id, span.start, span.end):
            record.accepted = False
            record.action = "keep"
            record.rejected_reason = (
                "this silence sits inside a protected setup or payoff, and "
                "protection is applied first"
            )
            return record

    if not spans:
        record.accepted = False
        record.action = "keep"
        record.rejected_reason = (
            "this stretch is not in the cut being edited"
        )
        return record

    record.spans = spans
    record.accepted = True
    record.action = "cut"
    record.seconds_removed = round(total_seconds(spans), 3)
    record.seconds_kept = round(keep, 3)
    record.reason = (
        f"{duration:.1f}s of {purpose.replace('_', ' ')} trimmed to "
        f"{keep:.1f}s -- long enough to land, short enough not to become its "
        "own problem"
        if purpose else
        f"{duration:.1f}s of silence doing nothing, trimmed to {keep:.1f}s"
    )
    return record


def _purpose(start: float, end: float, resolver: Resolver,
             protected: Sequence[SourceSpan]) -> Optional[str]:
    """What this silence is for, or ``None`` if it is doing nothing.

    Read from what surrounds it, because silence has no properties of its own
    -- the pause after a scream and the pause in an empty tunnel are the same
    measurement, and only the context tells them apart.
    """
    before = resolver.audio_types(max(0.0, start - CONTEXT_WINDOW), start)
    after = resolver.audio_types(end, end + CONTEXT_WINDOW)

    if before & REACTION_TYPES:
        return "aftermath"

    importances = resolver.importances(
        max(0.0, start - CONTEXT_WINDOW), end + CONTEXT_WINDOW)
    for importance, purpose in PURPOSEFUL_IMPORTANCE.items():
        if importance in importances:
            # Silence *before* a payoff is tension; after one it is aftermath.
            if importance in ("payoff", "reveal") and after & REACTION_TYPES:
                return "aftermath"
            return purpose

    if after & REACTION_TYPES:
        return "tension"

    for span in resolver.spans(start, end):
        if any_overlap(protected, span.asset_id, span.start, span.end):
            return "setup_payoff_timing"

    # A cut between two different places needs a beat to land.
    environments = {
        getattr(slot, "environment", "unknown")
        for slot in resolver.slots_between(
            max(0.0, start - CONTEXT_WINDOW), end + CONTEXT_WINDOW)
    }
    environments.discard("unknown")
    if len(environments) > 1:
        return "transition"

    return None
