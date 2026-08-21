"""Choosing what is actually worth asking about.

A finished run produces a few hundred decisions. Asking about all of them is
the same as asking about none of them -- the review is abandoned at item forty
and the feedback that does arrive is from the least interesting end of the
list, because that is where the reviewer still had patience.

So this module is a *selection* problem, not a listing problem. It gathers
candidates from every pass, scores them by how much the answer would be worth,
and returns a short queue that is deliberately not the top N by score.

## Why not just the top N

Because a pure ranking has three failure modes, and each has a rule here:

* **One pass floods it.** The style layer produces the most items, so an
  unweighted ranking is thirty captions and nothing else. ``RESERVED`` gives
  the structural, risky and uncertain items guaranteed slots before the
  ranking fills the rest.
* **Only failures get asked about.** A review made entirely of things that
  might be wrong teaches a later session what the editor dislikes and nothing
  about what to keep doing. ``POSITIVE_RATIO`` reserves a few slots for
  decisions that look *good*, marked ``positive_sample`` so nobody mistakes
  them for problems.
* **The same moment is asked about six times.** A caption, an SFX, a beat and
  a risk zone at 4:12 are four records of one moment. They are collapsed when
  they are near-identical and grouped when they are merely adjacent, so the
  reviewer reads one moment at a time.

## What a prompt has to carry

The system's decision and its confidence, every time. Feedback given without
seeing what the system thought is feedback about the video; feedback given with
it is feedback about the decision. Only the second kind is worth collecting
here, and a prompt with an empty ``system_decision`` is a bug in a generator.

Nothing in this module reads Premiere, FFmpeg, a model or the footage. It reads
JSON that other passes already wrote.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional, Sequence

from editing.feedback import targets as targets_module
from editing.feedback.schema import (
    NOT_MEASURED, ReviewPrompt, ReviewQueue, coerce_many, new_id,
    REASON_CATEGORIES, PROMPT_SOURCES,
)
from editing.feedback.targets import Artifacts
from editing.schema import clamp01

#: How many items a review queue defaults to. Twenty is about fifteen minutes
#: of honest reviewing, which is roughly as much as anyone does in one sitting.
DEFAULT_LIMIT = 20

#: Hard ceiling, so `--limit 5000` cannot produce something nobody will read.
MAX_LIMIT = 200

#: Candidates generated per source before ranking. A 200-clip cut must not put
#: 200 clip prompts into the pool; each generator picks its own most
#: interesting ones first and stops here.
PER_SOURCE_CANDIDATES = 40

#: Two prompts of the same kind closer than this collapse into one.
NEAR_WINDOW = 5.0

#: Prompts within this many seconds of each other share a group, so a reviewer
#: reads one moment at a time rather than jumping around the episode.
GROUP_WINDOW = 15.0

#: Guaranteed slots, applied in this order before the ranking fills the rest.
#: The numbers are small on purpose: they are a floor that stops a category
#: disappearing, not a quota that shapes the whole queue.
RESERVED = (
    ("structural", 3),
    ("retention_risk", 3),
    ("uncertain", 4),
    ("risky_automatic", 4),
    ("setup_payoff", 2),
    ("refused", 2),
)

#: Share of the queue kept for decisions that look right. Rounded down, but
#: never to zero once the queue is big enough to spare a slot.
POSITIVE_RATIO = 0.15
POSITIVE_MIN_LIMIT = 6

#: A clip at or over this share of the runtime is a structural decision.
LONG_CLIP_SHARE = 0.08
#: ...and this many seconds is one regardless of the runtime.
LONG_CLIP_SECONDS = 25.0

#: Confidence at or below which a decision counts as uncertain. Session 8's
#: number, deliberately: an item that pass would flag for a human is exactly
#: the item this queue should be asking about.
UNCERTAIN_AT_OR_BELOW = 0.50

#: Confidence at or above which a decision is a candidate positive sample.
CONFIDENT_AT_OR_ABOVE = 0.75

#: How much each flag adds to a prompt's priority. Additive and clamped, so an
#: item that is uncertain *and* high impact *and* risky outranks one that is
#: only spectacular at a single thing -- which is the item most worth a human.
FLAG_BOOST = {
    "uncertain": 0.16,
    "high_impact": 0.14,
    "risky_automatic": 0.12,
    "structural": 0.18,
    "retention_risk": 0.14,
    "setup_payoff": 0.08,
    "refused": 0.06,
    "positive_sample": -0.20,   # real, and never the most urgent thing
}


# ---------------------------------------------------------------------------
# Building one prompt
# ---------------------------------------------------------------------------

#: Flags that mean "this might be wrong". A prompt carrying any of them is in
#: the queue *because* of that, and cannot also be an example of a good call --
#: see ``_settle_flags``.
DOUBT_FLAGS = frozenset({
    "uncertain", "risky_automatic", "refused", "retention_risk",
})


def _settle_flags(flags: Sequence[str]) -> list[str]:
    """Deduplicate, and stop a prompt claiming to be both good and doubtful.

    Without this a confidently placed sound effect comes out flagged
    ``risky_automatic`` *and* ``positive_sample``, which is two contradictory
    reasons to be asked about and makes ``--no-positive`` meaningless. Doubt
    wins: if there is any reason to think a decision is wrong, that is why the
    reviewer is looking at it.
    """
    out: list[str] = []
    for flag in flags:
        if flag and flag not in out:
            out.append(flag)
    if DOUBT_FLAGS & set(out) and "positive_sample" in out:
        out.remove("positive_sample")
    return out


def _prompt(
    *,
    source: str,
    target,
    question: str,
    why_asked: str,
    system_decision: str,
    system_confidence: float = 0.0,
    evidence: Sequence[str] = (),
    category: str = "preference",
    suggested_ratings: Sequence[str] = (),
    base_priority: float = 0.5,
    impact: str = "medium",
    flags: Sequence[str] = (),
) -> ReviewPrompt:
    clean_flags = _settle_flags(flags)
    priority = clamp01(
        base_priority + sum(FLAG_BOOST.get(flag, 0.0) for flag in clean_flags),
        0.5,
    )
    return ReviewPrompt(
        prompt_id=new_id("q", source, target.key(), question),
        source=source,
        target=target,
        question=question,
        why_asked=why_asked,
        system_decision=system_decision or "(the pass recorded no reason)",
        system_confidence=clamp01(system_confidence, 0.0),
        evidence=[str(line)[:300] for line in evidence if line],
        category=category if category in REASON_CATEGORIES else "preference",
        suggested_ratings=list(suggested_ratings),
        priority=priority,
        impact=impact,
        flags=clean_flags,
    )


def _uncertain(confidence: float) -> bool:
    return confidence <= UNCERTAIN_AT_OR_BELOW


def _confident(confidence: float) -> bool:
    return confidence >= CONFIDENT_AT_OR_ABOVE


def _has_risks(record: Any) -> bool:
    return bool(getattr(record, "risks", ()) or ())


# ---------------------------------------------------------------------------
# One generator per pass
# ---------------------------------------------------------------------------

def _from_roughcut(artifacts: Artifacts) -> list[ReviewPrompt]:
    """Clips that were kept, and recommendations the cut could not convert."""
    plan = artifacts.roughcut
    if plan is None:
        return []
    out: list[ReviewPrompt] = []
    runtime = max(1.0, float(getattr(plan, "total_duration", 0.0) or 0.0))

    placements = list(getattr(plan, "placements", ()) or ())
    # Longest first: a thirty-second clip is a bigger decision than a two-second
    # one, and on a long cut the pool has to stop somewhere.
    ranked = sorted(
        placements,
        key=lambda p: float(getattr(p, "sequence_duration", 0.0) or 0.0),
        reverse=True,
    )
    for placement in ranked[:PER_SOURCE_CANDIDATES]:
        target = targets_module.target_for(
            "roughcut_placement", placement, artifacts)
        duration = target.duration
        share = duration / runtime
        is_long = share >= LONG_CLIP_SHARE or duration >= LONG_CLIP_SECONDS
        protected = bool(getattr(placement, "protected", False))
        unexplained = not (getattr(placement, "recommendation_ids", ()) or ())

        flags = []
        if is_long or protected:
            flags.append("high_impact")
        if unexplained:
            flags.append("uncertain")
        if protected and not unexplained:
            flags.append("positive_sample")

        out.append(_prompt(
            source="roughcut",
            target=target,
            question=(
                f"This clip runs {duration:.1f}s "
                f"({share * 100:.0f}% of the cut). Right length, right place?"
            ),
            why_asked=(
                "a long clip is the decision a viewer notices first"
                if is_long else
                "kept with nothing recorded about why" if unexplained else
                "a clip the cut chose to keep"
            ),
            system_decision=(
                f"kept: {getattr(placement, 'keep_reason', '') or 'no reason recorded'}"
            ),
            system_confidence=0.5 if unexplained else 0.7,
            evidence=[
                f"source {getattr(placement, 'source_file', '')}",
                f"source in/out {getattr(placement, 'source_in', 0):.2f}"
                f"-{getattr(placement, 'source_out', 0):.2f}",
                f"speed {getattr(placement, 'speed', 1.0)}",
            ],
            category="pacing",
            suggested_ratings=["keep", "shorten", "cut", "boring", "good"],
            base_priority=0.45 + min(0.25, share),
            impact="high" if is_long else "medium",
            flags=flags,
        ))

    for missed in list(getattr(plan, "unconverted", ()) or ())[:8]:
        start = float(getattr(missed, "start", 0.0) or 0.0)
        target = targets_module.range_target(
            start, max(start, float(getattr(missed, "end", start) or start)),
            label=f"unconverted {getattr(missed, 'category', '?')}",
            name=artifacts.name,
        )
        target.source_ids = [
            str(getattr(missed, "recommendation_id", "") or "")]
        out.append(_prompt(
            source="roughcut",
            target=target,
            question="The cut could not act on this recommendation. Does that "
                     "matter?",
            why_asked="a proposal that was dropped for a mechanical reason, "
                      "not a creative one",
            system_decision=(
                f"{getattr(missed, 'category', '?')} not converted: "
                f"{getattr(missed, 'reason', '')}"
            ),
            system_confidence=0.4,
            category="technical",
            suggested_ratings=["okay", "bad", "unsure"],
            base_priority=0.40,
            impact="low",
            flags=["refused"],
        ))
    return out


def _from_recommendations(artifacts: Artifacts) -> list[ReviewPrompt]:
    """What the safety pass removed, and the holds it deliberately kept."""
    recommendations = artifacts.recommendations
    if recommendations is None:
        return []
    out: list[ReviewPrompt] = []

    removed = list(getattr(recommendations, "removed", lambda: [])())
    for record in removed[:PER_SOURCE_CANDIDATES]:
        target = targets_module.target_for(
            "recommendation", record, artifacts)
        out.append(_prompt(
            source="recommend",
            target=target,
            question=f"The safety pass {getattr(record, 'status', 'removed')} "
                     "this edit. Was it right to?",
            why_asked="a proposal that was rejected or softened -- the place "
                      "where the system is most likely to be over-cautious",
            system_decision=(
                f"{getattr(record, 'category', '?')} "
                f"{getattr(record, 'status', '?')}: "
                f"{getattr(record, 'status_reason', '') or 'no reason recorded'}"
            ),
            system_confidence=targets_module.confidence_of(record),
            evidence=list(
                getattr(getattr(record, "evidence", None),
                        "transcript_quotes", ()) or ())[:3],
            category="preference",
            suggested_ratings=["okay", "bad", "too_little", "unsure"],
            base_priority=0.42,
            impact="medium",
            flags=["refused"] + (
                ["uncertain"]
                if _uncertain(targets_module.confidence_of(record)) else []),
        ))

    holds = list(getattr(recommendations, "deliberate_holds", lambda: [])())
    for record in holds[:6]:
        target = targets_module.target_for("recommendation", record, artifacts)
        out.append(_prompt(
            source="recommend",
            target=target,
            question="The planner chose to leave this stretch alone. Agree?",
            why_asked="a deliberate hold is a decision, and the one nobody "
                      "ever reviews because nothing happened",
            system_decision=f"hold: {getattr(record, 'reason', '')}",
            system_confidence=targets_module.confidence_of(record),
            category="pacing",
            suggested_ratings=["keep", "good", "boring", "cut"],
            base_priority=0.38,
            impact="medium",
            flags=["positive_sample"],
        ))
    return out


def _from_critic(artifacts: Artifacts) -> list[ReviewPrompt]:
    """What the critic saw, and what the revision pass proposed doing."""
    out: list[ReviewPrompt] = []

    report = artifacts.critique
    if report is not None:
        findings = list(getattr(report, "findings", ()) or ())
        findings.sort(
            key=lambda f: (
                {"high": 0, "medium": 1, "low": 2}.get(
                    getattr(f, "severity", "low"), 2),
                -targets_module.confidence_of(f),
            )
        )
        for finding in findings[:PER_SOURCE_CANDIDATES]:
            confidence = targets_module.confidence_of(finding)
            severity = getattr(finding, "severity", "low")
            flags = []
            if severity == "high":
                flags.append("high_impact")
            if getattr(finding, "is_uncertain", False) or _uncertain(confidence):
                flags.append("uncertain")
            out.append(_prompt(
                source="critic",
                target=targets_module.target_for(
                    "critic_finding", finding, artifacts),
                question=f"The critic called this "
                         f"'{getattr(finding, 'issue', '?')}'. Is it?",
                why_asked="the critic judges single frames with no memory of "
                          "the episode, so this is where it is most likely to "
                          "be wrong",
                system_decision=(
                    f"{severity} {getattr(finding, 'issue', '?')}: "
                    f"{getattr(finding, 'evidence', '')}"
                ),
                system_confidence=confidence,
                evidence=[
                    f"frame {getattr(finding, 'frame_id', '')}",
                    str(getattr(finding, "suggested_fix", "") or ""),
                ],
                category="visual",
                suggested_ratings=["good", "bad", "confusing", "unsure"],
                base_priority=(
                    0.55 if severity == "high"
                    else 0.45 if severity == "medium" else 0.35
                ),
                impact="high" if severity == "high" else "medium",
                flags=flags,
            ))

    revisions = artifacts.revisions
    if revisions is not None:
        records = list(getattr(revisions, "ranked", lambda: [])())
        for revision in records[:PER_SOURCE_CANDIDATES]:
            confidence = targets_module.confidence_of(revision)
            status = getattr(revision, "status", "")
            flags = []
            if status == "accepted":
                flags.append("risky_automatic")
                flags.append("high_impact")
            if getattr(revision, "needs_human", False) or _uncertain(confidence):
                flags.append("uncertain")
            if status == "rejected":
                flags.append("refused")
            out.append(_prompt(
                source="critic",
                target=targets_module.target_for(
                    "revision_recommendation", revision, artifacts),
                question=(
                    f"The revision pass wants to "
                    f"'{getattr(revision, 'suggested_fix', '?')}' here. Right "
                    "call?"
                ),
                why_asked="a fix built on a critic finding: if the finding was "
                          "wrong the fix changes the edit for no reason",
                system_decision=(
                    f"{status}: {getattr(revision, 'fix_detail', '') or getattr(revision, 'issue', '')}"
                ),
                system_confidence=confidence,
                evidence=[
                    str(getattr(revision, "visual_evidence", "") or ""),
                    str(getattr(revision, "transcript_evidence", "") or ""),
                ],
                category="visual",
                suggested_ratings=["good", "bad", "wrong_moment", "unsure"],
                base_priority=0.48,
                impact="high" if status == "accepted" else "medium",
                flags=flags,
            ))
    return out


#: Which reason category a style item belongs to, by layer.
_STYLE_CATEGORY = {
    "caption": "caption", "title": "caption", "audio": "audio",
    "emphasis": "visual", "marker": "style", "polish": "style",
    "deferred": "style", "base": "pacing",
}


def _from_style(artifacts: Artifacts) -> list[ReviewPrompt]:
    """Captions, cards, zooms and the items the style pass declined to draw."""
    plan = artifacts.layers
    if plan is None:
        return []
    out: list[ReviewPrompt] = []

    items = [
        item for item in (getattr(plan, "items", ()) or ())
        if getattr(item, "layer", "") != "base"
    ]
    # Active items first: something that draws on screen matters more than a
    # marker nobody but the editor will ever see.
    items.sort(
        key=lambda item: (
            not getattr(item, "is_active", False),
            -float(getattr(item, "priority", 0.0) or 0.0),
        )
    )
    for item in items[:PER_SOURCE_CANDIDATES]:
        confidence = float(getattr(item, "priority", 0.0) or 0.0)
        status = getattr(item, "status", "planned")
        active = bool(getattr(item, "is_active", False))
        payload = getattr(item, "payload", {}) or {}
        copy = str(payload.get("text") or payload.get("title") or "")

        flags = []
        if active:
            flags.append("high_impact")
            # Everything the style pass plans was decided automatically, so
            # the flag only earns its place when something is *also* off:
            # a named risk, or a confidence the pass itself was unsure of.
            if _has_risks(item) or _uncertain(confidence):
                flags.append("risky_automatic")
        if status in ("deferred", "rejected"):
            flags.append("refused")
        if _uncertain(confidence):
            flags.append("uncertain")
        elif active and _confident(confidence) and not _has_risks(item):
            flags.append("positive_sample")

        kind = getattr(item, "kind", "?")
        is_text = bool(getattr(item, "is_text", False))
        question = (
            f'Caption "{copy[:50]}" -- right words, right moment?' if is_text and copy
            else f"A {kind} here. Does it earn its place?"
        )
        out.append(_prompt(
            source="style",
            target=targets_module.target_for("layer_item", item, artifacts),
            question=question,
            why_asked=(
                "this draws on screen, so a wrong one is the most visible "
                "mistake in the edit" if active else
                "the style pass declined this; that limit may be too tight"
                if status != "planned" else
                "a styling choice made from a preset rather than from this "
                "footage"
            ),
            system_decision=(
                f"{kind} [{status}]: "
                f"{getattr(item, 'reason', '') or 'no reason recorded'}"
                + (f" (risks: {', '.join(getattr(item, 'risks', ()))})"
                   if _has_risks(item) else "")
            ),
            system_confidence=confidence,
            evidence=([f'copy: "{copy}"'] if copy else []) + [
                f"layer {getattr(item, 'layer', '?')}, "
                f"effect {getattr(item, 'effect', '?')}",
            ],
            category=_STYLE_CATEGORY.get(getattr(item, "layer", ""), "style"),
            suggested_ratings=(
                ["good_caption", "bad_caption", "too_much", "wrong_moment"]
                if is_text else
                ["good", "bad", "too_much", "wrong_style"]
            ),
            base_priority=0.40 + (0.12 if active else 0.0),
            impact="high" if active else "low",
            flags=flags,
        ))
    return out


def _from_assets(artifacts: Artifacts) -> list[ReviewPrompt]:
    """Sounds that were placed, and placeholders nothing was good enough for."""
    plan = artifacts.asset_plan
    if plan is None:
        return []
    out: list[ReviewPrompt] = []

    placements = list(getattr(plan, "placements", ()) or ())
    placements.sort(key=lambda p: getattr(p, "status", "") != "placed")
    for placement in placements[:PER_SOURCE_CANDIDATES]:
        status = getattr(placement, "status", "marker_only")
        placed = status == "placed"
        best = getattr(placement, "best", None)
        score = float(getattr(best, "score", 0.0) or 0.0) if best else 0.0

        flags = []
        if placed:
            flags.append("high_impact")
            if _has_risks(placement) or not _confident(score):
                flags.append("risky_automatic")
            else:
                flags.append("positive_sample")
        else:
            flags.append("refused")
        if _uncertain(score) and placed:
            flags.append("uncertain")

        filename = getattr(placement, "asset_filename", "")
        out.append(_prompt(
            source="assets",
            target=targets_module.target_for(
                "asset_placement", placement, artifacts),
            question=(
                f"'{filename}' plays here. Right sound for this moment?"
                if placed else
                f"Nothing was placed for this "
                f"{getattr(placement, 'kind', '?')}. Is the silence better?"
            ),
            why_asked=(
                "a placed sound is heard by every viewer and matching is "
                "tags and folders, never listening" if placed else
                "four of the five placement outcomes place nothing; this is "
                "where the library is thin rather than the edit wrong"
            ),
            system_decision=(
                f"{status}: {getattr(placement, 'reason', '') or 'no reason recorded'}"
            ),
            system_confidence=score,
            evidence=[
                f"track {getattr(placement, 'track', '') or '(none)'}",
                f"match score {score:.2f}" if best else "no candidate scored",
            ],
            category="audio",
            suggested_ratings=(
                ["good_music_sfx", "bad_music_sfx", "too_much", "wrong_moment"]
                if placed else
                ["okay", "too_little", "unsure"]
            ),
            base_priority=0.42 + (0.10 if placed else 0.0),
            impact="high" if placed else "low",
            flags=flags,
        ))
    return out


def _from_episode(artifacts: Artifacts) -> list[ReviewPrompt]:
    """Beats the layer was unsure of, open loops, callbacks, setup/payoff."""
    memory = artifacts.memory
    if memory is None:
        return []
    out: list[ReviewPrompt] = []

    beats = list(getattr(memory, "beats", ()) or ())
    # Only the beats worth asking about: the ones it could not name, the ones
    # it was unsure of, and the peak. Asking about forty confident "travel"
    # beats is exactly the noise this queue exists to avoid.
    notable = [
        beat for beat in beats
        if getattr(beat, "kind", "") in ("unknown", "climax", "payoff",
                                         "reveal", "resolution")
        or _uncertain(targets_module.confidence_of(beat))
    ]
    for beat in notable[:16]:
        confidence = targets_module.confidence_of(beat)
        kind = getattr(beat, "kind", "unknown")
        flags = []
        if _uncertain(confidence) or kind == "unknown":
            flags.append("uncertain")
        if kind in ("climax", "payoff", "resolution"):
            flags.append("structural")
        out.append(_prompt(
            source="episode",
            target=targets_module.target_for("episode_beat", beat, artifacts),
            question=(
                "This stretch was read as the peak of the episode. Is it?"
                if kind == "climax" else
                "Nothing could be read off this stretch. What is it doing?"
                if kind == "unknown" else
                f"This was read as a '{kind}' beat. Right?"
            ),
            why_asked="the episode layer has never been checked against a "
                      "finished edit, so its beat labels are calibrated "
                      "against intuition",
            system_decision=f"{kind}: {getattr(beat, 'why', '')}",
            system_confidence=confidence,
            evidence=list(
                getattr(getattr(beat, "evidence", None), "quotes", ()) or ()
            )[:2] + [
                f"alternative reading: {getattr(beat, 'alternative', '') or 'none'}",
            ],
            category="story",
            suggested_ratings=["good", "bad", "confusing", "unsure"],
            base_priority=0.40,
            impact="medium",
            flags=flags,
        ))

    for loop in list(getattr(memory, "open_loops", ()) or ())[:12]:
        resolved = bool(getattr(loop, "resolved", False))
        confidence = targets_module.confidence_of(loop)
        flags = ["setup_payoff"]
        if not resolved:
            flags.append("high_impact")
        if _uncertain(confidence):
            flags.append("uncertain")
        out.append(_prompt(
            source="episode",
            target=targets_module.target_for("open_loop", loop, artifacts),
            question=(
                f'"{getattr(loop, "question", "")[:60]}" -- '
                + ("does the episode answer this?" if resolved
                   else "is this left hanging on purpose?")
            ),
            why_asked="a question the episode raises and never answers is the "
                      "kind of thing a viewer notices and an editor forgets",
            system_decision=(
                f"{getattr(loop, 'status', '?')}: "
                f"{getattr(loop, 'resolution_reason', '') or getattr(loop, 'why', '')}"
            ),
            system_confidence=confidence,
            category="story",
            suggested_ratings=["good", "bad", "confusing", "weak_payoff",
                               "unsure"],
            base_priority=0.44,
            impact="high" if not resolved else "medium",
            flags=flags,
        ))

    for callback in list(getattr(memory, "callbacks", ()) or ())[:10]:
        confidence = targets_module.confidence_of(callback)
        out.append(_prompt(
            source="episode",
            target=targets_module.target_for("callback", callback, artifacts),
            question="This refers back to something earlier. Worth calling "
                     "out in the edit?",
            why_asked="callbacks are cheap to add and easy to force; both "
                      "answers are useful",
            system_decision=(
                f"{getattr(callback, 'kind', '?')} callback to "
                f"{float(getattr(callback, 'refers_to_time', 0.0) or 0.0):.1f}s: "
                f"{getattr(callback, 'why', '')}"
            ),
            system_confidence=confidence,
            evidence=[str(getattr(callback, "suggested_text", "") or "")],
            category="story",
            suggested_ratings=["good_callback", "forced_callback", "unsure"],
            base_priority=0.40,
            impact="medium",
            flags=["setup_payoff"] + (
                ["uncertain"] if _uncertain(confidence) else []),
        ))

    for setup in list(getattr(memory, "setups", ()) or ())[:10]:
        if not getattr(setup, "payoff_id", ""):
            continue
        confidence = targets_module.confidence_of(setup)
        target = targets_module.target_for("episode_beat", setup, artifacts)
        out.append(_prompt(
            source="episode",
            target=target,
            question="This sets something up that pays off later. Does the "
                     "pair land?",
            why_asked="a setup and its payoff are one decision in two places, "
                      "and neither reads correctly on its own",
            system_decision=(
                f"setup '{getattr(setup, 'text', '')[:60]}' -> payoff "
                f"{getattr(setup, 'payoff_id', '')}"
            ),
            system_confidence=confidence,
            category="story",
            suggested_ratings=["strong_payoff", "weak_payoff", "good", "unsure"],
            base_priority=0.42,
            impact="medium",
            flags=["setup_payoff"],
        ))
    return out


def _from_retention(artifacts: Artifacts) -> list[ReviewPrompt]:
    """Risk zones, hook candidates, the peak, the ending, and the suggestions."""
    plan = artifacts.retention
    if plan is None:
        return []
    out: list[ReviewPrompt] = []

    risks = list(getattr(plan, "risks", ()) or ())
    risks.sort(
        key=lambda r: {"high": 0, "medium": 1, "low": 2}.get(
            getattr(r, "severity", "low"), 2)
    )
    for risk in risks[:16]:
        severity = getattr(risk, "severity", "low")
        confidence = targets_module.confidence_of(risk)
        flags = ["retention_risk"]
        if severity == "high":
            flags.append("high_impact")
        if _uncertain(confidence):
            flags.append("uncertain")
        if getattr(risk, "fix_is_safe_automatically", False):
            flags.append("risky_automatic")
        out.append(_prompt(
            source="retention",
            target=targets_module.target_for(
                "retention_suggestion", risk, artifacts),
            question=f"This was flagged as '{getattr(risk, 'risk', '?')}'. "
                     "Does it actually drag?",
            why_asked="a creative risk read off edit evidence -- nothing here "
                      "has seen an audience, so a human is the only check",
            system_decision=(
                f"{severity} {getattr(risk, 'risk', '?')}: "
                f"{getattr(risk, 'why', '')}"
            ),
            system_confidence=confidence,
            evidence=[
                f"suggested fix: {getattr(risk, 'suggested_fix', '') or 'none'}",
                f"safe to apply automatically: "
                f"{bool(getattr(risk, 'fix_is_safe_automatically', False))}",
            ],
            category="retention",
            suggested_ratings=["boring", "okay", "shorten", "keep", "unsure"],
            base_priority=(
                0.58 if severity == "high"
                else 0.46 if severity == "medium" else 0.36
            ),
            impact="high" if severity == "high" else "medium",
            flags=flags,
        ))

    for hook in list(getattr(plan, "hooks", ()) or ())[:6]:
        confidence = targets_module.confidence_of(hook)
        out.append(_prompt(
            source="retention",
            target=targets_module.target_for(
                "hook_candidate", hook, artifacts),
            question="Would this moment open the video?",
            why_asked="the opening is the single highest-leverage decision in "
                      "the edit, and this layer is guessing at it",
            system_decision=(
                f"{getattr(hook, 'hook_type', '?')} hook, score "
                f"{float(getattr(hook, 'score', 0.0) or 0.0):.2f}: "
                f"{getattr(hook, 'suggested_text', '') or getattr(hook, 'why', '')}"
            ),
            system_confidence=confidence,
            evidence=[
                f"text source: {getattr(hook, 'text_source', '?')}",
                f"viewer question: {getattr(hook, 'viewer_question', '') or 'none'}",
            ],
            category="retention",
            suggested_ratings=["good_hook", "bad_hook", "hype", "unsure"],
            base_priority=0.55,
            impact="high",
            flags=["structural", "high_impact"] + (
                ["uncertain"] if _uncertain(confidence) else []),
        ))

    for label, candidate, question in (
        ("climax", getattr(plan, "climax", None),
         "This was picked as the peak of the episode. Is it?"),
        ("ending", getattr(plan, "ending", None),
         "This was picked as the ending. Does it land?"),
    ):
        if candidate is None:
            continue
        confidence = targets_module.confidence_of(candidate)
        # Both are "a moment proposed for a structural job", which is what the
        # ``hook_candidate`` collection covers -- see ``targets._COLLECTIONS``.
        target = targets_module.target_for(
            "hook_candidate", candidate, artifacts)
        target.label = f"{label} candidate: {target.label}"
        out.append(_prompt(
            source="retention",
            target=target,
            question=question,
            why_asked=f"the {label} is one of three moments that decide the "
                      "shape of the whole episode",
            system_decision=f"{label}: {getattr(candidate, 'why', '')}",
            system_confidence=confidence,
            category="story",
            suggested_ratings=(
                ["strong_payoff", "weak_payoff", "good", "unsure"]
            ),
            base_priority=0.56,
            impact="high",
            flags=["structural", "high_impact"] + (
                ["uncertain"] if _uncertain(confidence) else []),
        ))

    for suggestion in list(getattr(plan, "suggestions", ()) or ())[:16]:
        confidence = targets_module.confidence_of(suggestion)
        auto = bool(getattr(suggestion, "auto_safe", False))
        flags = []
        if auto:
            flags.append("risky_automatic")
        if getattr(suggestion, "needs_human_review", False) or _uncertain(confidence):
            flags.append("uncertain")
        out.append(_prompt(
            source="retention",
            target=targets_module.target_for(
                "retention_suggestion", suggestion, artifacts),
            question=(
                f"A later pass would '{getattr(suggestion, 'type', '?')}' here. "
                "Do you want that?"
            ),
            why_asked=(
                "this is marked safe to apply without a human, so a wrong one "
                "would go in unreviewed" if auto else
                "a suggestion waiting on a person before anything acts on it"
            ),
            system_decision=(
                f"{getattr(suggestion, 'type', '?')} -> "
                f"{getattr(suggestion, 'downstream', '?')}: "
                f"{getattr(suggestion, 'reason', '')}"
            ),
            system_confidence=confidence,
            category="retention",
            suggested_ratings=["good", "bad", "too_much", "unsure"],
            base_priority=0.44 + (0.06 if auto else 0.0),
            impact="medium",
            flags=flags,
        ))
    return out


def _whole_edit(artifacts: Artifacts) -> list[ReviewPrompt]:
    """One prompt about the thing as a whole. Always last, always present."""
    target = targets_module.whole_edit_target()
    target.end = artifacts.duration
    missing = artifacts.missing
    return [_prompt(
        source="edit",
        target=target,
        question="Overall: would you publish this cut?",
        why_asked="a verdict on the whole is the one piece of feedback that "
                  "cannot be reconstructed from the parts",
        system_decision=(
            f"{artifacts.duration:.0f}s cut on "
            f"'{artifacts.sequence_name or 'no sequence'}'"
            + (f", built without: {', '.join(missing)}" if missing else "")
        ),
        system_confidence=0.0,
        category="preference",
        suggested_ratings=["good", "okay", "bad", "confusing", "unsure"],
        base_priority=0.50,
        impact="high",
        flags=["structural"],
    )]


GENERATORS = (
    _from_roughcut,
    _from_recommendations,
    _from_critic,
    _from_style,
    _from_assets,
    _from_episode,
    _from_retention,
    _whole_edit,
)


# ---------------------------------------------------------------------------
# Dedupe, collapse, group
# ---------------------------------------------------------------------------

def dedupe(prompts: list[ReviewPrompt]) -> list[ReviewPrompt]:
    """Drop prompts about a target another prompt already covers.

    Keyed on the *target*, not the question: two generators asking different
    questions about one caption is still one caption to look at, and the higher
    priority question is the one worth the reviewer's attention.
    """
    best: dict[str, ReviewPrompt] = {}
    order: list[str] = []
    for prompt in prompts:
        key = prompt.target.key()
        current = best.get(key)
        if current is None:
            best[key] = prompt
            order.append(key)
            continue
        keeper, loser = (
            (prompt, current) if prompt.priority > current.priority
            else (current, prompt)
        )
        keeper.duplicates += 1 + loser.duplicates
        for flag in loser.flags:
            if flag not in keeper.flags and flag != "positive_sample":
                keeper.flags.append(flag)
        best[key] = keeper
    return [best[key] for key in order]


def collapse_nearby(
    prompts: list[ReviewPrompt], *, window: float = NEAR_WINDOW
) -> list[ReviewPrompt]:
    """Fold near-identical prompts that sit within ``window`` of each other.

    "Near-identical" is the same source, the same target type and the same
    category -- three captions in eight seconds, say. Two *different* kinds of
    thing at the same moment are left alone; that is a busy moment worth
    seeing, not a duplicate.
    """
    buckets: dict[tuple, list[ReviewPrompt]] = {}
    order: list[tuple] = []
    for prompt in sorted(prompts, key=lambda p: (p.start, p.prompt_id)):
        key = (prompt.source, prompt.target.target_type, prompt.category)
        bucket = buckets.setdefault(key, [])
        if key not in order:
            order.append(key)
        bucket.append(prompt)

    out: list[ReviewPrompt] = []
    for key in order:
        run: list[ReviewPrompt] = []
        for prompt in buckets[key]:
            if run and prompt.start - run[0].start <= window:
                run.append(prompt)
                continue
            if run:
                out.append(_fold(run))
            run = [prompt]
        if run:
            out.append(_fold(run))
    return out


def _fold(run: list[ReviewPrompt]) -> ReviewPrompt:
    """The best prompt of a run, carrying the others' flags and count."""
    if len(run) == 1:
        return run[0]
    keeper = max(run, key=lambda p: (p.priority, -p.start))
    keeper.duplicates += len(run) - 1
    for prompt in run:
        if prompt is keeper:
            continue
        for flag in prompt.flags:
            # A positive sample folded into a problem is not a positive
            # sample any more; the problem is the reason to look.
            if flag not in keeper.flags and flag != "positive_sample":
                keeper.flags.append(flag)
    if "positive_sample" in keeper.flags and len(keeper.flags) > 1:
        keeper.flags.remove("positive_sample")
    return keeper


