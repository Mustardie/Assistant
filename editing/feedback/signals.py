"""Turning individual ratings into tendencies.

One person saying "too many captions" once is a reaction. The same person
saying it four times across three sessions is a preference, and that is what
this module tries to find: a ``(dimension, direction)`` pair with evidence
counted, disagreement counted too, and a confidence that is capped by how much
evidence there actually is.

## Why a dimension and a direction rather than a sentence

Because "user dislikes too many captions" and "user wants fewer captions" are
the same preference written twice, and a later session counting sentences would
treat them as two. ``caption_density`` + ``less`` is countable, comparable
across sessions, and renders back to a sentence through one table
(``PREFERENCE_STATEMENTS``) so the wording cannot drift either.

## Three rules that keep this honest

* **Disagreement is kept, not dropped.** A dimension where the editor said
  "more" twice and "less" once produces one signal in the majority direction
  with ``contradictions=1`` and a lowered ``agreement``. Filtering the odd one
  out would manufacture a consistency the data does not have.
* **Confidence is capped by evidence count.** ``PREFERENCE_CAP`` sits below
  1.0 at every count, the same shape as Session 8's channel cap. Six agreeing
  ratings is a strong preference, not a certainty.
* **A preference about timing is never automatically safe.** Same rule as
  Session 8's markers-versus-timing split: a wrong preference about caption
  tone costs a caption nobody liked, and a wrong preference about pace costs
  footage. ``TIMING_DIMENSIONS`` can reach high confidence and still returns
  ``safe_to_apply_automatically=False``, with the reason recorded.

**Nothing applies these.** They are written to ``summary.json`` and exported.
No pass reads them, and this session does not add one that does.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from editing.feedback.schema import (
    FeedbackItem, MIN_EVIDENCE, PREFERENCE_DIMENSIONS, PreferenceSignal,
    SAFE_TO_APPLY_AT_OR_ABOVE, new_id, preference_cap, statement_for,
)
from editing.schema import clamp01

#: Dimensions a style preset already controls, so a preference about one is
#: about *this* style rather than about editing in general.
STYLE_SPECIFIC_DIMENSIONS = frozenset({
    "caption_density", "caption_tone", "caption_quality", "sfx_use",
    "music_use", "marker_clutter", "cut_pace",
})

#: Dimensions whose application would change what is on the timeline and for
#: how long. Never automatically safe, at any confidence.
TIMING_DIMENSIONS = frozenset({
    "cut_pace", "grind_length", "story_vs_speed", "context_amount",
})

#: Agreement below which a preference is never automatically safe, however
#: much evidence there is: a dimension the editor keeps changing their mind
#: about is not a preference, it is a case-by-case judgement.
MIN_AGREEMENT_FOR_SAFE = 0.80

#: Evidence needed before automatic application could even be considered. One
#: more than ``MIN_EVIDENCE``, because the bar for "worth recording" and the
#: bar for "act on this without asking" should not be the same number.
MIN_EVIDENCE_FOR_SAFE = MIN_EVIDENCE + 1

#: At most this many preferences are read out of one rating. A single "bad
#: caption" is evidence about caption quality and possibly about caption tone;
#: it is not evidence about six things, and letting one item vote everywhere
#: is how a preference list becomes noise.
MAX_PER_ITEM = 2


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """One reading of a rating, as a preference.

    ``when`` gets the whole item so a rule can look at the rating, the reason,
    the target type and the label together. Rules are tried in order and the
    first ``MAX_PER_ITEM`` distinct dimensions win, so the specific rules are
    listed before the general ones.
    """

    dimension: str
    direction: str
    when: Callable[[FeedbackItem], bool]
    why: str = ""


def _label(item: FeedbackItem) -> str:
    """Everything textual about the item, lowered, for keyword rules.

    Keyword matching on a label is a *heuristic* and is only ever used to make
    a general rule more specific -- "this bad caption was a danger caption" --
    never to create a preference on its own.
    """
    parts = [item.target.label, item.note, item.target.target_id]
    if item.correction is not None:
        parts.append(item.correction.text)
    return " ".join(str(part or "") for part in parts).lower()


def _rating(item: FeedbackItem) -> str:
    return item.rating.rating


def _has(item: FeedbackItem, *words: str) -> bool:
    text = _label(item)
    return any(word in text for word in words)


def _is(item: FeedbackItem, *ratings: str) -> bool:
    return _rating(item) in ratings


def _cat(item: FeedbackItem, *categories: str) -> bool:
    return bool(set(categories) & set(item.categories))


def _target(item: FeedbackItem, *types: str) -> bool:
    return item.target.target_type in types


RULES: tuple[Rule, ...] = (
    # -- captions, most specific first --------------------------------------
    Rule("caption_tone", "more",
         lambda i: _has(i, "danger_text", "danger", "warn")
         and i.polarity == "positive" and _cat(i, "caption"),
         "a positive rating on a danger caption"),
    Rule("caption_tone", "less",
         lambda i: _has(i, "danger_text", "danger", "warn")
         and i.polarity in ("negative", "corrective") and _cat(i, "caption"),
         "a negative rating on a danger caption"),
    Rule("caption_quality", "more", lambda i: _is(i, "good_caption")),
    Rule("caption_quality", "less", lambda i: _is(i, "bad_caption")),
    Rule("caption_density", "less",
         lambda i: _cat(i, "caption") and _is(i, "too_much", "cut", "bad")),
    Rule("caption_density", "more",
         lambda i: _cat(i, "caption") and _is(i, "too_little")),

    # -- sound ---------------------------------------------------------------
    Rule("music_use", "more",
         lambda i: _is(i, "good_music_sfx")
         and _has(i, "music", "bed", "ambience", "tension_bed")),
    Rule("music_use", "less",
         lambda i: _is(i, "bad_music_sfx", "too_much")
         and _has(i, "music", "bed", "ambience", "tension_bed")),
    Rule("sfx_use", "less",
         lambda i: _cat(i, "audio")
         and _is(i, "bad_music_sfx", "too_much", "wrong_moment", "bad")),
    Rule("sfx_use", "more",
         lambda i: _cat(i, "audio") and _is(i, "good_music_sfx", "too_little")),

    # -- the picture ---------------------------------------------------------
    Rule("hud_visibility", "more",
         lambda i: _has(i, "hud", "hides_gameplay", "hud_risk", "hotbar",
                        "health bar")
         and i.polarity in ("negative", "corrective")),
    Rule("marker_clutter", "less",
         lambda i: _has(i, "marker") and _is(i, "too_much", "bad", "cut")),
    Rule("marker_clutter", "more",
         lambda i: _has(i, "marker") and _is(i, "too_little")),

    # -- story ---------------------------------------------------------------
    Rule("callbacks", "more", lambda i: _is(i, "good_callback")),
    Rule("callback_naturalness", "less", lambda i: _is(i, "forced_callback")),
    Rule("hook_strength", "keep", lambda i: _is(i, "good_hook")),
    Rule("hook_strength", "more", lambda i: _is(i, "bad_hook")),
    Rule("clickbait", "less",
         lambda i: _is(i, "bad_hook")
         and _has(i, "clickbait", "overpromise", "overpromises", "misleading")),
    Rule("payoff_strength", "keep", lambda i: _is(i, "strong_payoff")),
    Rule("payoff_strength", "more", lambda i: _is(i, "weak_payoff")),
    Rule("objective_clarity", "more",
         lambda i: _is(i, "confusing") and _cat(i, "story", "clarity")),
    Rule("transition_clarity", "more",
         lambda i: _is(i, "confusing")
         and (_target(i, "roughcut_placement") or _has(i, "transition"))),
    Rule("context_amount", "more", lambda i: _is(i, "useful_context")),
    Rule("context_amount", "less", lambda i: _is(i, "useless_context")),
    Rule("story_vs_speed", "more",
         lambda i: _is(i, "useful_context", "strong_payoff", "keep")
         and _cat(i, "story")),
    Rule("comedy", "more", lambda i: _is(i, "funny")),

    # -- pace, most general last --------------------------------------------
    Rule("grind_length", "less",
         lambda i: _has(i, "grind", "mining", "repetition", "repetitive")
         and _is(i, "boring", "cut", "shorten", "weak_retention")),
    Rule("cut_pace", "more",
         lambda i: _is(i, "boring", "shorten", "cut", "bad_pacing",
                       "weak_retention")),
    Rule("cut_pace", "less",
         lambda i: _is(i, "extend", "keep", "strong_retention")
         and not _cat(i, "caption", "audio")),
)


def readings(item: FeedbackItem) -> list[tuple[str, str, str]]:
    """The ``(dimension, direction, why)`` triples one item supports."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for rule in RULES:
        if rule.dimension in seen or len(out) >= MAX_PER_ITEM:
            continue
        try:
            matched = bool(rule.when(item))
        except Exception:  # noqa: BLE001 - a bad rule must not lose the item
            matched = False
        if matched:
            seen.add(rule.dimension)
            out.append((rule.dimension, rule.direction, rule.why))
    return out


