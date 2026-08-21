"""Turning what someone typed into a feedback item, and appending it.

The CLI is thin over this module, which is where the three awkward parts of
collecting feedback actually live:

* **What did they point at?** ``resolve_ident`` takes one string and tries, in
  order: the whole edit, a time range, a queue prompt, an earlier rating, and
  finally any record ID in any artifact. A reviewer copying an ID out of the
  queue should never have to also tell the tool which pass produced it.
* **What if it points at nothing?** Then the feedback is still kept -- with
  ``resolved`` false and a follow-up flag -- unless the caller asked for
  strictness, in which case the error names every collection that was searched
  and how many records were in each. The usual cause of a miss is not a typo;
  it is that the pass which would have produced the record never ran, and
  "unknown ID" alone would never tell you that.
* **What if they change their mind?** Nothing is edited. A second rating of the
  same target appends a new item carrying ``supersedes``, and both stay in the
  log. ``feedback list`` shows what stands; ``feedback show --history`` shows
  how it got there.

Notes and corrections go through the same path as ratings, because a note that
lands somewhere other than where the ratings land is a note nobody will read
next to the thing it was about.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from editing.config import EditingConfig
from editing.errors import EditingError
from editing.feedback import store as store_module
from editing.feedback import targets as targets_module
from editing.feedback.schema import (
    FeedbackCorrection, FeedbackItem, FeedbackRating, FeedbackReason,
    FeedbackSession, FeedbackTarget, RATINGS, ReviewPrompt, build_correction,
    coerce_many, coerce_one, default_reason_for, now, reasons_from,
)
from editing.feedback.targets import Artifacts
from editing.schema import clamp01

#: What ``feedback correct`` should call the rating when there was no rating
#: yet. Correcting something you liked is rare enough that "bad" is the honest
#: default for an unmapped action, and every mapped one is more specific.
RATING_FOR_ACTION = {
    "cut": "cut",
    "remove": "cut",
    "shorten": "shorten",
    "extend": "extend",
    "move_earlier": "move_earlier",
    "move_later": "move_later",
    "retime": "bad_pacing",
    "replace": "bad",
    "restyle": "wrong_style",
    "change_text": "bad_caption",
    "add": "too_little",
    "reorder": "wrong_moment",
    "other": "bad",
    "none": "okay",
}

#: Words that mean "the whole thing" on the command line.
WHOLE_EDIT_WORDS = frozenset({"whole", "whole_edit", "edit", "all", "overall"})

_RANGE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:-|\.\.|to)\s*(\d+(?:\.\d+)?)\s*$"
)


# ---------------------------------------------------------------------------
# Starting a session
# ---------------------------------------------------------------------------

def start_session(
    config: EditingConfig,
    artifacts: Artifacts,
    *,
    run_id: str = "",
    session_id: str = "",
    title: str = "",
    notes: str = "",
    force: bool = False,
) -> FeedbackSession:
    """Create the session folder and record what there was to review.

    ``sources`` is captured at start rather than derived at report time
    on purpose: re-running the style pass tomorrow must not retroactively make
    yesterday's review look like it had seen the new layers.
    """
    session = FeedbackSession(
        session_id=session_id or store_module.session_id_for(
            run_id=run_id or artifacts.run_id, name=artifacts.name),
        created_at=now(),
        status="open",
        run_id=run_id or artifacts.run_id,
        name=artifacts.name,
        sequence_name=artifacts.sequence_name,
        timebase=artifacts.timebase,
        duration=artifacts.duration,
        style=artifacts.style,
        artifact_root=artifacts.artifact_root,
        sources=artifacts.sources,
        title=title,
        notes=notes,
        counts={"items": 0},
    )
    if artifacts.is_empty:
        session.warnings.append(
            "no artifacts were found for this timeline, so there is nothing "
            "to review yet; build a rough cut first"
        )
    missing = artifacts.missing
    if missing:
        session.warnings.append(
            "reviewed without: " + ", ".join(missing)
            + " -- decisions from those passes are absent from this session "
              "rather than approved by it"
        )
    session.warnings.extend(artifacts.warnings)

    store_module.create(config, session, force=force)
    return session


def close_session(
    config: EditingConfig, session: FeedbackSession
) -> FeedbackSession:
    session.status = "closed"
    store_module.save_session(config, session)
    return session


def refresh_counts(
    config: EditingConfig, session: FeedbackSession
) -> FeedbackSession:
    """Recompute the session's counters from the log and save them.

    Derived from the log every time rather than incremented, so a count can
    never drift from the file it describes.
    """
    history = store_module.read_all(config, session.session_id)
    current = store_module.current_of(history)
    session.counts = counts_of(history, current)
    store_module.save_session(config, session)
    return session


def counts_of(
    history: Sequence[FeedbackItem], current: Sequence[FeedbackItem]
) -> dict:
    by_polarity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_target: dict[str, int] = {}
    for item in current:
        by_polarity[item.polarity] = by_polarity.get(item.polarity, 0) + 1
        for category in item.categories:
            by_category[category] = by_category.get(category, 0) + 1
        key = item.target.target_type
        by_target[key] = by_target.get(key, 0) + 1
    return {
        "items": len(current),
        "history": len(history),
        "superseded": max(0, len(history) - len(current)),
        "with_correction": sum(1 for i in current if i.has_correction),
        "with_note": sum(1 for i in current if i.note),
        "usable_for_training": sum(
            1 for i in current if i.usable_for_training),
        "needs_follow_up": sum(1 for i in current if i.needs_follow_up),
        "unresolved_targets": sum(
            1 for i in current
            if i.target.is_identified and i.target.checked
            and not i.target.resolved
        ),
        "by_polarity": by_polarity,
        "by_category": by_category,
        "by_target_type": by_target,
    }


# ---------------------------------------------------------------------------
# Working out what was pointed at
# ---------------------------------------------------------------------------

def parse_range(text: str) -> Optional[tuple[float, float]]:
    """``"12.5-30"``, ``"12.5..30"`` or ``"12.5 to 30"`` as a pair of seconds."""
    match = _RANGE.match(str(text or ""))
    if not match:
        return None
    start, end = float(match.group(1)), float(match.group(2))
    return (start, max(start, end))


def resolve_ident(
    config: EditingConfig,
    session: FeedbackSession,
    artifacts: Artifacts,
    ident: str,
    *,
    target_type: str = "",
    strict: bool = True,
) -> tuple[FeedbackTarget, Optional[ReviewPrompt], Optional[FeedbackItem]]:
    """What one identifier on the command line refers to.

    Returns ``(target, prompt, previous)``. ``prompt`` is the queue question
    being answered when there is one, and ``previous`` is the rating this one
    will supersede.

    The order matters: a queue prompt is checked before an artifact ID because
    a prompt carries the *question*, and feedback that remembers what it was
    asked is worth more than feedback that only remembers what it was about.
    """
    text = str(ident or "").strip()
    if not text:
        raise EditingError(
            "Nothing to give feedback on",
            hint="Pass a prompt ID from `feedback queue`, a record ID, a "
                 "range like 120-155, or the word 'whole'.",
        )

    if text.lower() in WHOLE_EDIT_WORDS:
        target = targets_module.whole_edit_target()
        target.end = artifacts.duration
        return target, None, _previous_for(config, session, target)

    span = parse_range(text)
    if span is not None:
        target = targets_module.range_target(
            span[0], span[1], name=artifacts.name)
        return target, None, _previous_for(config, session, target)

    prompt = store_module.find_prompt(config, session.session_id, text)
    if prompt is not None:
        target = FeedbackTarget.from_dict(prompt.target.to_dict())
        # The queue was built from artifacts that may have been rebuilt since.
        # Re-resolving keeps a stale prompt honest rather than asserting the
        # record is still there.
        if target.is_identified and target.target_id:
            fresh = targets_module.resolve(
                artifacts, target.target_id,
                target_type=target.target_type)
            if fresh is not None:
                target = fresh
            else:
                target.resolved = False
                target.resolution_note = (
                    "this record was in the queue but is not in the artifacts "
                    "now; the pass has probably been re-run since"
                )
        return target, prompt, _previous_for(config, session, target)

    history = store_module.read_all(config, session.session_id)
    earlier = store_module.find_item(history, text)
    if earlier is not None:
        return earlier.target, None, earlier

    if strict:
        target = targets_module.require(
            artifacts, text, target_type=target_type)
    else:
        found = targets_module.resolve(
            artifacts, text, target_type=target_type)
        target = found if found is not None else targets_module.unresolved_target(
            text, artifacts, target_type=target_type)
    return target, None, _previous_for(config, session, target)


def _previous_for(
    config: EditingConfig, session: FeedbackSession, target: FeedbackTarget
) -> Optional[FeedbackItem]:
    """The rating that currently stands on this target, if any."""
    current = store_module.read_current(config, session.session_id)
    key = target.key()
    for item in reversed(current):
        if item.target.key() == key:
            return item
    return None


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def rate(
    config: EditingConfig,
    session: FeedbackSession,
    artifacts: Artifacts,
    ident: str,
    rating: str,
    *,
    reasons: Iterable[str] = (),
    note: str = "",
    correction: str = "",
    correction_action: str = "",
    correction_seconds: Optional[float] = None,
    priority: Optional[float] = None,
    confidence: Optional[float] = None,
    strength: Optional[float] = None,
    target_type: str = "",
    usable_for_training: Optional[bool] = None,
    needs_follow_up: bool = False,
    strict: bool = True,
) -> FeedbackItem:
    """Record one rating. Appends; never edits.

    An unknown rating is refused rather than coerced to ``okay``: the whole
    vocabulary exists so a later session can count ratings, and a silent
    fallback would put a typo in the data as a real opinion.
    """
    chosen = coerce_one(rating, RATINGS, "")
    if not chosen:
        raise EditingError(
            f"'{rating}' is not a rating this collector accepts",
            hint="Ratings: " + ", ".join(RATINGS),
            detail={"given": rating, "allowed": list(RATINGS)},
        )

    target, prompt, previous = resolve_ident(
        config, session, artifacts, ident,
        target_type=target_type, strict=strict,
    )

    given = reasons_from(list(reasons))[:6]
    if not given:
        given = [FeedbackReason(
            category=(prompt.category if prompt is not None
                      else default_reason_for(chosen))
        )]

    item = FeedbackItem(
        created_at=now(),
        session_id=session.session_id,
        run_id=session.run_id,
        prompt_id=prompt.prompt_id if prompt is not None else "",
        target=target,
        rating=FeedbackRating(
            rating=chosen,
            strength=clamp01(strength, 0.5) if strength is not None else 0.5,
        ),
        reasons=given,
        note=str(note or "").strip()[:2000],
        correction=build_correction(
            correction, action=correction_action, seconds=correction_seconds,
        ),
        priority=(
            clamp01(priority, 0.5) if priority is not None
            else (prompt.priority if prompt is not None else 0.5)
        ),
        confidence=clamp01(confidence, 0.7) if confidence is not None else 0.7,
        needs_follow_up=bool(needs_follow_up),
        supersedes=previous.feedback_id if previous is not None else "",
    )
    if usable_for_training is not None:
        item.usable_for_training = bool(usable_for_training)
        if not usable_for_training:
            item.training_note = "excluded by the reviewer"

    _carry_prompt_context(item, prompt)
    return store_module.append(config, session.session_id, item)


def add_note(
    config: EditingConfig,
    session: FeedbackSession,
    artifacts: Artifacts,
    ident: str,
    text: str,
    *,
    reasons: Iterable[str] = (),
    target_type: str = "",
    strict: bool = True,
) -> FeedbackItem:
    """Attach an observation with no verdict attached to it.

    A note keeps whatever rating already stands on the target, and when there
    is none it records ``okay`` and marks itself unusable for training. That
    second part matters: a note is a sentence about a moment, and treating it
    as a neutral *judgement* of the decision would put a label in the dataset
    that the reviewer never gave.
    """
    body = str(text or "").strip()
    if not body:
        raise EditingError(
            "A note needs some text",
            hint='For example: feedback note <id> "the music is too loud here"',
        )
    target, prompt, previous = resolve_ident(
        config, session, artifacts, ident,
        target_type=target_type, strict=strict,
    )

    if previous is not None:
        item = _extend(previous, note=body)
    else:
        given = reasons_from(list(reasons))[:6] or [FeedbackReason(
            category=(prompt.category if prompt is not None else "preference")
        )]
        item = FeedbackItem(
            created_at=now(),
            session_id=session.session_id,
            run_id=session.run_id,
            prompt_id=prompt.prompt_id if prompt is not None else "",
            target=target,
            rating=FeedbackRating(rating="okay"),
            reasons=given,
            note=body,
            usable_for_training=False,
            training_note=(
                "recorded as a note rather than a rating: there is no "
                "judgement of the decision here to learn from"
            ),
        )
        _carry_prompt_context(item, prompt)
    return store_module.append(config, session.session_id, item)


def add_correction(
    config: EditingConfig,
    session: FeedbackSession,
    artifacts: Artifacts,
    ident: str,
    text: str,
    *,
    action: str = "",
    seconds: Optional[float] = None,
    start: float = 0.0,
    end: float = 0.0,
    target_type: str = "",
    strict: bool = True,
) -> FeedbackItem:
    """Record what the editor would have done instead.

    Keeps an existing rating when there is one, because "shorten this" after
    "bad" is more information than "bad", and replacing the rating would throw
    the first half away.
    """
    body = str(text or "").strip()
    if not body and not action:
        raise EditingError(
            "A correction needs text or an --action",
            hint='For example: feedback correct <id> "cut this shorter"',
        )
    target, prompt, previous = resolve_ident(
        config, session, artifacts, ident,
        target_type=target_type, strict=strict,
    )
    correction = build_correction(
        body, action=action, seconds=seconds, start=start, end=end)

    if previous is not None:
        item = _extend(previous, correction=correction)
    else:
        chosen = RATING_FOR_ACTION.get(
            correction.action if correction else "other", "bad")
        item = FeedbackItem(
            created_at=now(),
            session_id=session.session_id,
            run_id=session.run_id,
            prompt_id=prompt.prompt_id if prompt is not None else "",
            target=target,
            rating=FeedbackRating(rating=chosen),
            reasons=[FeedbackReason(
                category=(prompt.category if prompt is not None
                          else default_reason_for(chosen))
            )],
            correction=correction,
        )
        _carry_prompt_context(item, prompt)
    return store_module.append(config, session.session_id, item)


def _extend(
    previous: FeedbackItem,
    *,
    note: str = "",
    correction: Optional[FeedbackCorrection] = None,
) -> FeedbackItem:
    """A new item that supersedes ``previous`` with one thing added.

    A copy rather than a mutation, because the old item is already in the log
    and the log is not rewritten. Both remain, and ``supersedes`` says which
    order they happened in.
    """
    item = FeedbackItem.from_dict(previous.to_dict())
    item.feedback_id = ""       # recomputed by settle for the new content
    item.created_at = now()
    item.supersedes = previous.feedback_id
    item.summary = ""
    if note:
        item.note = (f"{item.note}\n{note}" if item.note else note)[:2000]
    if correction is not None:
        item.correction = correction
        # A correction answers the question a directional rating leaves open,
        # so the follow-up flag it set is cleared here rather than in settle.
        item.needs_follow_up = False
        item.follow_up_note = ""
    return item


def _carry_prompt_context(
    item: FeedbackItem, prompt: Optional[ReviewPrompt]
) -> None:
    """Copy the queue's context onto the item so it reads standalone."""
    if prompt is None:
        return
    for artifact in (prompt.target.artifact,):
        if artifact and artifact not in item.source_artifacts:
            item.source_artifacts.append(artifact)
    if not item.target.label:
        item.target.label = prompt.target.label


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