def assign_groups(
    prompts: list[ReviewPrompt], *, window: float = GROUP_WINDOW
) -> list[ReviewPrompt]:
    """Give prompts in the same timeline neighbourhood a shared ``group_id``.

    Walked rather than bucketed by division: fixed buckets put two prompts one
    second apart in different groups whenever they straddle a boundary, which
    is the one case grouping exists to handle.
    """
    ordered = sorted(prompts, key=lambda p: (p.start, p.prompt_id))
    anchor: Optional[float] = None
    group_id = ""
    for prompt in ordered:
        if anchor is None or prompt.start - anchor > window:
            anchor = prompt.start
            group_id = f"g{int(anchor):06d}"
        prompt.group_id = group_id
    return prompts


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def select(
    candidates: list[ReviewPrompt], *, limit: int, include_positive: bool = True
) -> list[ReviewPrompt]:
    """The queue itself: reserved slots first, then the ranking, then order.

    Returned in group order -- the groups sorted by their best member -- so the
    queue reads as a walk through the episode's interesting moments rather than
    as a scoreboard.
    """
    limit = max(0, min(int(limit), MAX_LIMIT))
    if limit <= 0 or not candidates:
        return []

    ranked = sorted(candidates, key=lambda p: p.rank())
    if not include_positive:
        # Dropped from the pool rather than merely un-reserved: the fill pass
        # below walks the whole ranking, so filtering only the reserved pass
        # would let them back in through the side door.
        ranked = [p for p in ranked if not p.is_positive_sample]
    chosen: list[ReviewPrompt] = []
    taken: set[str] = set()

    def take(prompt: ReviewPrompt) -> bool:
        if prompt.prompt_id in taken or len(chosen) >= limit:
            return False
        taken.add(prompt.prompt_id)
        chosen.append(prompt)
        return True

    positives = [p for p in ranked if p.is_positive_sample]
    problems = [p for p in ranked if not p.is_positive_sample]

    for flag, quota in RESERVED:
        filled = 0
        for prompt in problems:
            if filled >= quota or len(chosen) >= limit:
                break
            if prompt.has_flag(flag) and take(prompt):
                filled += 1

    if include_positive and limit >= POSITIVE_MIN_LIMIT:
        wanted = max(1, int(limit * POSITIVE_RATIO))
        # Positive samples take from the *end* of the queue's budget, so they
        # can never crowd out a reserved category above.
        room = max(0, limit - len(chosen))
        for prompt in positives[:min(wanted, room)]:
            take(prompt)

    for prompt in ranked:
        if len(chosen) >= limit:
            break
        take(prompt)

    return _in_group_order(chosen)