# ---------------------------------------------------------------------------
# Assembling signals
# ---------------------------------------------------------------------------

def extract(
    items: Sequence[FeedbackItem],
    *,
    style: str = "",
    session_ids: Optional[Sequence[str]] = None,
) -> list[PreferenceSignal]:
    """Every preference the feedback supports, strongest first.

    Items the reviewer was unsure of are excluded: ``unsure`` is a statement
    about the item, and counting it as evidence for a tendency would be the
    layer inventing an opinion nobody expressed.
    """
    usable = [
        item for item in items
        if not item.rating.is_uncertain and item.polarity != "uncertain"
    ]

    votes: dict[str, list[tuple[FeedbackItem, str, str]]] = {}
    for item in usable:
        for dimension, direction, why in readings(item):
            votes.setdefault(dimension, []).append((item, direction, why))

    known_sessions = set(session_ids or ()) or {
        item.session_id for item in items if item.session_id
    }

    signals = [
        _signal(dimension, entries, style=style, sessions=known_sessions)
        for dimension, entries in votes.items()
        if dimension in PREFERENCE_DIMENSIONS
    ]
    signals.sort(
        key=lambda s: (-s.confidence, -s.evidence_count, s.dimension))
    return signals


def _signal(
    dimension: str,
    entries: list[tuple[FeedbackItem, str, str]],
    *,
    style: str,
    sessions: set,
) -> PreferenceSignal:
    by_direction: dict[str, list[FeedbackItem]] = {}
    for item, direction, _why in entries:
        by_direction.setdefault(direction, []).append(item)

    # Most evidence wins; a tie is broken by whose evidence the editor was
    # surer of, then by which they said most recently. An alphabetical
    # tie-break would silently pick "more" over "less" every time two ratings
    # disagreed, which is a coin toss dressed up as a finding.
    direction = max(
        by_direction,
        key=lambda key: (
            len(by_direction[key]),
            sum(item.confidence for item in by_direction[key]),
            max(item.created_at for item in by_direction[key]),
        ),
    )
    agreeing = by_direction[direction]
    total = sum(len(group) for group in by_direction.values())
    contradictions = total - len(agreeing)
    agreement = len(agreeing) / total if total else 0.0

    #: Confidence is the editor's own average certainty, scaled by how
    #: consistently the evidence pointed one way, then capped by how much of it
    #: there is. All three have to be high; any one of them being low is enough
    #: to keep the preference tentative.
    mean_confidence = (
        sum(item.confidence for item in agreeing) / len(agreeing)
        if agreeing else 0.0
    )
    confidence = round(
        min(
            clamp01(mean_confidence * agreement, 0.0),
            preference_cap(len(agreeing)),
        ),
        3,
    )

    positive = [
        item.feedback_id for item in agreeing if item.polarity == "positive"]
    negative = [
        item.feedback_id for item in agreeing
        if item.polarity in ("negative", "corrective")
    ]
    ordered = sorted(agreeing, key=lambda item: item.created_at)

    signal = PreferenceSignal(
        signal_id=new_id("pref", dimension, direction, style),
        dimension=dimension,
        direction=direction,
        statement=statement_for(dimension, direction),
        evidence_count=len(agreeing),
        positive_examples=positive,
        negative_examples=negative,
        source_feedback_ids=[item.feedback_id for item in ordered],
        quotes=[item.summary for item in ordered[:6] if item.summary],
        confidence=confidence,
        agreement=round(agreement, 3),
        contradictions=contradictions,
        is_style_specific=dimension in STYLE_SPECIFIC_DIMENSIONS and bool(style),
        style=style if dimension in STYLE_SPECIFIC_DIMENSIONS else "",
        scope="global" if len(sessions) > 1 else "episode",
        first_seen=ordered[0].created_at if ordered else "",
        last_seen=ordered[-1].created_at if ordered else "",
    )
    _decide_safety(signal)
    return signal


