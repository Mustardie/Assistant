"""The half that decides.

The model proposes. This disposes, and it is the reason a language model is
allowed anywhere near an edit in this system.

## Eleven checks, in a fixed order

Order matters, and it is: **validity, then premise, then conflict, then
ceilings.** A decision that refers to nothing cannot be checked for
overlapping; a decision whose premise is false should be rejected before its
duration is counted against a budget. Running the cheap structural checks
first also means the expensive whole-plan checks see a smaller, saner list.

Each check does one of three things and records which:

``reject``  the decision is out, with a named reason
``modify``  the decision stays, changed, and the change is recorded
``warn``    the decision stands and something is noted

## What each check is actually protecting

* **resolvable / valid_range** -- a decision must name real footage.
* **confidence** -- below the floor a decision may be recorded but never
  change a frame. Same line Session 8 draws.
* **evidence** -- a decision that cites nothing is a hunch. Hunches are kept
  as ``needs_human_review`` rather than acted on.
* **speech_speed** -- sped-up dialogue is unusable, full stop. This one
  modifies rather than rejects, because the *judgement* (this is dull) is
  usually right even when the remedy is wrong.
* **protected_payoff** -- a payoff the episode built to is never cut, never
  retimed. If the model asks, the ask is refused and recorded.
* **required_setup** -- cutting the setup for a kept payoff makes the payoff
  arrive from nowhere. This is the single check that most justifies the whole
  layer: it is a *whole-episode* constraint no local heuristic can see.
* **overlap** -- two kept ranges over the same footage would duplicate it.
* **hook_ceiling / callback_ceiling** -- three hooks is not a hook.
* **grind_budget** -- keeping forty minutes of tunnelling at 2x is still
  twenty minutes of tunnelling.
* **duration** -- the hard runtime cap, applied last, because it is the only
  check that decides between decisions that are each individually fine.

## The rule about rejection

**Nothing is deleted.** A rejected decision stays in the plan with the check
that rejected it and why, exactly as Session 2's safety pass keeps what it
refused. A plan where half the model's ideas were thrown away silently would
be impossible to argue with, and arguing with it is the point.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.director.schema import (
    MIN_ACTIONABLE_CONFIDENCE, SINGLE_CHANNEL_CAP, DirectorConfig,
    DirectorContext, DirectorDecision, DirectorRange, DirectorSafetyReview,
    SafetyViolation,
)

logger = logging.getLogger("nova.editing.director.safety")

#: The checks, in the order they run.
CHECKS = (
    "resolvable",
    "valid_range",
    "confidence",
    "evidence",
    "speech_speed",
    "protected_payoff",
    "required_setup",
    "overlap",
    "hook_ceiling",
    "callback_ceiling",
    "grind_budget",
    "duration",
)

#: Ranges overlapping by less than this are treated as touching, not colliding
#: -- selection handles is how two adjacent ranges end up sharing a frame.
OVERLAP_TOLERANCE = 0.35

#: A decision cutting a range that a kept payoff's setup covers by at least
#: this fraction is cutting that setup.
SETUP_COVERAGE = 0.5


def review(
    decisions: Sequence[DirectorDecision],
    context: DirectorContext,
    *,
    config: Optional[DirectorConfig] = None,
) -> tuple:
    """Run every check. Returns ``(decisions, ranges, review)``.

    The decisions come back *in place* -- same objects, with ``accepted``,
    ``modified``, ``rejected_reason`` and ``safety_notes`` filled in. Callers
    keep the whole list, including the rejections, because a plan that shows
    only what survived cannot be argued with.
    """
    config = (config or DirectorConfig()).validated()
    entries = list(decisions)
    record = DirectorSafetyReview(
        checks_run=list(CHECKS), proposed=len(entries))

    for decision in entries:
        decision.accepted = True
        decision.rejected_reason = ""

    _resolvable(entries, context, record)
    _valid_range(entries, context, record)
    _confidence(entries, config, record)
    _evidence(entries, record)
    _speech_speed(entries, context, record)
    _protected_payoff(entries, context, record)
    _required_setup(entries, context, record)
    _overlap(entries, record)
    _hook_ceiling(entries, config, record)
    _callback_ceiling(entries, config, record)
    _grind_budget(entries, config, record)

    ranges = _ranges_of(entries)
    ranges = _duration(entries, ranges, config, record)

    record.accepted = sum(1 for entry in entries if entry.accepted)
    record.rejected = sum(1 for entry in entries if not entry.accepted)
    record.modified = sum(1 for entry in entries if entry.modified)
    record.measurements.update({
        "cut_seconds": round(sum(item.cut_duration for item in ranges), 2),
        "source_seconds": round(sum(item.duration for item in ranges), 2),
        "ranges": len(ranges),
    })

    if not ranges and entries:
        record.warnings.append(
            "Every decision that would have put footage in the cut was "
            "rejected. The cut is empty; `director show-rejected` says why."
        )
    _explain_a_silent_episode(entries, context, config, record)
    return entries, ranges, record


def _explain_a_silent_episode(
    decisions, context: DirectorContext, config: DirectorConfig, record
) -> None:
    """Name the usual cause when most decisions were capped out of the cut.

    Footage with no transcript gives the director one channel of evidence,
    which caps every decision at 0.45 -- below the 0.55 a decision needs to
    change a frame. That is the correct outcome (a director working from
    pictures alone is guessing) and a baffling one to read as twelve separate
    "confidence too low" rejections. So it gets said once, with the fix.
    """
    capped = [
        entry for entry in decisions
        if any("confidence capped" in note for note in entry.safety_notes)
    ]
    if len(capped) < max(2, len(decisions) // 2):
        return
    if context.sources.get("transcript"):
        record.warnings.append(
            f"{len(capped)} decision(s) were capped at {SINGLE_CHANNEL_CAP} "
            "because only one channel of evidence covers their range."
        )
        return
    record.warnings.append(
        f"{len(capped)} of {len(decisions)} decision(s) were capped at "
        f"{SINGLE_CHANNEL_CAP} and so cannot reach the "
        f"{config.min_confidence:.2f} needed to change the cut. The cause is "
        "that this episode has no transcript: with only picture to go on the "
        "director has one channel of evidence, and one channel can never "
        "corroborate itself. Transcribe the footage "
        "(`transcribe folder <path>`) and run this again."
    )


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def _reject(
    decision: DirectorDecision, record: DirectorSafetyReview,
    check: str, message: str, **detail,
) -> None:
    decision.accepted = False
    decision.rejected_reason = message
    record.violations.append(SafetyViolation(
        check=check, decision_id=decision.decision_id,
        severity="reject", message=message, detail=dict(detail),
    ))


def _modify(
    decision: DirectorDecision, record: DirectorSafetyReview,
    check: str, message: str, **detail,
) -> None:
    decision.modified = True
    decision.modifications.append(message)
    record.violations.append(SafetyViolation(
        check=check, decision_id=decision.decision_id,
        severity="modify", message=message, detail=dict(detail),
    ))


def _warn(
    decision: DirectorDecision, record: DirectorSafetyReview,
    check: str, message: str, **detail,
) -> None:
    decision.safety_notes.append(message)
    record.violations.append(SafetyViolation(
        check=check, decision_id=decision.decision_id,
        severity="warn", message=message, detail=dict(detail),
    ))


def _live(decisions: Sequence[DirectorDecision]) -> list[DirectorDecision]:
    return [entry for entry in decisions if entry.accepted]


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def _resolvable(decisions, context: DirectorContext, record) -> None:
    """Every decision must name at least one segment that exists."""
    known = context.segment_ids
    for decision in _live(decisions):
        if not decision.segment_ids:
            _reject(decision, record, "resolvable",
                    "names no segment, so there is nothing to act on")
            continue
        missing = [i for i in decision.segment_ids if i not in known]
        if len(missing) == len(decision.segment_ids):
            _reject(decision, record, "resolvable",
                    "every segment it names is unknown -- the model invented "
                    "this range",
                    segment_ids=decision.segment_ids[:10])


def _valid_range(decisions, context: DirectorContext, record) -> None:
    """Times must be real, ordered, and inside the footage."""
    for decision in _live(decisions):
        if decision.end <= decision.start:
            _reject(decision, record, "valid_range",
                    f"the range {decision.start:.2f}-{decision.end:.2f}s is "
                    "empty or reversed")
            continue
        if not decision.keeps_footage:
            continue
        if decision.out_end <= decision.out_start:
            _reject(decision, record, "valid_range",
                    "the selected output range is empty")
            continue
        if decision.out_start < decision.start - 0.01 \
                or decision.out_end > decision.end + 0.01:
            before = (decision.out_start, decision.out_end)
            decision.out_start = max(decision.start, decision.out_start)
            decision.out_end = min(decision.end, decision.out_end)
            _modify(decision, record, "valid_range",
                    "the selected range reached outside the segments it "
                    "names and was clamped back inside them",
                    was=[round(value, 3) for value in before])
        if decision.speed <= 0 or decision.speed > 8.0:
            _modify(decision, record, "valid_range",
                    f"speed {decision.speed:g}x is not usable; rendered at 1x",
                    speed=decision.speed)
            decision.speed = 1.0


def _confidence(decisions, config: DirectorConfig, record) -> None:
    """Below the floor, a decision is a note rather than an edit."""
    floor = max(config.min_confidence, 0.0)
    for decision in _live(decisions):
        if decision.changes_nothing:
            continue
        if decision.confidence < floor:
            _modify(decision, record, "confidence",
                    f"confidence {decision.confidence:.2f} is below the "
                    f"{floor:.2f} needed to change the cut; kept as a note "
                    "for a person",
                    confidence=round(decision.confidence, 3))
            decision.action = "needs_human_review"


def _evidence(decisions, record) -> None:
    """A decision that cites nothing is a hunch, and is treated as one."""
    for decision in _live(decisions):
        if decision.changes_nothing:
            continue
        has_reason = bool(decision.reason.text.strip())
        has_evidence = bool(decision.evidence) or bool(
            decision.beat_id or decision.payoff_id or decision.setup_id
            or decision.open_loop_id or decision.suggestion_id
            or decision.recommendation_ids)
        if has_reason and has_evidence:
            continue
        if not has_reason:
            _modify(decision, record, "evidence",
                    "no reason was given, so this cannot be reviewed; kept as "
                    "a note for a person")
            decision.action = "needs_human_review"
        else:
            _warn(decision, record, "evidence",
                  "reasoned but cites no evidence beyond the range itself")


def _speech_speed(decisions, context: DirectorContext, record) -> None:
    """Never retime somebody talking.

    Modified rather than rejected: "this stretch is dull" is usually a correct
    judgement even when "so speed it up" is the wrong remedy, and throwing the
    judgement away with the remedy loses the useful half.
    """
    for decision in _live(decisions):
        if decision.action != "speed_up" or decision.speed == 1.0:
            continue
        speaking = [
            segment_id for segment_id in decision.segment_ids
            if (context.segment(segment_id) or _blank()).said
        ]
        if not speaking:
            continue
        _modify(decision, record, "speech_speed",
                "somebody is talking over this range, and sped-up dialogue is "
                "unusable; kept at full speed",
                segment_ids=speaking[:5])
        decision.action = "keep"
        decision.speed = 1.0


def _protected_payoff(decisions, context: DirectorContext, record) -> None:
    """A payoff the episode built to is never cut and never retimed.

    The premise is checked before the action, the way Session 4's critic does:
    the payoffs come from the episode memory in the *context*, so a model that
    invents one cannot protect anything, and a model that wants to cut a real
    one is refused.
    """
    payoffs = _spans(context.payoffs)
    climax = _span_of(context.climax)
    if climax:
        payoffs.append(("climax", climax[0], climax[1]))
    if not payoffs:
        return

    for decision in _live(decisions):
        hit = _covering(payoffs, decision.start, decision.end)
        if hit is None:
            continue
        item_id, _start, _end = hit
        if decision.action == "cut":
            _reject(decision, record, "protected_payoff",
                    f"this range holds the payoff the episode builds to "
                    f"({item_id}); cutting it removes what everything before "
                    "it was for",
                    payoff_id=item_id)
        elif decision.action == "speed_up":
            _modify(decision, record, "protected_payoff",
                    f"retimes the payoff ({item_id}); kept at full speed",
                    payoff_id=item_id)
            decision.action = "hold"
            decision.speed = 1.0
        elif decision.action == "shorten":
            _modify(decision, record, "protected_payoff",
                    f"trims the payoff ({item_id}); kept whole",
                    payoff_id=item_id)
            decision.action = "hold"
            decision.out_start = decision.start
            decision.out_end = decision.end
        elif decision.action == "keep":
            # An earlier check may already have downgraded a retime to a plain
            # keep -- and a plain keep over a payoff is still unprotected, so
            # a later style or asset pass could zoom or duck it. Anything that
            # keeps a payoff ends up holding it.
            _modify(decision, record, "protected_payoff",
                    f"keeps the payoff ({item_id}), so it is held: no retime "
                    "and no effects on top of it",
                    payoff_id=item_id)
            decision.action = "hold"
            decision.speed = 1.0


def _required_setup(decisions, context: DirectorContext, record) -> None:
    """Do not cut the setup for a payoff that is staying in.

    The check that most justifies this whole layer. No local heuristic can
    see it: the setup looks like nothing, and the only reason to keep it sits
    twenty minutes later.
    """
    if not context.setups:
        return

    kept_payoff_ids = set()
    for decision in _live(decisions):
        if not decision.keeps_footage:
            continue
        for item_id, start, end in _spans(context.payoffs):
            if _overlap_seconds(decision.start, decision.end, start, end) > 0:
                kept_payoff_ids.add(item_id)
        if decision.payoff_id:
            kept_payoff_ids.add(decision.payoff_id)

    required = [
        (setup.get("id", ""), float(setup.get("start", 0.0)),
         float(setup.get("end", 0.0)), setup.get("payoff_id", ""))
        for setup in context.setups
        if setup.get("payoff_id") and setup.get("payoff_id") in kept_payoff_ids
    ]
    if not required:
        return

    for decision in _live(decisions):
        if decision.action != "cut":
            continue
        for setup_id, start, end, payoff_id in required:
            span = max(0.0, end - start)
            covered = _overlap_seconds(decision.start, decision.end, start, end)
            if span > 0 and covered >= span * SETUP_COVERAGE:
                _reject(decision, record, "required_setup",
                        f"cuts the setup ({setup_id}) for a payoff that stays "
                        f"in the cut ({payoff_id}); the payoff would arrive "
                        "from nowhere",
                        setup_id=setup_id, payoff_id=payoff_id)
                break


def _overlap(decisions, record) -> None:
    """Two kept ranges over the same footage would use it twice.

    The earlier decision wins on ties, and a protected one wins outright: the
    same rule Session 3's merge uses, so a director cut and a heuristic cut
    resolve a collision the same way.
    """
    kept = [entry for entry in _live(decisions) if entry.keeps_footage]
    kept.sort(key=lambda entry: (entry.asset_id, entry.out_start, entry.order))

    for index, decision in enumerate(kept):
        for other in kept[index + 1:]:
            if other.asset_id != decision.asset_id:
                continue
            if other.out_start >= decision.out_end - OVERLAP_TOLERANCE:
                continue
            overlap = _overlap_seconds(
                decision.out_start, decision.out_end,
                other.out_start, other.out_end)
            if overlap <= OVERLAP_TOLERANCE:
                continue

            loser, winner = other, decision
            if other.is_protecting and not decision.is_protecting:
                loser, winner = decision, other

            if overlap >= loser.out_duration - 0.05:
                _reject(loser, record, "overlap",
                        f"covers the same footage as {winner.decision_id}, "
                        "which keeps it",
                        overlap=round(overlap, 2))
            else:
                before = (loser.out_start, loser.out_end)
                if loser.out_start < winner.out_start:
                    loser.out_end = winner.out_start
                else:
                    loser.out_start = winner.out_end
                _modify(loser, record, "overlap",
                        f"trimmed by {overlap:.2f}s where it overlapped "
                        f"{winner.decision_id}",
                        was=[round(value, 3) for value in before])


def _hook_ceiling(decisions, config: DirectorConfig, record) -> None:
    """Three hooks is not a hook."""
    hooks = [entry for entry in _live(decisions) if entry.action == "hook"]
    if len(hooks) <= config.max_hooks_in_cut:
        return
    hooks.sort(key=lambda entry: (entry.priority, entry.confidence),
               reverse=True)
    for decision in hooks[config.max_hooks_in_cut:]:
        _modify(decision, record, "hook_ceiling",
                f"more than {config.max_hooks_in_cut} hook(s) were chosen; "
                "this one is kept as an ordinary clip",
                priority=round(decision.priority, 2))
        decision.action = "keep"
        decision.order = 100


def _callback_ceiling(decisions, config: DirectorConfig, record) -> None:
    """Callbacks get annoying faster than anything else in an edit."""
    callbacks = [e for e in _live(decisions) if e.action == "callback"]
    if len(callbacks) <= config.max_callbacks_in_cut:
        return
    callbacks.sort(key=lambda entry: (entry.priority, entry.confidence),
                   reverse=True)
    for decision in callbacks[config.max_callbacks_in_cut:]:
        _modify(decision, record, "callback_ceiling",
                f"more than {config.max_callbacks_in_cut} callback(s) were "
                "chosen; this one is kept as an ordinary clip")
        decision.action = "keep"


def _grind_budget(decisions, config: DirectorConfig, record) -> None:
    """Forty minutes of tunnelling at 2x is still twenty minutes of it."""
    if config.max_grind_seconds <= 0:
        return
    # ``speed_up`` is grind by definition, and a keep the director itself
    # called repetitive is grind by its own account. A keep reasoned as
    # "pacing" is *not*: that is the natural category for an ordinary keep,
    # and counting it here made the budget reject most of a normal cut.
    grind = [
        entry for entry in _live(decisions)
        if entry.action == "speed_up"
        or (entry.action == "keep"
            and entry.reason.category == "boring_repetition")
    ]
    total = sum(entry.cut_duration for entry in grind)
    record.measurements["grind_seconds"] = round(total, 2)
    if total <= config.max_grind_seconds:
        return

    grind.sort(key=lambda entry: (entry.priority, entry.confidence))
    for decision in grind:
        if total <= config.max_grind_seconds:
            break
        total -= decision.cut_duration
        _reject(decision, record, "grind_budget",
                f"the cut held {record.measurements['grind_seconds']:.0f}s of "
                f"low-value footage against a {config.max_grind_seconds:.0f}s "
                "budget; this was the least defensible of it",
                cut_seconds=round(decision.cut_duration, 2))


def _duration(
    decisions, ranges: list[DirectorRange], config: DirectorConfig, record
) -> list[DirectorRange]:
    """The hard runtime cap, applied last.

    Last because it is the only check that decides between decisions that are
    each individually fine, and it should only ever have to arbitrate what the
    others have already left standing. Protected ranges are dropped last, in
    priority order, so a runtime cap can never remove the payoff first.
    """
    limit = config.max_duration
    total = sum(item.cut_duration for item in ranges)
    record.measurements["cut_seconds_before_cap"] = round(total, 2)
    if limit <= 0 or total <= limit:
        return ranges

    by_id = {entry.decision_id: entry for entry in decisions}

    def sort_key(item: DirectorRange):
        decision = by_id.get(item.decision_id)
        priority = decision.priority if decision else 0.0
        confidence = decision.confidence if decision else 0.0
        return (item.protected, priority, confidence)

    ordered = sorted(ranges, key=sort_key)
    kept = list(ranges)
    for item in ordered:
        if total <= limit:
            break
        decision = by_id.get(item.decision_id)
        total -= item.cut_duration
        kept.remove(item)
        if decision is not None:
            _reject(decision, record, "duration",
                    f"the cut ran to "
                    f"{record.measurements['cut_seconds_before_cap']:.0f}s "
                    f"against a {limit:.0f}s maximum; this was dropped to fit",
                    cut_seconds=round(item.cut_duration, 2))
    record.measurements["cut_seconds_after_cap"] = round(total, 2)
    record.warnings.append(
        f"The cut was trimmed to fit the {limit:.0f}s maximum. "
        f"{len(ranges) - len(kept)} range(s) were dropped, least important "
        "first."
    )
    return kept


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ranges_of(decisions: Sequence[DirectorDecision]) -> list[DirectorRange]:
    """Accepted decisions as concrete ranges, in play order.

    Hooks come first, whatever their source time -- that *is* what a hook is.
    Everything else plays in source order, so the episode still runs forwards.
    """
    out: list[DirectorRange] = []
    for decision in decisions:
        if not decision.accepted:
            continue
        item = decision.as_range()
        if item is not None:
            out.append(item)
    # Hooks first, whatever their source time -- that is what a hook is.
    # Everything else in source order, so the episode still runs forwards.
    out.sort(key=lambda item: (
        0 if item.is_hook else 1,
        item.order if item.is_hook else 0,
        item.source_file,
        item.start,
    ))
    return out


def _spans(items: Sequence[dict]) -> list:
    out = []
    for item in items or ():
        try:
            out.append((
                str(item.get("id", "")),
                float(item.get("start", 0.0)),
                float(item.get("end", 0.0)),
            ))
        except (TypeError, ValueError):
            continue
    return out


def _span_of(item: dict) -> Optional[tuple]:
    if not item:
        return None
    try:
        return (float(item.get("start", 0.0)), float(item.get("end", 0.0)))
    except (TypeError, ValueError):
        return None


def _covering(spans: Sequence, start: float, end: float) -> Optional[tuple]:
    """The first span this range overlaps meaningfully."""
    for entry in spans:
        item_id, span_start, span_end = entry
        if _overlap_seconds(start, end, span_start, span_end) > 0.5:
            return entry
    return None


def _overlap_seconds(a_start: float, a_end: float,
                     b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


class _Blank:
    said = ""
    audio: list = []
    importance = "unknown"


def _blank():
    return _Blank()
