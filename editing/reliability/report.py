"""The reliability report: what is wrong, in order of how much it matters.

Failures first, then warnings, then everything that passed collapsed into one
line each. That ordering is the whole design: a report where the two things
that matter sit below thirteen ticks is a report nobody scrolls.
"""
from __future__ import annotations

from editing.reliability.schema import (
    GATE_NAMES, NOT_A_QUALITY_JUDGEMENT, GateReport,
)

_RULE = "=" * 78
_THIN = "-" * 78


def render(report: GateReport) -> str:
    """Every gate, worst first."""
    lines: list[str] = []
    add = lines.append
    stats = report.stats()

    add(_RULE)
    add(f"RELIABILITY CHECKS -- {report.run_id or '(no run id)'}")
    add(_RULE)
    add(f"overall  : {stats['status']}")
    add(f"gates    : {stats['passed']} passed, {stats['warned']} warned, "
        f"{stats['failed']} failed, {stats['skipped']} not applicable")
    add(f"usable   : {'yes' if report.usable else 'NO'}")
    add("")

    if report.blocking:
        add(_THIN)
        add("THE OUTPUT IS NOT USABLE")
        add(_THIN)
        for result in report.blocking:
            _detail(add, result)
        add("")

    other_failures = [r for r in report.failures if r.can_continue]
    if other_failures:
        add(_THIN)
        add("FAILED, BUT THE RUN STILL PRODUCED SOMETHING")
        add(_THIN)
        for result in other_failures:
            _detail(add, result)
        add("")

    if report.warnings:
        add(_THIN)
        add(f"WORTH KNOWING ({len(report.warnings)})")
        add(_THIN)
        for result in report.warnings:
            _detail(add, result)
        add("")

    add(_THIN)
    add("EVERY CHECK")
    add(_THIN)
    order = {name: index for index, name in enumerate(GATE_NAMES)}
    for result in sorted(report.gates,
                         key=lambda r: order.get(r.name, len(order))):
        add(f"  {result.line()}")
    add("")
    add(f"  {NOT_A_QUALITY_JUDGEMENT}")
    add(_RULE)
    return "\n".join(lines)


def _detail(add, result) -> None:
    add(f"  {result.status.upper():<8} {result.name}")
    add(f"      what : {result.title}")
    add(f"      why  : {result.reason[:200]}")
    if result.evidence:
        pairs = ", ".join(
            f"{key}={value}" for key, value in list(result.evidence.items())[:6]
        )
        add(f"      saw  : {pairs[:200]}")
    if result.suggested_fix:
        add(f"      fix  : {result.suggested_fix[:200]}")
    if not result.can_continue:
        add("      stop : this one means the output is not worth reviewing")
    add("")


def render_short(report: GateReport) -> str:
    """One line per gate that is not a pass, for the run report."""
    interesting = [r for r in report.gates if r.status in ("warn", "fail")]
    if not interesting:
        return "  All reliability checks passed or did not apply."
    lines = []
    for result in interesting:
        lines.append(f"  {result.line()}")
        if result.suggested_fix:
            lines.append(f"      fix : {result.suggested_fix[:140]}")
    return "\n".join(lines)


def summary_lines(report: GateReport) -> list[str]:
    """The two or three sentences a review index wants."""
    stats = report.stats()
    out = [
        f"{stats['passed']} check(s) passed, {stats['warned']} warned, "
        f"{stats['failed']} failed."
    ]
    if report.blocking:
        out.append(
            "This run's output is not usable: "
            + "; ".join(r.reason[:120] for r in report.blocking)
        )
    elif report.failures:
        out.append(
            "Something failed and the run still produced an edit: "
            + "; ".join(r.reason[:120] for r in report.failures)
        )
    elif report.warnings:
        out.append(
            "Nothing failed. The warnings worth reading first: "
            + "; ".join(r.reason[:100] for r in report.warnings[:2])
        )
    return out
