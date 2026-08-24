"""The compiler: retention findings plus a cut, into a better-shaped cut.

    EpisodeMemory + EpisodeRetentionPlan + (DirectorPlan | RoughCutPlan)
        -> RetentionCutPlan -> SelectedRange[]

## The order is the safety model

    1. resolve      episode time -> real footage, once, through the track
    2. protect      setups, payoffs, callbacks, the peak  -> claimed spans
    3. cold open    choose an opening, decide what happens to the original
    4. compress     risk zones, refusing anything protection claimed
    5. dead air     silence, refusing anything protection claimed
    6. validate     every decision, deterministically
    7. apply        transform the base ranges

Protection runs before anything that removes footage, so a compression pass
literally cannot take a setup out -- the claim already exists when the question
is asked. Reversing those two steps would work most of the time, which is worse
than not working at all.

The cold open sits between them on purpose: it needs to know what is protected
(so it can refuse to gut the peak) and the compressor needs to know what the
cold open took (so it does not compress footage that is now the opening).

## What comes out

A new list of ranges. The base cut is untouched -- ``apply`` builds a fresh
list rather than mutating what it was given, so disagreeing with the retention
pass never costs the cut it was arguing with.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.retention import coldopen, deadair, protect as protect_module
from editing.retention import sag as sag_module
from editing.retention.resolve import (
    Resolver, any_overlap, build_resolver, total_seconds,
)
from editing.retention.schema import (
    RetentionCutConfig, RetentionCutDecision, RetentionCutFailure,
    RetentionCutPlan, SourceSpan, now,
)
from editing.roughcut.select import SelectedRange

logger = logging.getLogger("nova.editing.retention.compile")

#: A range shorter than this after retention edits is dropped: it is a frame
#: and a half, not a shot.
MIN_RANGE = 0.6


def build(
    memory,
    retention,
    base_ranges: Sequence[SelectedRange],
    timeline,
    *,
    config: Optional[RetentionCutConfig] = None,
    roughcut=None,
    director_plan=None,
    base: str = "heuristic",
    name: str = "structure",
) -> tuple:
    """Compile a retention-aware cut. Returns ``(plan, ranges)``.

    ``ranges`` is the new selection. In ``report_only`` mode it is the base
    list unchanged -- every decision is still made and recorded, and none of
    them is applied.
    """
    config = (config or RetentionCutConfig()).validated()

    plan = RetentionCutPlan(
        name=name,
        mode=config.mode,
        config=config,
        base=base,
        timebase=str(getattr(memory, "timebase", "empty")),
        generated_at=now(),
        base_ranges=len(base_ranges),
        base_duration=_runtime(base_ranges),
        warnings=list(config.warnings),
        sources={
            "episode_memory": memory is not None,
            "retention_plan": retention is not None,
            "director_plan": director_plan is not None,
            "roughcut": roughcut is not None,
        },
    )

    if config.mode == "off":
        plan.failure = RetentionCutFailure(
            stage="config",
            code="mode_is_off",
            message="Retention wiring is switched off, so the cut is "
                    "unchanged.",
            hint="Use --mode report_only to see what it would do, or "
                 "--mode retention to let it.",
            recoverable=True,
        )
        return plan, list(base_ranges)

    if retention is None:
        plan.failure = RetentionCutFailure(
            stage="no_retention_plan",
            code="no_retention_plan",
            message="There is no retention plan to wire in.",
            hint="Build one with `python -m editing.cli episode "
                 "plan-retention`.",
            recoverable=True,
        )
        return plan, list(base_ranges)

    if not base_ranges:
        plan.failure = RetentionCutFailure(
            stage="no_base_cut",
            code="no_base_cut",
            message="There is no cut to apply retention wiring to.",
            hint="Build one with `python -m editing.cli roughcut build`.",
            recoverable=True,
        )
        return plan, []

    resolver = build_resolver(
        timeline, roughcut, timebase=plan.timebase)
    if resolver.track.is_empty:
        plan.failure = RetentionCutFailure(
            stage="no_track",
            code="empty_track",
            message="The episode clock is empty, so no finding can be placed "
                    "on real footage.",
            hint="Run `python -m editing.cli run --folder <folder>` to build "
                 "a timeline first.",
            recoverable=True,
        )
        return plan, list(base_ranges)

    if getattr(resolver, "mismatched", False):
        plan.failure = RetentionCutFailure(
            stage="no_track",
            code="timebase_mismatch",
            message="The episode memory was built from a rough cut and there "
                    "is no rough cut to resolve it against.",
            hint="Build one with `python -m editing.cli roughcut build`, or "
                 "rebuild the memory without one (`episode build-memory`). "
                 "Acting on this would place every finding against a "
                 "different clock, so nothing was changed.",
            recoverable=True,
            detail={"timebase": plan.timebase},
        )
        return plan, list(base_ranges)

    base_spans = _spans_of(base_ranges)

    # -- 2. protect ------------------------------------------------------
    setups, payoffs, protect_decisions = protect_module.protect(
        memory, resolver, config, base_spans,
        climax=getattr(retention, "climax", None),
    )
    plan.setups = setups
    plan.payoffs = payoffs
    plan.decisions.extend(protect_decisions)
    protected = protect_module.spans_of(protect_decisions)

    # -- 3. cold open ----------------------------------------------------
    cold, cold_decision = coldopen.choose(
        getattr(retention, "hooks", []) or [],
        resolver, config,
        climax=getattr(retention, "climax", None),
        protected_ranges=protected,
    )
    plan.cold_open = cold
    if cold_decision is not None:
        plan.decisions.append(cold_decision)
        teaser = coldopen.teaser_decision(cold)
        if teaser is not None:
            plan.decisions.append(teaser)

    # Footage that is now the opening must not also be compressed: it is no
    # longer in the middle of the episode, and a rule that thinks it is would
    # be reasoning about a cut that no longer exists.
    claimed = list(protected)
    if cold.chosen:
        claimed.extend(cold.spans)

    # -- 4. compress -----------------------------------------------------
    sag_plan, sag_decisions = sag_module.compress(
        getattr(retention, "risks", []) or [],
        resolver, config, claimed,
        base_seconds=plan.base_duration,
    )
    plan.sag = sag_plan
    plan.decisions.extend(sag_decisions)

    # -- 5. dead air -----------------------------------------------------
    dead_records, dead_decisions = deadair.sweep(resolver, config, claimed)
    plan.dead_air = dead_records
    plan.decisions.extend(dead_decisions)

    # -- 6. validate -----------------------------------------------------
    _validate(plan, config, base_spans)

    # -- 7. apply --------------------------------------------------------
    if config.acts:
        ranges = apply(base_ranges, plan, config)
    else:
        ranges = list(base_ranges)
        plan.warnings.append(
            "report_only: every decision above was made and none was "
            "applied. The cut is exactly what it was."
        )

    plan.cut_ranges = len(ranges)
    plan.cut_duration = _runtime(ranges)
    _warn(plan, config)
    return plan, ranges


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(plan: RetentionCutPlan, config: RetentionCutConfig,
              base_spans: Sequence[SourceSpan]) -> None:
    """The deterministic pass over everything proposed.

    Two halves. First **everything is accepted** except what the module that
    made it already refused -- a decision arrives with ``accepted=False``, the
    schema default, and something has to say yes. Then the checks run and can
    take it back.

    That ordering is deliberate and was a bug before it was a design: a
    decision is a request, so the default is no, and the pass that grants
    requests has to be a pass rather than an assumption. Skipping decisions
    that had not been accepted yet meant the protection and cold-open
    decisions were never accepted at all, and the whole layer quietly did
    nothing.

    Most decisions also arrive already checked by their own module -- the sag
    pass refuses protected footage itself, because it needs the answer to
    decide what to do. What runs here is what crosses module boundaries: two
    decisions on the same footage, a decision on footage the base cut does not
    contain, and confidence.
    """
    protected = plan.protected_spans

    for decision in plan.decisions:
        if not decision.reject_code:
            decision.accepted = True

    for decision in plan.decisions:
        if not decision.accepted:
            continue

        if decision.changes_footage and not decision.is_resolved:
            _reject(decision, "unresolvable",
                    "this decision resolves to no footage in the cut")
            continue

        if (decision.changes_footage
                and decision.confidence < config.min_confidence
                and decision.action != "cold_open"):
            _reject(decision, "low_confidence",
                    f"confidence {decision.confidence:.2f} is under the "
                    f"{config.min_confidence:.2f} needed to change footage")
            continue

        # A removal that reaches into protected footage, whichever module
        # proposed it. The cold open's own teaser cut is exempt: it is a
        # deliberate removal of footage that is now at the front.
        if decision.action in ("cut", "speed_up", "shorten") \
                and decision.source_type != "hook":
            hit = _first_protected(decision.spans, protected)
            if hit is not None:
                _reject(decision, "protected_range",
                        "this overlaps footage a setup or payoff claimed, and "
                        "protection is applied before anything that removes")
                continue

        if decision.changes_footage and not _touches_base(
                decision.spans, base_spans):
            _reject(decision, "unresolvable",
                    "the base cut does not contain this footage, so there is "
                    "nothing here to change")

    # A dead-air record and its decision are two objects describing one
    # judgement, and validation can refuse the decision after the sweep
    # accepted the record. Left alone they disagree, and the report adds up
    # seconds that were never removed.
    by_id = {decision.decision_id: decision for decision in plan.decisions}
    for record in plan.dead_air:
        decision = by_id.get(record.decision_id)
        if decision is None or record.accepted == decision.accepted:
            continue
        record.accepted = decision.accepted
        record.rejected_reason = decision.rejected_reason
        record.action = decision.action if decision.accepted else "keep"
        if not decision.accepted:
            record.seconds_removed = 0.0

    # Two accepted decisions removing the same footage would double-count
    # against every total the report prints.
    seen: list[tuple] = []
    for decision in plan.decisions:
        if not decision.accepted or decision.action != "cut":
            continue
        for span in decision.spans:
            key = (span.asset_id, round(span.start, 2), round(span.end, 2))
            if key in seen:
                _reject(decision, "duplicate_footage",
                        "another decision already removes this footage")
                break
            seen.append(key)


def _reject(decision: RetentionCutDecision, code: str, why: str) -> None:
    decision.accepted = False
    decision.reject_code = code
    decision.rejected_reason = why


def _first_protected(spans: Sequence[SourceSpan],
                     protected: Sequence[SourceSpan]) -> Optional[SourceSpan]:
    for span in spans:
        if any_overlap(protected, span.asset_id, span.start, span.end):
            return span
    return None


def _touches_base(spans: Sequence[SourceSpan],
                  base_spans: Sequence[SourceSpan]) -> bool:
    for span in spans:
        if any_overlap(base_spans, span.asset_id, span.start, span.end):
            return True
    return False


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def apply(base_ranges: Sequence[SelectedRange], plan: RetentionCutPlan,
          config: RetentionCutConfig) -> list[SelectedRange]:
    """Transform the base ranges. Returns a new list; the input is untouched.

    Four passes over the ranges, in the order the decisions were made:
    protect, then remove, then retime, then place the opening. Removal before
    retiming so a sped-up range is never sped up after being cut in half, and
    the opening last so nothing can shorten it afterwards.
    """
    ranges = [_copy(entry) for entry in base_ranges]

    _apply_protection(ranges, plan)
    ranges = _apply_removals(ranges, plan)
    _apply_speed(ranges, plan)
    ranges = _apply_cold_open(ranges, plan, config)

    return [entry for entry in ranges if entry.duration >= MIN_RANGE]


#: How much of a range protection has to cover before the *whole* range is
#: marked protected.
PROTECTED_SHARE = 0.5


def _apply_protection(ranges: list[SelectedRange],
                      plan: RetentionCutPlan) -> None:
    """Mark claimed footage, so nothing downstream retimes or effects it.

    Marked by *share*, not by any overlap. A ten-second setup inside a
    four-minute clip is protected footage; the clip is not, and flagging the
    whole thing made every later rule treat four minutes of grind as
    untouchable. Removal is checked span by span in ``_subtract`` instead,
    which is both stricter and narrower: the protected seconds cannot be cut,
    and the rest of the clip can.
    """
    protected = plan.protected_spans
    for entry in ranges:
        if entry.duration <= 0:
            continue
        covered = sum(
            span.covers(entry.asset_id, entry.start, entry.end)
            for span in protected
        )
        if covered < entry.duration * PROTECTED_SHARE:
            continue
        entry.protected = True
        if entry.speed != 1.0:
            entry.speed = 1.0
            entry.notes = _note(
                entry, "retention: protected, so kept at full speed")


def _apply_removals(ranges: list[SelectedRange],
                    plan: RetentionCutPlan) -> list[SelectedRange]:
    """Cut footage out, splitting a range when the cut lands in its middle."""
    protected = plan.protected_spans
    cuts: list[tuple] = []
    for decision in plan.accepted:
        if decision.action not in ("cut", "shorten"):
            continue
        for span in decision.spans:
            cuts.append((span, decision))

    out: list[SelectedRange] = []
    for entry in ranges:
        pieces = [entry]
        for span, decision in cuts:
            if span.asset_id != entry.asset_id:
                continue
            nxt: list[SelectedRange] = []
            for piece in pieces:
                nxt.extend(_subtract(piece, span, decision, protected))
            pieces = nxt
        out.extend(pieces)
    return out


def _subtract(entry: SelectedRange, span: SourceSpan,
              decision: RetentionCutDecision,
              protected: Sequence[SourceSpan]) -> list[SelectedRange]:
    """One range minus one span. Zero, one or two ranges come back."""
    if span.end <= entry.start or span.start >= entry.end:
        return [entry]

    # Protected *footage* is never removed. Checked against the span being
    # taken out rather than against a flag on the range: this is the second
    # place the guarantee is enforced, and a decision that slipped through
    # validation still cannot cut a protected second.
    if any_overlap(protected, span.asset_id,
                   max(span.start, entry.start), min(span.end, entry.end)):
        return [entry]

    label = f"retention[{decision.decision_id}]: {decision.reason[:200]}"
    out: list[SelectedRange] = []

    if span.start > entry.start + 0.05:
        head = _copy(entry)
        head.end = round(span.start, 3)
        head.notes = _note(head, label)
        out.append(head)

    if span.end < entry.end - 0.05:
        tail = _copy(entry)
        tail.start = round(span.end, 3)
        tail.notes = _note(tail, label)
        out.append(tail)

    return out


def _apply_speed(ranges: list[SelectedRange],
                 plan: RetentionCutPlan) -> None:
    """Retime what the sag pass wants sped up, unless it is protected."""
    for decision in plan.accepted:
        if decision.action != "speed_up":
            continue
        for span in decision.spans:
            for entry in ranges:
                if entry.asset_id != span.asset_id or entry.protected:
                    continue
                if span.covers(entry.asset_id, entry.start, entry.end) <= 0.1:
                    continue
                # Speech is never sped up, whatever the plan says. The sag
                # pass checks this too; a second check costs nothing and this
                # is the one that touches the actual cut.
                if entry.keep_reason in ("setup", "hold") and entry.speed == 1.0 \
                        and "narration" in (entry.notes or ""):
                    continue
                entry.speed = max(entry.speed, decision.speed)
                entry.keep_reason = entry.keep_reason or "filler"
                entry.notes = _note(
                    entry,
                    f"retention[{decision.decision_id}]: "
                    f"{decision.speed:g}x -- {decision.reason[:160]}")


def _apply_cold_open(ranges: list[SelectedRange], plan: RetentionCutPlan,
                     config: RetentionCutConfig) -> list[SelectedRange]:
    """Put the opening at the front.

    The opening is a *new* range built from the hook's spans rather than a
    reordering of an existing one, because the hook rarely lines up with a
    clip boundary and moving a whole clip would bring the wrong footage with
    it.
    """
    cold = plan.cold_open
    if not cold.chosen or not cold.spans:
        return ranges

    opening: list[SelectedRange] = []
    for span in cold.spans:
        opening.append(SelectedRange(
            asset_id=span.asset_id,
            source_file=span.source_file,
            start=span.start,
            end=span.end,
            keep_reason="reveal",
            speed=1.0,
            protected=True,
            recommendation_ids=[],
            segment_ids=list(span.segment_ids),
            notes=(
                f"retention: COLD OPEN -- {cold.hook_type} lifted from "
                f"{cold.original_start:.0f}s."
                + (f" Asks: {cold.viewer_question}"
                   if cold.viewer_question else "")
            ),
        ))

    if cold.duplicate_policy == "keep":
        return opening + ranges

    # Take the teased footage out of wherever else it appears -- *including*
    # protected ranges, which is the one place this layer is allowed to trim
    # something protection claimed.
    #
    # The justification is that the footage is not being removed from the
    # episode. It has moved to the front. A payoff whose first twelve seconds
    # are now the cold open still plays in full, in order, across the two
    # positions; leaving it in both would play it twice, which is the thing a
    # viewer actually notices.
    remaining: list[SelectedRange] = []
    for entry in ranges:
        pieces = [entry]
        for span in cold.spans:
            nxt: list[SelectedRange] = []
            for piece in pieces:
                nxt.extend(_carve(piece, span))
            pieces = nxt
        for piece in pieces:
            if piece.duration >= MIN_RANGE:
                remaining.append(piece)
    return opening + remaining


def _carve(entry: SelectedRange, span: SourceSpan) -> list[SelectedRange]:
    """One range minus the cold open's footage. Ignores protection.

    The narrow exception to "protection is applied before anything that
    removes": this does not remove footage from the episode, it removes a
    *second copy* of footage that is now at the front.
    """
    if span.asset_id != entry.asset_id:
        return [entry]
    if span.end <= entry.start or span.start >= entry.end:
        return [entry]

    label = "retention: this plays as the cold open instead"
    out: list[SelectedRange] = []
    if span.start > entry.start + 0.05:
        head = _copy(entry)
        head.end = round(span.start, 3)
        head.notes = _note(head, label)
        out.append(head)
    if span.end < entry.end - 0.05:
        tail = _copy(entry)
        tail.start = round(span.end, 3)
        tail.notes = _note(tail, label)
        out.append(tail)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _copy(entry: SelectedRange) -> SelectedRange:
    return SelectedRange(
        asset_id=entry.asset_id,
        source_file=entry.source_file,
        start=entry.start,
        end=entry.end,
        keep_reason=entry.keep_reason,
        speed=entry.speed,
        protected=entry.protected,
        recommendation_ids=list(entry.recommendation_ids),
        segment_ids=list(entry.segment_ids),
        notes=entry.notes,
    )


def _note(entry: SelectedRange, text: str) -> str:
    existing = (entry.notes or "").strip()
    return f"{existing}  {text}".strip()[:500] if existing else text[:500]


def _spans_of(ranges: Sequence[SelectedRange]) -> list[SourceSpan]:
    return [
        SourceSpan(
            asset_id=entry.asset_id,
            source_file=entry.source_file,
            start=entry.start,
            end=entry.end,
            segment_ids=list(entry.segment_ids),
        )
        for entry in ranges
    ]


def _runtime(ranges: Sequence[SelectedRange]) -> float:
    return round(sum(
        entry.duration / (entry.speed if entry.speed > 0 else 1.0)
        for entry in ranges
    ), 3)


def _warn(plan: RetentionCutPlan, config: RetentionCutConfig) -> None:
    """The things a person needs told before trusting this cut."""
    if not plan.cold_open.chosen and config.cold_open:
        plan.warnings.append(
            "No cold open was chosen, so the episode opens where it always "
            f"did. {plan.cold_open.fallback_reason}"
        )
    if plan.applied and plan.base_duration > 0:
        share = 1.0 - (plan.cut_duration / plan.base_duration)
        if share > 0.6:
            plan.warnings.append(
                f"This removed {share:.0%} of the cut. That is a great deal "
                "to take out on retention evidence alone -- watch the proxy "
                "before trusting it."
            )
    for warning in plan.unresolved_warnings:
        if warning not in plan.warnings:
            plan.warnings.append(warning)
    if plan.timebase == "timeline":
        plan.warnings.append(
            "The episode memory was built without a rough cut, so its times "
            "are a synthetic ordering rather than sequence time. Findings "
            "were placed through segment ids, which is right but coarser."
        )