def _decide_safety(signal: PreferenceSignal) -> None:
    """Whether a later session could honour this without asking, and why not.

    Four conditions, and the reason for every failure is recorded. A signal
    that says "not safe" without saying why is the same as no signal at all to
    whoever has to decide what to do about it.
    """
    reasons: list[str] = []
    if signal.dimension in TIMING_DIMENSIONS:
        reasons.append(
            "this preference is about timing: acting on it wrongly costs "
            "footage, and a marker cannot undo it"
        )
    if signal.evidence_count < MIN_EVIDENCE_FOR_SAFE:
        reasons.append(
            f"only {signal.evidence_count} rating(s) support it, below the "
            f"{MIN_EVIDENCE_FOR_SAFE} needed"
        )
    if signal.agreement < MIN_AGREEMENT_FOR_SAFE:
        reasons.append(
            f"the evidence agrees {signal.agreement * 100:.0f}% of the time, "
            f"below {MIN_AGREEMENT_FOR_SAFE * 100:.0f}%"
        )
    if signal.confidence < SAFE_TO_APPLY_AT_OR_ABOVE:
        reasons.append(
            f"confidence {signal.confidence:.2f} is below the "
            f"{SAFE_TO_APPLY_AT_OR_ABOVE:.2f} floor"
        )

    signal.safe_to_apply_automatically = not reasons
    signal.why_not_safe = "; ".join(reasons)
    if not reasons:
        signal.why_not_safe = (
            "the evidence would support acting on this -- but nothing reads "
            "preference signals yet, and turning one on is a decision for a "
            "later session, not a licence granted here"
        )


def summarise(signals: Sequence[PreferenceSignal]) -> dict:
    return {
        "signals": len(signals),
        "with_enough_evidence": sum(
            1 for s in signals if s.evidence_count >= MIN_EVIDENCE),
        "contradicted": sum(1 for s in signals if s.contradictions),
        "style_specific": sum(1 for s in signals if s.is_style_specific),
        "global": sum(1 for s in signals if s.is_global),
        "would_be_safe_to_apply": sum(
            1 for s in signals if s.safe_to_apply_automatically),
        "dimensions": sorted({s.dimension for s in signals}),
    }
