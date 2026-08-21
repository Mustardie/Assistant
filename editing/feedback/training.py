"""Reshaping feedback into something a dataset builder can read.

This is preparation, not a dataset. There is no prompt here, no completion, no
tokenisation and no train/test split -- those are Session 10's decisions, and
baking them in now would mean regenerating every signal the first time that
session changed its mind about a format.

What a ``TrainingSignal`` does carry is the four things any of those formats
would need and none of them can reconstruct later:

* **what the system was looking at** -- ``input_refs``, as artifact-plus-ID
  pairs rather than copies, so the signal stays small and cannot drift out of
  sync with the artifact;
* **what it decided, and how sure it was** -- taken from the queue prompt the
  reviewer answered, which is the only place the system's own reasoning was
  ever written down in a form a person had seen;
* **what the human said**, rating, reasons, correction and note;
* **whether this is usable, and why or why not.**

That last one is the load-bearing part. A signal that says "not usable" *with
its reason* tells the next session what kind of feedback this collector is
losing -- unresolvable targets, unsure ratings, directions with no magnitude --
which is the difference between fixing the collector and guessing at it. An
unusable signal is therefore emitted, not skipped.
"""
from __future__ import annotations

from typing import Optional, Sequence

from editing.feedback.schema import (
    FeedbackItem, ReviewPrompt, TASK_FOR_TARGET, TASK_TYPES, TrainingSignal,
    new_id,
)
from editing.schema import clamp01

#: Weight floor for an item that is usable at all. Even a hesitant rating on a
#: traceable decision is worth more than nothing, and a zero weight would be
#: indistinguishable from an excluded example.
MIN_WEIGHT = 0.15


def task_for(item: FeedbackItem) -> str:
    """What this piece of feedback could teach.

    The target type is the floor; the reason category refines it where one
    target type covers two genuinely different decisions. A layer item is the
    case that matters: a caption teaches *what text to write and when*, and a
    punch-in teaches *whether this channel likes punch-ins*, and those are not
    the same task even though both are ``LayerItem`` rows.
    """
    base = TASK_FOR_TARGET.get(item.target.target_type, "unknown")
    categories = set(item.categories)

    if item.target.target_type == "layer_item":
        if "caption" in categories:
            return "caption_decision"
        if "audio" in categories:
            return "asset_matching"
        return "style_preference"
    if item.target.target_type == "timeline_range":
        if "retention" in categories:
            return "retention_decision"
        if "caption" in categories:
            return "caption_decision"
        return "edit_decision"
    if item.target.target_type == "whole_edit":
        return "classification"
    if item.target.target_type == "retention_suggestion" \
            and "story" in categories:
        return "retention_decision"
    return base if base in TASK_TYPES else "unknown"


def _refs(item: FeedbackItem) -> list[dict]:
    """Where to look to reconstruct what the system saw.

    The target first, then every record it was derived from. IDs rather than
    copies -- a dataset that embedded the artifacts would be stale the moment
    a pass was re-run, and would not be able to tell that it was.
    """
    out: list[dict] = []
    if item.target.target_id or item.target.artifact:
        out.append({
            "artifact": item.target.artifact,
            "id": item.target.target_id,
            "type": item.target.target_type,
            "role": "target",
        })
    for source_id in item.target.source_ids[:20]:
        out.append({"artifact": "", "id": source_id, "type": "source",
                    "role": "evidence"})
    for artifact in item.source_artifacts:
        if artifact and artifact != item.target.artifact:
            out.append({"artifact": artifact, "id": "", "type": "artifact",
                        "role": "context"})
    return out


def _before_after(
    item: FeedbackItem, prompt: Optional[ReviewPrompt]
) -> tuple[dict, dict]:
    """The state before the correction and the state it asks for.

    Only produced when the correction is specific enough to describe an
    "after" without inventing numbers. A directional correction with no
    magnitude -- "shorter", with no idea how much shorter -- gets a before and
    no after, which is exactly what it is.
    """
    before = {
        "start": round(item.target.start, 3),
        "end": round(item.target.end, 3),
        "duration": round(item.target.duration, 3),
        "decision": system_decision_for(item, prompt),
    }
    correction = item.correction
    if correction is None or not correction.is_specific:
        return before, {}

    after = {"action": correction.action, "asked_for": correction.text}
    if correction.has_range:
        after["start"] = round(correction.start, 3)
        after["end"] = round(correction.end, 3)
        after["duration"] = round(correction.end - correction.start, 3)
    elif correction.seconds is not None and item.target.has_range:
        seconds = float(correction.seconds)
        if correction.action == "shorten":
            after["start"] = before["start"]
            after["end"] = round(
                max(item.target.start, item.target.end - abs(seconds)), 3)
        elif correction.action == "extend":
            after["start"] = before["start"]
            after["end"] = round(item.target.end + abs(seconds), 3)
        elif correction.action in ("move_earlier", "move_later"):
            shift = -abs(seconds) if correction.action == "move_earlier" \
                else abs(seconds)
            after["start"] = round(max(0.0, item.target.start + shift), 3)
            after["end"] = round(max(0.0, item.target.end + shift), 3)
        if "end" in after and "start" in after:
            after["duration"] = round(after["end"] - after["start"], 3)
    return before, after


