"""What the director decided, said plainly.

Organised around the questions people ask about a creative pass, in order:

1. what shape of cut did it make?
2. **what did it decide, and why?**
3. what did the rules refuse, and which rule?
4. what is it unsure about?
5. what do I type next?

Point three gets its own section rather than a footnote. A layer where a model
proposes and rules dispose is only trustworthy if the disposing is visible --
a report showing forty accepted decisions and silently omitting the twelve
that were refused would misrepresent the whole design.
"""
from __future__ import annotations

from typing import Optional

from editing.director.schema import DirectorPlan, DirectorDecision

_RULE = "=" * 78
_THIN = "-" * 78

#: True of every director plan, however well the pass went.
LIMITATIONS = (
    "A language model wrote these judgements from a written description of "
    "the episode. It has not seen a single frame or heard a second of audio.",
    "The description it read is itself derived: vision labels, a transcript, "
    "audio heuristics. An error there is an error the director inherits.",
    "Decisions are checked for structure, not for taste. No rule can tell a "
    "good creative call from a confident bad one.",
    "Nothing here measures retention or audience response, and no figure in "
    "this plan is a prediction of either.",
    "The style guide is read by the model and not enforced by the rules -- a "
    "rule this system cannot check is one it must not claim to apply.",
    "This plan executes nothing. It produces ranges; the rough cut builder "
    "and its existing guards do the rest.",
)


