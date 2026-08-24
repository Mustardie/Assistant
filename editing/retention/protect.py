"""Keeping the things that make later things land.

Runs **before** any compression, and that ordering is the mechanism. A setup a
kept payoff needs is claimed here; every rule that runs afterwards checks the
claim and refuses to touch it. There is no negotiation later, because a
negotiation is a place for a bug to live.

## What gets protected, and on what evidence

``setup -> payoff``
    Session 8 links them. A setup whose payoff is *in the cut* is protected:
    without it the payoff arrives from nowhere. A setup whose payoff was cut
    is **not** protected -- it is footage with no reason to be there, and
    saying so is more useful than defending it.

``payoff``
    Kept whole. Not shortened below ``min_payoff_share``, never sped up. The
    peak is protected the same way, and additionally.

``callback``
    A callback only works if what it calls back to is still in the cut, so the
    earlier moment is protected whenever the callback is kept.

## The two warnings

A payoff in the cut whose setup is missing, and a setup in the cut whose payoff
is missing. Neither is an error and neither is automatically fixable -- one
means a viewer will not understand the moment, the other means a viewer is
waiting for something that never comes. Both are reported, because they are
exactly the sort of thing a person watching the proxy will feel and not be able
to name.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.retention.resolve import Resolver, any_overlap
from editing.retention.schema import (
    PayoffProtectionDecision, RetentionCutConfig, RetentionCutDecision,
    SetupProtectionDecision, SourceSpan, decision_id_for,
)

logger = logging.getLogger("nova.editing.retention.protect")

#: How much of a payoff has to survive in the base cut before it counts as
#: "kept". Half, because handles and merges mean a payoff is rarely covered to
#: the frame, and a payoff half in the cut still needs its setup.
KEPT_COVERAGE = 0.5


def protect(
    memory,
    resolver: Resolver,
    config: RetentionCutConfig,
    base_spans: Sequence[SourceSpan],
    *,
    climax=None,
) -> tuple:
    """Work out what may not be touched.

    Returns ``(setups, payoffs, decisions)``. The decisions are the ones that
    claim footage; the two record lists are what the report reads.
    """
    payoff_records: list[PayoffProtectionDecision] = []
    setup_records: list[SetupProtectionDecision] = []
    decisions: list[RetentionCutDecision] = []

    payoffs = list(getattr(memory, "payoffs", []) or [])
    setups = list(getattr(memory, "setups", []) or [])
    callbacks = list(getattr(memory, "callbacks", []) or [])

    kept_payoff_ids: set = set()

    # -- payoffs ---------------------------------------------------------
    for payoff in payoffs:
        record, decision, kept = _payoff(
            payoff, resolver, config, base_spans, setups, climax)
        payoff_records.append(record)
        if decision is not None:
            decisions.append(decision)
        if kept:
            kept_payoff_ids.add(record.payoff_id)

    # The peak, when the memory did not record it as a payoff.
    if climax is not None:
        climax_id = str(getattr(climax, "item_id", ""))
        if climax_id and not any(r.payoff_id == climax_id
                                 for r in payoff_records):
            record, decision, kept = _payoff(
                climax, resolver, config, base_spans, setups, climax)
            record.is_climax = True
            payoff_records.append(record)
            if decision is not None:
                decisions.append(decision)
            if kept:
                kept_payoff_ids.add(record.payoff_id)

    # -- setups ----------------------------------------------------------
    for setup in setups:
        record, decision = _setup(
            setup, resolver, config, base_spans, kept_payoff_ids)
        setup_records.append(record)
        if decision is not None:
            decisions.append(decision)

    # -- callbacks -------------------------------------------------------
    if config.protect_callbacks:
        for callback in callbacks:
            decision = _callback(callback, resolver, config, base_spans)
            if decision is not None:
                decisions.append(decision)

    return setup_records, payoff_records, decisions


def _payoff(payoff, resolver: Resolver, config: RetentionCutConfig,
            base_spans: Sequence[SourceSpan], setups: Sequence,
            climax) -> tuple:
    """One payoff: is it in the cut, is its setup, and protect it."""
    item_id = str(getattr(payoff, "item_id", ""))
    start = float(getattr(payoff, "start", 0.0))
    end = float(getattr(payoff, "end", 0.0))
    spans = resolver.resolve_item(payoff)

    is_climax = climax is not None and str(
        getattr(climax, "item_id", "")) == item_id
    record = PayoffProtectionDecision(
        payoff_id=item_id,
        setup_id=str(getattr(payoff, "setup_id", "")),
        episode_start=start,
        episode_end=end,
        spans=spans,
        confidence=float(getattr(payoff, "confidence", 0.0)),
        is_climax=is_climax,
    )

    if not spans:
        record.reason = (
            "This payoff is not in the cut being edited, so there is nothing "
            "to protect."
        )
        return record, None, False

    kept = _is_kept(spans, base_spans)
    if not kept:
        record.reason = (
            "The base cut does not include this payoff, so it was not "
            "protected."
        )
        record.warning = (
            f"A payoff at {start:.0f}s is not in the cut. Anything set up for "
            "it is now footage with no destination."
        )
        return record, None, False

    if not config.protect_payoffs:
        record.reason = "Payoff protection is switched off for this cut."
        return record, None, True

    record.protected = True
    record.reason = (
        "The peak of the episode: kept whole, at full speed."
        if is_climax else
        "A payoff the episode builds to: kept whole, at full speed."
    )

    # Setup present?
    setup_id = record.setup_id
    if setup_id:
        setup = next((item for item in setups
                      if str(getattr(item, "item_id", "")) == setup_id), None)
        if setup is not None:
            setup_spans = resolver.resolve_item(setup)
            record.setup_kept = bool(setup_spans) and _is_kept(
                setup_spans, base_spans)
            if not record.setup_kept:
                record.warning = (
                    f"This payoff is in the cut and its setup ({setup_id}) is "
                    "not. A viewer reaches the moment without knowing why it "
                    "matters."
                )
    else:
        record.warning = (
            f"A payoff at {start:.0f}s has no setup recorded anywhere in the "
            "episode. It may land as something that simply happens."
        )

    decision = RetentionCutDecision(
        decision_id=decision_id_for("protect_payoff", item_id, start),
        action="protect",
        source_type="climax" if is_climax else "payoff",
        source_id=item_id,
        episode_start=start,
        episode_end=end,
        spans=spans,
        confidence=record.confidence,
        priority=1.0,
        reason=record.reason,
        evidence=[item_id],
        viewer_effect="protects_a_payoff",
    )
    return record, decision, True


def _setup(setup, resolver: Resolver, config: RetentionCutConfig,
           base_spans: Sequence[SourceSpan],
           kept_payoff_ids: set) -> tuple:
    """One setup: protected only when the thing it pays off is staying in."""
    item_id = str(getattr(setup, "item_id", ""))
    payoff_id = str(getattr(setup, "payoff_id", ""))
    start = float(getattr(setup, "start", 0.0))
    end = float(getattr(setup, "end", 0.0))
    spans = resolver.resolve_item(setup)

    record = SetupProtectionDecision(
        setup_id=item_id,
        payoff_id=payoff_id,
        payoff_kept=payoff_id in kept_payoff_ids,
        episode_start=start,
        episode_end=end,
        spans=spans,
        confidence=float(getattr(setup, "confidence", 0.0)),
    )

    if not payoff_id:
        record.reason = (
            "Nothing in the episode pays this off, so it is not protected."
        )
        if spans and _is_kept(spans, base_spans):
            record.warning = (
                f"A setup at {start:.0f}s is in the cut and never pays off. A "
                "viewer is left waiting for something that does not come."
            )
        return record, None

    if not record.payoff_kept:
        record.reason = (
            f"Its payoff ({payoff_id}) is not in the cut, so this setup has "
            "nothing to protect."
        )
        return record, None

    if not spans:
        record.reason = (
            "This setup is not in the cut being edited, so there is nothing "
            "to protect."
        )
        record.warning = (
            f"The payoff for {payoff_id} is in the cut and its setup is not. "
            "The moment will arrive from nowhere."
        )
        return record, None

    if not config.protect_setups:
        record.reason = "Setup protection is switched off for this cut."
        return record, None

    record.protected = True
    record.reason = (
        f"Needed for the payoff at {payoff_id}, which is staying in the cut. "
        "Without it that moment arrives from nowhere."
    )

    decision = RetentionCutDecision(
        decision_id=decision_id_for("protect_setup", item_id, start),
        action="protect",
        source_type="setup",
        source_id=item_id,
        episode_start=start,
        episode_end=end,
        spans=spans,
        confidence=record.confidence,
        priority=0.9,
        reason=record.reason,
        evidence=[item_id, payoff_id],
        viewer_effect="protects_a_payoff",
    )
    return record, decision


def _callback(callback, resolver: Resolver, config: RetentionCutConfig,
              base_spans: Sequence[SourceSpan]
              ) -> Optional[RetentionCutDecision]:
    """A callback only works if what it calls back to is still there."""
    item_id = str(getattr(callback, "item_id", ""))
    start = float(getattr(callback, "start", 0.0))
    end = float(getattr(callback, "end", 0.0))
    spans = resolver.resolve_item(callback)
    if not spans or not _is_kept(spans, base_spans):
        return None

    return RetentionCutDecision(
        decision_id=decision_id_for("protect_callback", item_id, start),
        action="protect",
        source_type="callback",
        source_id=item_id,
        episode_start=start,
        episode_end=end,
        spans=spans,
        confidence=float(getattr(callback, "confidence", 0.0)),
        priority=0.7,
        reason=(
            "A callback: the moment it refers to has to stay in the cut or "
            "the reference lands on nothing."
        ),
        evidence=[item_id],
        viewer_effect="lands_a_joke",
    )


def _is_kept(spans: Sequence[SourceSpan],
             base_spans: Sequence[SourceSpan]) -> bool:
    """Whether the base cut actually contains this footage."""
    wanted = sum(span.duration for span in spans)
    if wanted <= 0:
        return False
    covered = 0.0
    for span in spans:
        for base in base_spans:
            covered += base.covers(span.asset_id, span.start, span.end)
    return covered >= wanted * KEPT_COVERAGE


def spans_of(decisions: Sequence[RetentionCutDecision]) -> list[SourceSpan]:
    """Every span the protection decisions claimed."""
    out: list[SourceSpan] = []
    for decision in decisions:
        if decision.protects:
            out.extend(decision.spans)
    return out
