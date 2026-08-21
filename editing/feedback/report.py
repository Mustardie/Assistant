"""What the reviewer reads: the queue, one item, and the session report.

Two audiences, and they want opposite things.

``report.md`` is for the person who did the review, and for whoever opens the
session in six months. It leads with **what this feedback cannot support** --
how much of it was unusable, and why -- for the same reason the Session 7 run
report leads with what it did not do and the Session 8 report leads with what
it could not see. A page of preference signals reads as "the system now knows
what I like" unless the reader is told first that four of the eleven ratings
could not be joined to a record.

The CLI renderers are for the person mid-review, who wants the next question
and nothing else.

Every renderer prints ``NOT_MEASURED`` verbatim. One constant, so it cannot
soften into a claim in one of six places.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from editing.feedback import signals as signals_module
from editing.feedback import training as training_module
from editing.feedback.schema import (
    FeedbackItem, FeedbackSession, NOT_MEASURED, PreferenceSignal,
    ReviewPrompt, ReviewQueue, TrainingSignal,
)

_RULE = "=" * 78
_THIN = "-" * 78


def _wrap(text: str, width: int = 76, indent: str = "  ") -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = indent
    for word in words:
        if len(current) + len(word) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = indent
        current += word + " "
    if current.strip():
        lines.append(current.rstrip())
    return lines


def _clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    return f"{int(seconds // 60):d}:{seconds % 60:05.2f}"


# ---------------------------------------------------------------------------
# The summary, as data
# ---------------------------------------------------------------------------

def build_summary(
    session: FeedbackSession,
    *,
    history: Sequence[FeedbackItem],
    current: Sequence[FeedbackItem],
    preferences: Sequence[PreferenceSignal],
    training: Sequence[TrainingSignal],
    queue: Optional[ReviewQueue] = None,
    problems: Sequence[str] = (),
) -> dict:
    """Everything worth saying about a session, gathered into one object.

    Derived from the log every time it is called. Nothing here is a source of
    truth, which is why regenerating it is always safe and never asked about.
    """
    from editing.feedback.collect import counts_of

    counts = counts_of(history, current)
    coverage = _coverage(queue, current)
    return {
        "session": session.to_dict(),
        "basis": NOT_MEASURED,
        "counts": counts,
        "coverage": coverage,
        "preferences": {
            **signals_module.summarise(preferences),
            "items": [signal.to_dict() for signal in preferences],
        },
        "training": {
            **training_module.summarise(training),
            "items": [signal.to_dict() for signal in training],
        },
        "follow_up": [
            {
                "feedback_id": item.feedback_id,
                "summary": item.summary,
                "why": item.follow_up_note,
            }
            for item in current if item.needs_follow_up
        ],
        "log_problems": list(problems),
    }


def _coverage(
    queue: Optional[ReviewQueue], current: Sequence[FeedbackItem]
) -> dict:
    """How much of what was asked actually got answered.

    Reported because a review that answered three of twenty questions and a
    review that answered all twenty produce the same-looking preference list,
    and only one of them is worth acting on.
    """
    if queue is None:
        return {"queued": 0, "answered": 0, "share": 0.0,
                "unanswered_high_impact": 0}
    from editing.feedback.collect import (
        answered_prompt_ids, answered_target_keys,
    )
    prompts = answered_prompt_ids(current)
    keys = answered_target_keys(current)

    def is_answered(prompt) -> bool:
        return prompt.prompt_id in prompts or prompt.target.key() in keys

    answered = [prompt for prompt in queue.prompts if is_answered(prompt)]
    unanswered = [
        prompt for prompt in queue.prompts if not is_answered(prompt)
    ]
    return {
        "queued": len(queue.prompts),
        "answered": len(answered),
        "share": (
            round(len(answered) / len(queue.prompts), 3)
            if queue.prompts else 0.0
        ),
        "unanswered_high_impact": sum(
            1 for prompt in unanswered if prompt.impact == "high"),
        "candidates_not_queued": max(
            0, queue.candidates - len(queue.prompts)),
    }


# ---------------------------------------------------------------------------
# report.md
# ---------------------------------------------------------------------------

def render_report(summary: dict, *, limit: int = 40) -> str:
    """The session report, in Markdown, limits first."""
    session = summary.get("session", {})
    counts = summary.get("counts", {})
    coverage = summary.get("coverage", {})
    preferences = summary.get("preferences", {})
    training = summary.get("training", {})

    lines: list[str] = []
    add = lines.append

    add(f"# Feedback session -- {session.get('session_id', '(unnamed)')}")
    add("")
    if session.get("title"):
        add(f"**{session['title']}**")
        add("")
    add(f"- sequence: `{session.get('sequence_name') or '(no rough cut)'}`")
    add(f"- run: `{session.get('run_id') or '(no auto run)'}`")
    add(f"- timeline: `{session.get('name', 'structure')}`, timebase "
        f"`{session.get('timebase', 'empty')}`, "
        f"{_clock(session.get('duration', 0.0))} long")
    add(f"- style: `{session.get('style') or '(none recorded)'}`")
    add(f"- started: {session.get('created_at', '')}, "
        f"last updated: {session.get('updated_at', '')}")
    add("")

    add("## What this feedback cannot support")
    add("")
    add(f"> {NOT_MEASURED}")
    add("")
    unusable = int(training.get("unusable", 0) or 0)
    total = int(counts.get("items", 0) or 0)
    if total and unusable:
        add(f"- **{unusable} of {total}** ratings cannot become training "
            "data. Why:")
        for reason, number in sorted(
            (training.get("blocked_because") or {}).items(),
            key=lambda pair: -pair[1],
        ):
            add(f"  - {number} x {reason}")
    elif total:
        add(f"- all {total} rating(s) are usable as training material.")
    else:
        add("- no feedback has been recorded in this session yet.")

    if counts.get("unresolved_targets"):
        add(f"- {counts['unresolved_targets']} rating(s) point at an ID that "
            "is not in the artifacts. They are kept, and cannot be joined to "
            "a record.")
    if counts.get("needs_follow_up"):
        add(f"- {counts['needs_follow_up']} rating(s) need a follow-up, "
            "usually a correction saying *how much*.")
    if coverage.get("queued"):
        add(f"- {coverage.get('answered', 0)} of {coverage['queued']} queued "
            f"questions were answered "
            f"({float(coverage.get('share', 0.0)) * 100:.0f}%). "
            f"{coverage.get('unanswered_high_impact', 0)} high-impact "
            "question(s) are still open.")
    if coverage.get("candidates_not_queued"):
        add(f"- {coverage['candidates_not_queued']} further item(s) were "
            "worth reviewing and did not fit the queue limit.")
    for warning in session.get("warnings", []) or []:
        add(f"- {warning}")
    for problem in summary.get("log_problems", []) or []:
        add(f"- **log problem**: {problem}")
    add("")

    add("## What was said")
    add("")
    add(f"- {counts.get('items', 0)} rating(s) standing, "
        f"{counts.get('history', 0)} in the log "
        f"({counts.get('superseded', 0)} superseded)")
    add(f"- {counts.get('with_correction', 0)} carry a correction, "
        f"{counts.get('with_note', 0)} carry a note")
    by_polarity = counts.get("by_polarity", {}) or {}
    if by_polarity:
        add("- by verdict: " + ", ".join(
            f"{key} {value}" for key, value in sorted(by_polarity.items())))
    by_category = counts.get("by_category", {}) or {}
    if by_category:
        add("- by reason: " + ", ".join(
            f"{key} {value}" for key, value
            in sorted(by_category.items(), key=lambda p: -p[1])))
    by_target = counts.get("by_target_type", {}) or {}
    if by_target:
        add("- by target: " + ", ".join(
            f"{key} {value}" for key, value
            in sorted(by_target.items(), key=lambda p: -p[1])))
    add("")

    add("## Preference signals")
    add("")
    add("_Collected, never applied. Nothing in this system reads these._")
    add("")
    items = preferences.get("items", []) or []
    if not items:
        add("Nothing consistent enough to name yet.")
    else:
        add("| preference | evidence | agreement | confidence | scope | "
            "auto-safe |")
        add("|---|---|---|---|---|---|")
        for signal in items[:limit]:
            add(
                f"| {signal.get('statement', '')} "
                f"| {signal.get('evidence_count', 0)} "
                f"({signal.get('contradictions', 0)} against) "
                f"| {float(signal.get('agreement', 0.0)) * 100:.0f}% "
                f"| {float(signal.get('confidence', 0.0)):.2f} "
                f"| {signal.get('scope', 'episode')}"
                + (f", {signal.get('style')}" if signal.get('style') else "")
                + f" | {'yes' if signal.get('safe_to_apply_automatically') else 'no'} |"
            )
        add("")
        unsafe = [
            signal for signal in items
            if not signal.get("safe_to_apply_automatically")
            and signal.get("why_not_safe")
        ]
        if unsafe:
            add("Why the rest could not be acted on unsupervised:")
            add("")
            for signal in unsafe[:limit]:
                add(f"- **{signal.get('dimension')}** -- "
                    f"{signal.get('why_not_safe')}")
    add("")

    add("## Training material")
    add("")
    add("_Prepared for a later session. Nothing has been trained._")
    add("")
    add(f"- {training.get('usable', 0)} usable signal(s) of "
        f"{training.get('signals', 0)}, mean weight "
        f"{training.get('mean_weight', 0.0)}")
    add(f"- {training.get('with_before_after', 0)} carry a concrete "
        "before/after, "
        f"{training.get('with_correction', 0)} carry a correction")
    by_task = training.get("by_task", {}) or {}
    if by_task:
        add("")
        add("| task | usable examples |")
        add("|---|---|")
        for task, number in sorted(by_task.items(), key=lambda p: -p[1]):
            add(f"| {task} | {number} |")
    add("")

    follow_up = summary.get("follow_up", []) or []
    if follow_up:
        add("## Still needs you")
        add("")
        for entry in follow_up[:limit]:
            add(f"- `{entry.get('feedback_id')}` -- {entry.get('why')}")
            add(f"  - {entry.get('summary')}")
        add("")

    add("## What happens to this")
    add("")
    add("Nothing, yet. This session collects; Session 10 builds the dataset. "
        "Export it with:")
    add("")
    add("```")
    add(f"python -m editing.cli feedback export <out.jsonl> --session "
        f"{session.get('session_id', '<id>')}")
    add("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The CLI views
# ---------------------------------------------------------------------------

def render_queue(queue: ReviewQueue, *, limit: int = 40) -> str:
    lines: list[str] = []
    add = lines.append
    stats = queue.stats()

    add(_RULE)
    add(f"REVIEW QUEUE -- {queue.name}"
        + (f"  ({queue.sequence_name})" if queue.sequence_name else ""))
    add(_RULE)
    add("")
    add(f"  {stats['prompts']} question(s) from {stats['candidates']} "
        f"candidate(s), in {stats['groups']} group(s)")
    add(f"  runtime  : {_clock(queue.duration)}   timebase: {queue.timebase}")
    if stats["by_flag"]:
        add("  flags    : " + ", ".join(
            f"{key} {value}" for key, value
            in sorted(stats["by_flag"].items(), key=lambda p: -p[1])))
    add("")

    add("WHAT THIS QUEUE COULD NOT ASK ABOUT")
    add(_THIN)
    if queue.warnings:
        for warning in queue.warnings:
            for line in _wrap(f"! {warning}"):
                add(line)
    else:
        add("  Every pass had produced something, and it all fit.")
    add("")

    add("QUESTIONS")
    add(_THIN)
    add("  flags: ? uncertain  * high impact  ! decided automatically")
    add("         ^ structural  r retention risk  p setup/payoff")
    add("         x refused     + a decision that looks right")
    add("")
    # Groups are in priority order, not timeline order, so the divider is
    # numbered as well as timed -- "around 0:40" above "around 0:00" reads as
    # a bug unless it is clear these are ranked moments rather than a walk.
    last_group = ""
    number = 0
    for prompt in queue.prompts[:limit]:
        if prompt.group_id != last_group:
            last_group = prompt.group_id
            number += 1
            add(f"  -- moment {number}, around {_clock(prompt.start)} "
                + "-" * 34)
        add(f"  {prompt.line()}")
        if prompt.duplicates:
            add(f"      (+{prompt.duplicates} similar item(s) folded in)")
    if len(queue.prompts) > limit:
        add(f"  ... {len(queue.prompts) - limit} more")
    add("")
    add(_THIN)
    for line in _wrap(NOT_MEASURED):
        add(line)
    return "\n".join(lines)


def render_prompt(
    prompt: ReviewPrompt, *, history: Sequence[FeedbackItem] = ()
) -> str:
    """One question, in full, with anything already said about it."""
    lines: list[str] = []
    add = lines.append

    add(_RULE)
    add(f"{prompt.prompt_id}   [{prompt.source}]   priority "
        f"{prompt.priority:.2f}   impact {prompt.impact}")
    add(_RULE)
    add("")
    add(f"  when   : {_clock(prompt.start)} - {_clock(prompt.end)}")
    add(f"  about  : {prompt.target.describe()}")
    if prompt.target.artifact:
        add(f"  in     : {prompt.target.artifact}")
    add(f"  flags  : {', '.join(prompt.flags) or 'none'}")
    add("")
    add("QUESTION")
    add(_THIN)
    for line in _wrap(prompt.question):
        add(line)
    add("")
    add("WHY YOU ARE BEING ASKED")
    add(_THIN)
    for line in _wrap(prompt.why_asked):
        add(line)
    add("")
    add("WHAT THE SYSTEM DECIDED")
    add(_THIN)
    for line in _wrap(prompt.system_decision):
        add(line)
    add(f"  (its own confidence: {prompt.system_confidence:.2f})")
    if prompt.evidence:
        add("")
        add("EVIDENCE")
        add(_THIN)
        for entry in prompt.evidence:
            for line in _wrap(f"- {entry}"):
                add(line)
    if prompt.target.source_ids:
        add("")
        add("  built from: " + ", ".join(prompt.target.source_ids[:8]))

    if history:
        add("")
        add("ALREADY SAID")
        add(_THIN)
        for item in history:
            add(f"  {item.created_at}  {item.line()}")
            if item.note:
                for line in _wrap(f'"{item.note}"', indent="      "):
                    add(line)

    add("")
    add("ANSWER IT")
    add(_THIN)
    ratings = "|".join(prompt.suggested_ratings[:4]) or "good|bad|unsure"
    add(f"  python -m editing.cli feedback rate {prompt.prompt_id} "
        f"<{ratings}> \\")
    add(f"      --reason {prompt.category} --note \"...\"")
    return "\n".join(lines)


def render_item(
    item: FeedbackItem, *, history: Sequence[FeedbackItem] = ()
) -> str:
    lines: list[str] = []
    add = lines.append

    add(_RULE)
    add(f"{item.feedback_id}   {item.rating.rating}   ({item.polarity})")
    add(_RULE)
    add("")
    add(f"  when    : {item.created_at}")
    add(f"  about   : {item.target.describe()}")
    add(f"  artifact: {item.target.artifact or '(none)'}")
    add(f"  resolved: {item.target.resolved}"
        + (f" -- {item.target.resolution_note}"
           if item.target.resolution_note else ""))
    add(f"  reasons : {', '.join(item.categories)}")
    add(f"  priority: {item.priority:.2f}   your confidence: "
        f"{item.confidence:.2f}")
    if item.prompt_id:
        add(f"  answered: {item.prompt_id}")
    if item.supersedes:
        add(f"  replaces: {item.supersedes}")
    add("")
    if item.note:
        add("NOTE")
        add(_THIN)
        for line in _wrap(item.note):
            add(line)
        add("")
    if item.correction is not None:
        add("CORRECTION")
        add(_THIN)
        add(f"  action: {item.correction.action}"
            + (" (inferred from the text)" if item.correction.inferred else ""))
        for line in _wrap(item.correction.text):
            add(line)
        if item.correction.seconds is not None:
            add(f"  by    : {item.correction.seconds:+.2f}s")
        add("")
    add("WHAT HAPPENS TO THIS")
    add(_THIN)
    if item.usable_for_training:
        for line in _wrap(f"Usable as training material: {item.training_note}"):
            add(line)
    else:
        for line in _wrap(f"Not usable as training material: "
                          f"{item.training_note}"):
            add(line)
    if item.needs_follow_up:
        for line in _wrap(f"Needs follow-up: {item.follow_up_note}"):
            add(line)

    older = [entry for entry in history if entry.feedback_id != item.feedback_id]
    if older:
        add("")
        add("EARLIER ON THIS TARGET")
        add(_THIN)
        for entry in older:
            add(f"  {entry.created_at}  {entry.line()}")
    return "\n".join(lines)


def render_list(items: Sequence[FeedbackItem], *, limit: int = 60) -> str:
    lines = [
        f"{len(items)} item(s).  +positive -negative ~corrective ?unsure "
        "=neutral | T usable for training | ! needs follow-up",
        _THIN,
    ]
    for item in items[:limit]:
        lines.append(item.line())
    if len(items) > limit:
        lines.append(f"... {len(items) - limit} more")
    return "\n".join(lines)


def render_sessions(sessions: Sequence[FeedbackSession]) -> str:
    if not sessions:
        return ("No feedback sessions yet. Start one with "
                "`python -m editing.cli feedback start`.")
    lines = [f"{len(sessions)} session(s), newest first.", _THIN]
    for session in sessions:
        counts = session.counts or {}
        lines.append(
            f"  {session.session_id}  [{session.status}]  "
            f"{counts.get('items', 0)} item(s)  "
            f"run={session.run_id or '-'}  "
            f"{session.title[:30]}"
        )
    return "\n".join(lines)


def render_stats(summary: dict) -> str:
    """The numbers, without the prose. What ``feedback stats`` prints."""
    counts = summary.get("counts", {})
    coverage = summary.get("coverage", {})
    preferences = summary.get("preferences", {})
    training = summary.get("training", {})
    session = summary.get("session", {})

    lines: list[str] = [
        f"Session {session.get('session_id', '?')}  [{session.get('status', '?')}]",
        _THIN,
        f"  standing         : {counts.get('items', 0)}",
        f"  in the log       : {counts.get('history', 0)} "
        f"({counts.get('superseded', 0)} superseded)",
        f"  with a correction: {counts.get('with_correction', 0)}",
        f"  needs follow-up  : {counts.get('needs_follow_up', 0)}",
        f"  unresolved target: {counts.get('unresolved_targets', 0)}",
        "",
        f"  queue answered   : {coverage.get('answered', 0)}"
        f"/{coverage.get('queued', 0)}"
        f"  ({coverage.get('unanswered_high_impact', 0)} high-impact still "
        "open)",
        "",
        f"  preferences      : {preferences.get('signals', 0)} "
        f"({preferences.get('would_be_safe_to_apply', 0)} would be safe to "
        "apply, none are applied)",
        f"  training signals : {training.get('usable', 0)} usable of "
        f"{training.get('signals', 0)}",
    ]
    by_task = training.get("by_task", {}) or {}
    for task, number in sorted(by_task.items(), key=lambda p: -p[1]):
        lines.append(f"      {task:<26} {number}")
    lines.append("")
    lines.extend(_wrap(NOT_MEASURED))
    return "\n".join(lines)


def render_preferences(
    preferences: Sequence[PreferenceSignal], *, limit: int = 40
) -> str:
    lines = [
        f"{len(preferences)} preference signal(s). "
        "'A' means the evidence would support acting on it automatically; "
        "nothing does.",
        _THIN,
    ]
    for signal in preferences[:limit]:
        lines.append("  " + signal.line())
        if signal.why_not_safe:
            lines.extend(_wrap(signal.why_not_safe, indent="        "))
    lines.append("")
    lines.extend(_wrap(NOT_MEASURED))
    return "\n".join(lines)


def write(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