def render(plan: DirectorPlan) -> str:
    """The full report, for ``<name>.plan.txt`` and ``director report``."""
    stats = plan.stats()
    lines: list[str] = []
    add = lines.append

    add(_RULE)
    add(f"DIRECTOR PASS -- {plan.name}")
    add(_RULE)
    add(f"mode       : {plan.mode}")
    add(f"backend    : {plan.backend}   model: {plan.model or '(none)'}")
    add(f"style guide: {plan.style_guide.name} ({plan.style_guide.source}, "
        f"{len(plan.style_guide.rules)} line(s))")
    add(f"decisions  : {stats['decisions']} proposed, "
        f"{stats['accepted']} accepted, {stats['rejected']} rejected, "
        f"{stats['modified']} modified")
    add(f"cut        : {stats['ranges']} range(s), "
        f"{stats['cut_duration']:.0f}s from {stats['source_duration']:.0f}s "
        f"of source")
    if plan.elapsed:
        add(f"took       : {plan.elapsed:.1f}s"
            + ("  (cached answer)" if plan.cached else ""))
    add("")

    if plan.mock:
        add(_THIN)
        add("MOCK DIRECTOR")
        add(_THIN)
        add("  These decisions come from four fixed rules over the candidate")
        add("  list, not from a model. Nothing below is an editorial")
        add("  judgement, and a cut built from it is a rule-based cut with")
        add("  extra steps. Point --backend at a real endpoint for the real")
        add("  thing.")
        add("")

    if plan.failure is not None:
        add(_THIN)
        add("FAILED")
        add(_THIN)
        for line in plan.failure.render().splitlines():
            add(f"  {line}")
        add("")
        add("  The heuristic selector is unaffected. A rough cut built")
        add("  without --director still works.")
        add("")

    if plan.approach:
        add(_THIN)
        add("THE DIRECTOR'S OWN SUMMARY")
        add(_THIN)
        for line in _wrap(plan.approach, 74):
            add(f"  {line}")
        add("")

    # -- what it decided ---------------------------------------------------
    add(_THIN)
    add(f"DECISIONS ({len(plan.accepted)} accepted)")
    add(_THIN)
    if not plan.accepted:
        add("  None survived. See the rejections below.")
    for decision in sorted(plan.accepted,
                           key=lambda d: (d.order, d.start))[:60]:
        add(f"  {decision.line()}")
        if decision.reason.style_rule:
            add(f"      style: \"{decision.reason.style_rule[:66]}\"")
        for note in decision.modifications[:2]:
            add(f"      changed: {note[:66]}")
    if len(plan.accepted) > 60:
        add(f"  ... and {len(plan.accepted) - 60} more.")
    add("")

    by_action = stats["by_action"]
    if by_action:
        add("  by action   : " + ", ".join(
            f"{name} x{count}" for name, count in
            sorted(by_action.items(), key=lambda pair: -pair[1])))
    by_reason = stats["by_reason"]
    if by_reason:
        add("  by reason   : " + ", ".join(
            f"{name} x{count}" for name, count in
            sorted(by_reason.items(), key=lambda pair: -pair[1])[:8]))
    add("")

    # -- what the rules refused --------------------------------------------
    add(_THIN)
    add(f"WHAT THE RULES REFUSED ({len(plan.rejected)})")
    add(_THIN)
    add("  The model proposes; deterministic checks decide. Every rejection")
    add("  names the check that made it.")
    add("")
    if not plan.rejected:
        add("  Nothing was rejected.")
    for decision in plan.rejected[:30]:
        add(f"  x {decision.decision_id[:14]:<14} {decision.action:<16} "
            f"{decision.start:.0f}-{decision.end:.0f}s")
        add(f"      why : {decision.rejected_reason[:66]}")
    if len(plan.rejected) > 30:
        add(f"  ... and {len(plan.rejected) - 30} more.")
    add("")

    by_check = plan.safety.by_check()
    if by_check:
        add("  by check    : " + ", ".join(
            f"{name} x{count}" for name, count in
            sorted(by_check.items(), key=lambda pair: -pair[1])))
        add("")

    measurements = plan.safety.measurements
    if measurements:
        add("  measured    : " + ", ".join(
            f"{key}={value}" for key, value in measurements.items()))
        add("")

    # -- uncertainty --------------------------------------------------------
    unsure = plan.needs_human_review
    add(_THIN)
    add(f"WORTH A HUMAN LOOK ({len(unsure)})")
    add(_THIN)
    if not unsure:
        add("  Nothing was flagged.")
    for decision in unsure[:20]:
        add(f"  ? {decision.start:.0f}-{decision.end:.0f}s  "
            f"{decision.action}  ({decision.confidence:.2f})")
        add(f"      {decision.reason.text[:66]}")
    add("")

    if plan.warnings:
        add(_THIN)
        add(f"WARNINGS ({len(plan.warnings)})")
        add(_THIN)
        for warning in plan.warnings[:25]:
            add(f"  ! {warning[:150]}")
        if len(plan.warnings) > 25:
            add(f"  ... and {len(plan.warnings) - 25} more.")
        add("")

    add(_THIN)
    add("NEXT")
    add(_THIN)
    for command in next_commands(plan):
        add(f"  {command}")
    add("")

    add(_THIN)
    add("LIMITATIONS")
    add(_THIN)
    for line in LIMITATIONS:
        for wrapped in _wrap(f"- {line}", 74):
            add(f"  {wrapped}")
    add("")
    add(f"  {plan.not_measured}")
    add("")
    add(_RULE)
    return "\n".join(lines)


def next_commands(plan: DirectorPlan) -> list[str]:
    """The shortest path forward from wherever this pass got to."""
    out: list[str] = []
    if plan.failure is not None:
        if plan.failure.hint:
            out.append(plan.failure.hint)
        out.append("python -m editing.cli director plan --backend mock")
        out.append("python -m editing.cli roughcut build")
        return out

    out.append(f"python -m editing.cli director show-decisions "
               f"--name {plan.name}")
    if plan.rejected:
        out.append(f"python -m editing.cli director show-rejected "
                   f"--name {plan.name}")
    out.append(f"python -m editing.cli director compare-heuristic "
               f"--name {plan.name}")
    out.append(f"python -m editing.cli director render --quality proxy")
    return out


def render_decisions(
    plan: DirectorPlan, *, limit: int = 60, action: str = "",
    rejected: bool = False,
) -> str:
    """``director show-decisions`` / ``show-rejected``."""
    pool = plan.rejected if rejected else plan.accepted
    if action:
        pool = [entry for entry in pool if entry.action == action]

    heading = "REJECTED DECISIONS" if rejected else "DECISIONS"
    lines = [_RULE, f"{heading} -- {plan.name} ({len(pool)})", _RULE, ""]
    if plan.mock:
        lines.append("  ! MOCK: these come from fixed rules, not a model.")
        lines.append("")
    if not pool:
        lines.append("  Nothing to show.")
        return "\n".join(lines)

    for decision in pool[:limit]:
        lines.extend(_detail(decision, rejected=rejected))
    if len(pool) > limit:
        lines.append(f"  ... and {len(pool) - limit} more.")
    return "\n".join(lines)