def filtered(
    items: Sequence[FeedbackItem],
    *,
    ratings: Iterable[str] = (),
    categories: Iterable[str] = (),
    target_types: Iterable[str] = (),
    polarity: str = "",
    needs_follow_up: bool = False,
    training_only: bool = False,
) -> list[FeedbackItem]:
    wanted_ratings = set(coerce_many(list(ratings), RATINGS, limit=40))
    # Coerced the same way ``--reason`` is, so filtering by "boring" finds the
    # pacing items that word produced rather than silently matching nothing.
    wanted_categories = {
        reason.category for reason in reasons_from(list(categories))
    }
    wanted_targets = {str(t) for t in target_types if t}

    out = []
    for item in items:
        if wanted_ratings and item.rating.rating not in wanted_ratings:
            continue
        if wanted_categories and not (
            wanted_categories & set(item.categories)
        ):
            continue
        if wanted_targets and item.target.target_type not in wanted_targets:
            continue
        if polarity and item.polarity != polarity:
            continue
        if needs_follow_up and not item.needs_follow_up:
            continue
        if training_only and not item.usable_for_training:
            continue
        out.append(item)
    return out


def answered_prompt_ids(items: Sequence[FeedbackItem]) -> set[str]:
    return {item.prompt_id for item in items if item.prompt_id}


def answered_target_keys(items: Sequence[FeedbackItem]) -> set[str]:
    return {item.target.key() for item in items}


def mark_answered(queue, items: Sequence[FeedbackItem]):
    """Flag the queue's prompts that already have feedback.

    Matched on target as well as prompt ID, so rating a record directly still
    ticks off the queue entry that was about it.
    """
    prompts = answered_prompt_ids(items)
    keys = answered_target_keys(items)
    for prompt in queue.prompts:
        prompt.answered = (
            prompt.prompt_id in prompts or prompt.target.key() in keys
        )
    return queue