def system_decision_for(
    item: FeedbackItem, prompt: Optional[ReviewPrompt]
) -> str:
    """What the system decided about this target, in words, or nothing.

    The queue prompt is the real answer: it is the only place a pass's
    reasoning was written down in a form the reviewer actually saw. Failing
    that, a record's own label is a fair stand-in -- ``danger_text "RUN" -- a
    creeper is right there`` *is* the decision.

    A ``timeline_range`` or ``whole_edit`` label is not. "the edit from 200.0s
    to 260.0s" is a description of where the reviewer was pointing, and
    treating it as a decision would let an opinion about the video into the
    dataset labelled as an opinion about a decision.
    """
    if prompt is not None and prompt.system_decision:
        return prompt.system_decision
    return item.target.label if item.target.is_identified else ""


def _weight(item: FeedbackItem) -> float:
    """How much this example should count.

    Editor confidence carries most of it, with priority as a smaller term:
    "I am sure about this" and "this matters" are different claims and the
    first is the one that makes an example trustworthy. Nothing here is a
    model's opinion of anything.
    """
    return round(
        max(
            MIN_WEIGHT,
            clamp01(0.7 * item.confidence + 0.3 * item.priority, 0.0),
        ),
        3,
    )


def signal_for(
    item: FeedbackItem,
    *,
    prompt: Optional[ReviewPrompt] = None,
    timebase: str = "empty",
) -> TrainingSignal:
    """One feedback item as a training signal, usable or not."""
    before, after = _before_after(item, prompt)
    signal = TrainingSignal(
        signal_id=new_id("ts", item.feedback_id),
        feedback_id=item.feedback_id,
        session_id=item.session_id,
        run_id=item.run_id,
        created_at=item.created_at,
        task=task_for(item),
        input_refs=_refs(item),
        system_decision=system_decision_for(item, prompt),
        system_confidence=(
            prompt.system_confidence if prompt is not None else 0.0),
        human_rating=item.rating.rating,
        human_polarity=item.polarity,
        human_correction=(
            item.correction.text if item.correction is not None else ""),
        correction_action=(
            item.correction.action if item.correction is not None else "none"),
        reason_labels=list(item.categories),
        note=item.note,
        before=before,
        after=after,
        target_type=item.target.target_type,
        target_id=item.target.target_id,
        start=item.target.start,
        end=item.target.end,
        timebase=timebase or "empty",
        usable_for_training=item.usable_for_training,
        weight=_weight(item) if item.usable_for_training else 0.0,
    )
    _explain(signal, item, prompt)
    return signal


def _explain(
    signal: TrainingSignal, item: FeedbackItem, prompt: Optional[ReviewPrompt]
) -> None:
    """Fill ``why`` and ``why_not``, and downgrade what cannot be learned from.

    Two extra exclusions live here rather than on the item, because they are
    about *training* rather than about whether the feedback was any good:

    * an ``unknown`` task -- nothing knows what this would teach;
    * no recorded system decision -- the human's answer is real, but with
      nothing to compare it to it is an opinion about the video rather than
      about a decision, and only the second kind can supervise anything.
    """
    blockers: list[str] = []
    if not item.usable_for_training:
        blockers.append(item.training_note or "excluded at collection time")
    if signal.task == "unknown":
        blockers.append(
            "nothing maps this target type to a task, so there is no question "
            "this example would be the answer to")
    if not signal.system_decision:
        blockers.append(
            "no system decision was recorded alongside the rating, so this is "
            "an opinion about the video rather than about a decision")

    if blockers:
        signal.usable_for_training = False
        signal.weight = 0.0
        signal.why_not = "; ".join(blockers)
        signal.why = ""
        return

    parts = [
        f"a {signal.human_polarity} human judgement of a recorded "
        f"{signal.target_type} decision",
    ]
    if signal.has_before_after:
        parts.append("with a concrete before and after")
    elif signal.human_correction:
        parts.append("with a correction giving the direction but not the size")
    if prompt is not None:
        parts.append(f"answering: {prompt.question[:80]}")
    signal.why = "; ".join(parts)
    signal.why_not = ""


def extract(
    items: Sequence[FeedbackItem],
    *,
    prompts: Optional[dict] = None,
    timebase: str = "empty",
) -> list[TrainingSignal]:
    """Every item as a signal, usable ones first.

    ``prompts`` maps prompt IDs to ``ReviewPrompt`` objects. Passing it is what
    lets a signal carry the system's own reasoning; without it every signal is
    unusable for the reason above, which is correct rather than unfortunate --
    feedback collected outside the queue genuinely does not know what it was
    disagreeing with.
    """
    lookup = prompts or {}
    signals = [
        signal_for(item, prompt=lookup.get(item.prompt_id), timebase=timebase)
        for item in items
    ]
    signals.sort(key=lambda s: (not s.usable_for_training, -s.weight, s.task))
    return signals


def usable(signals: Sequence[TrainingSignal]) -> list[TrainingSignal]:
    return [signal for signal in signals if signal.usable_for_training]


def summarise(signals: Sequence[TrainingSignal]) -> dict:
    by_task: dict[str, int] = {}
    blocked: dict[str, int] = {}
    for signal in signals:
        if signal.usable_for_training:
            by_task[signal.task] = by_task.get(signal.task, 0) + 1
        else:
            # The first clause of the reason is the actual blocker; the rest
            # is detail. Counting whole strings would give one bucket per item.
            key = (signal.why_not or "unknown").split(";")[0].strip()[:80]
            blocked[key] = blocked.get(key, 0) + 1
    ready = usable(signals)
    return {
        "signals": len(signals),
        "usable": len(ready),
        "unusable": len(signals) - len(ready),
        "with_before_after": sum(1 for s in ready if s.has_before_after),
        "with_correction": sum(1 for s in ready if s.human_correction),
        "mean_weight": (
            round(sum(s.weight for s in ready) / len(ready), 3) if ready else 0.0
        ),
        "by_task": by_task,
        "blocked_because": blocked,
    }