def _in_group_order(prompts: list[ReviewPrompt]) -> list[ReviewPrompt]:
    groups: dict[str, list[ReviewPrompt]] = {}
    for prompt in prompts:
        groups.setdefault(prompt.group_id or prompt.prompt_id, []).append(prompt)
    ordered_groups = sorted(
        groups.values(), key=lambda members: min(p.rank() for p in members)
    )
    out: list[ReviewPrompt] = []
    for members in ordered_groups:
        out.extend(sorted(members, key=lambda p: p.rank()))
    return out


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------

def candidates_for(artifacts: Artifacts) -> list[ReviewPrompt]:
    """Every prompt every generator can produce, before any selection."""
    out: list[ReviewPrompt] = []
    for generator in GENERATORS:
        try:
            out.extend(generator(artifacts))
        except Exception as error:  # noqa: BLE001
            # One malformed artifact must not cost the whole review. The
            # failure is recorded on the queue rather than raised.
            artifacts.warnings.append(
                f"{generator.__name__} produced nothing: {error}"
            )
    return out


def build(
    artifacts: Artifacts,
    *,
    session_id: str = "",
    run_id: str = "",
    limit: int = DEFAULT_LIMIT,
    categories: Iterable[str] = (),
    sources: Iterable[str] = (),
    include_positive: bool = True,
) -> ReviewQueue:
    """Gather, dedupe, collapse, group, select. Reads JSON and nothing else."""
    wanted_categories = coerce_many(list(categories), REASON_CATEGORIES, limit=14)
    wanted_sources = coerce_many(list(sources), PROMPT_SOURCES, limit=8)

    raw = candidates_for(artifacts)
    if wanted_categories:
        raw = [p for p in raw if p.category in wanted_categories]
    if wanted_sources:
        raw = [p for p in raw if p.source in wanted_sources]

    pooled = collapse_nearby(dedupe(raw))
    assign_groups(pooled)
    chosen = select(pooled, limit=limit, include_positive=include_positive)

    queue = ReviewQueue(
        queue_id=new_id("rq", session_id or run_id, artifacts.name,
                        len(pooled), limit),
        session_id=session_id,
        run_id=run_id,
        name=artifacts.name,
        sequence_name=artifacts.sequence_name,
        timebase=artifacts.timebase,
        duration=artifacts.duration,
        prompts=chosen,
        candidates=len(pooled),
        limit=limit,
        filters={
            "categories": wanted_categories,
            "sources": wanted_sources,
            "include_positive": bool(include_positive),
        },
        sources=artifacts.sources,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    _add_warnings(queue, artifacts, pooled)
    return queue


def _add_warnings(
    queue: ReviewQueue, artifacts: Artifacts, pooled: list[ReviewPrompt]
) -> None:
    """Say what the queue could not ask about, in the queue itself.

    A reviewer who does not know the critic never ran will read a queue with no
    critic prompts as "the critic had no complaints" rather than "the critic
    was never asked".
    """
    queue.warnings.extend(artifacts.warnings)

    dropped = len(pooled) - len(queue.prompts)
    if dropped > 0:
        queue.warnings.append(
            f"{dropped} more item(s) were worth reviewing and did not fit in "
            f"a queue of {queue.limit}; raise --limit to see them"
        )
    missing = artifacts.missing
    if missing:
        queue.warnings.append(
            "nothing in this queue comes from: " + ", ".join(missing)
            + " -- those passes had not run, so their decisions are absent "
              "rather than approved"
        )
    if artifacts.timebase == "timeline":
        queue.warnings.append(
            "these times are the synthetic timeline ordering, not sequence "
            "time: no Premiere sequence looks like this, so feedback on a "
            "range has to be read through segment_ids"
        )
    if artifacts.is_empty:
        queue.warnings.append(
            "no artifacts were found for this timeline, so the only question "
            "here is about the edit as a whole; build a rough cut first"
        )
    elif not queue.prompts:
        queue.warnings.append(
            "nothing was worth queueing, which almost always means the "
            "filters excluded everything rather than that the edit is perfect"
        )
    if not any(p.is_positive_sample for p in queue.prompts) and queue.prompts:
        queue.warnings.append(
            "no decision looked confident enough to include as a positive "
            "sample, so this queue is entirely problems; feedback built only "
            "from problems teaches what to avoid and nothing about what to keep"
        )


def estimate(artifacts: Artifacts) -> dict:
    """How much is worth reviewing, without creating a session.

    Used by the auto report, which should be able to say "there are 34 things
    worth looking at" without starting a review nobody asked for.
    """
    pooled = collapse_nearby(dedupe(candidates_for(artifacts)))
    assign_groups(pooled)
    by_flag: dict[str, int] = {}
    for prompt in pooled:
        for flag in prompt.flags:
            by_flag[flag] = by_flag.get(flag, 0) + 1
    return {
        "worth_reviewing": len(pooled),
        "suggested_limit": min(DEFAULT_LIMIT, len(pooled)),
        "high_impact": by_flag.get("high_impact", 0),
        "uncertain": by_flag.get("uncertain", 0),
        "risky_automatic": by_flag.get("risky_automatic", 0),
        "structural": by_flag.get("structural", 0),
        "retention_risk": by_flag.get("retention_risk", 0),
        "by_flag": by_flag,
        "basis": NOT_MEASURED,
    }