def _detail(decision: DirectorDecision, *, rejected: bool) -> list[str]:
    lines = [
        f"  {decision.decision_id}  {decision.action.upper()}",
        f"    range     : {decision.start:.1f}-{decision.end:.1f}s"
        + (f"  (uses {decision.out_start:.1f}-{decision.out_end:.1f}s)"
           if decision.out_duration < decision.duration - 0.05 else "")
        + (f"  @ {decision.speed:g}x" if decision.speed != 1.0 else ""),
        f"    segments  : {', '.join(decision.segment_ids[:6])}",
        f"    reason    : [{decision.reason.category}] "
        f"{decision.reason.text[:200]}",
    ]
    if decision.reason.style_rule:
        lines.append(f"    style rule: \"{decision.reason.style_rule[:120]}\"")
    lines.append(f"    effect    : {decision.viewer_effect}   "
                 f"confidence {decision.confidence:.2f}   "
                 f"priority {decision.priority:.2f}")
    links = [
        f"{label}={value}" for label, value in (
            ("beat", decision.beat_id), ("loop", decision.open_loop_id),
            ("setup", decision.setup_id), ("payoff", decision.payoff_id),
            ("suggestion", decision.suggestion_id),
        ) if value
    ]
    if links:
        lines.append(f"    links     : {', '.join(links)}")
    if decision.evidence:
        lines.append(f"    evidence  : {', '.join(decision.evidence[:5])}")
    if rejected:
        lines.append(f"    REFUSED   : {decision.rejected_reason[:200]}")
    for note in decision.modifications:
        lines.append(f"    changed   : {note[:200]}")
    for note in decision.safety_notes[:3]:
        lines.append(f"    note      : {note[:200]}")
    lines.append("")
    return lines


def render_context_summary(context) -> str:
    """``director build-context`` -- what the model would be shown."""
    stats = context.stats()
    lines = [_RULE, f"DIRECTOR CONTEXT -- {context.name}", _RULE, ""]
    lines.append(f"  footage    : {stats['duration']:.0f}s in "
                 f"{stats['segments']} candidate range(s)")
    lines.append(f"  with speech: {stats['with_speech']}   "
                 f"dead air: {stats['dead_air']}")
    lines.append(f"  story      : {stats['beats']} beat(s), "
                 f"{stats['open_loops']} open loop(s), "
                 f"{stats['setups']} setup(s), {stats['risks']} risk(s)")
    lines.append(f"  hooks      : {stats['hook_candidates']} candidate(s)")
    lines.append(f"  advice     : {stats['recommendations']} "
                 f"recommendation(s), {stats['preferences']} preference(s)")
    lines.append(f"  style guide: {context.style_guide.name} "
                 f"({context.style_guide.source})")
    lines.append("")

    present = [name for name, ok in context.sources.items() if ok]
    missing = [name for name, ok in context.sources.items() if not ok]
    lines.append(f"  built from : {', '.join(sorted(present)) or 'nothing'}")
    if missing:
        lines.append(f"  missing    : {', '.join(sorted(missing))}")
    lines.append("")

    if context.dropped:
        lines.append(_THIN)
        lines.append("LEFT OUT TO FIT THE BUDGET")
        lines.append(_THIN)
        for entry in context.dropped:
            lines.append(f"  - {entry}")
        lines.append("")

    if context.warnings:
        lines.append(_THIN)
        lines.append(f"WARNINGS ({len(context.warnings)})")
        lines.append(_THIN)
        for warning in context.warnings:
            lines.append(f"  ! {warning[:150]}")
        lines.append("")

    lines.append(_THIN)
    lines.append("SUMMARY THE DIRECTOR READS")
    lines.append(_THIN)
    for line in _wrap(context.summary, 74):
        lines.append(f"  {line}")
    lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]
