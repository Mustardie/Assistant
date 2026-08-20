"""The human-readable revision report.

Written as a separate file from the rough cut's own report, never over it. A
revision pass is a *second opinion*, and losing the first one to make room for
it would destroy the only baseline anyone could compare against.

The order is chosen for someone deciding whether to run the plan: what could
not be fixed comes **before** what could. The automatic fixes are bounded and
reversible by construction; the deferred findings are where the real problems
with the cut are, and burying them under a list of successes is how a report
stops being read.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from editing.critic.schema import (
    SEVERITY_ORDER, CriticReport, RevisionPlan, RevisionSet,
)

_RULE = "=" * 78
_THIN = "-" * 78


def render(
    revisions: RevisionSet,
    *,
    critique: Optional[CriticReport] = None,
    plan: Optional[RevisionPlan] = None,
    limit: int = 40,
) -> str:
    lines: list[str] = []
    add = lines.append

    add(_RULE)
    add(f"REVISION REPORT -- {revisions.sequence_name or 'rough cut'}")
    add(_RULE)
    add(f"generated : {revisions.generated_at}")
    add(f"critic    : {revisions.model or 'unknown'}")
    if revisions.mock:
        add("            *** MOCK CRITIC -- no picture was examined ***")
    add("")

    if critique is not None:
        stats = critique.stats()
        add(f"Frames examined : {stats['frames_examined']}"
            f"  ({stats['frames_clean']} clean, "
            f"{stats['frames_failed']} failed)")
        add(f"Findings        : {stats['findings']} across "
            f"{stats['frames_with_findings']} frame(s)")
        if stats["by_issue"]:
            add("  by issue      : " + ", ".join(
                f"{issue} x{count}"
                for issue, count in sorted(
                    stats["by_issue"].items(), key=lambda kv: -kv[1]
                )
            ))
        add("")

    stats = revisions.stats()
    add(f"Revisions       : {stats['total']}")
    add(f"  accepted      : {stats['accepted']} "
        f"({stats['actionable']} with operations)")
    add(f"  needs a human : {stats['needs_human_review']}")
    add(f"  rejected      : {stats['rejected']}")
    add("")

    for warning in revisions.warnings:
        add(f"  ! {warning}")
    if revisions.warnings:
        add("")

    # -- what could NOT be fixed, first ---------------------------------
    deferred = sorted(
        revisions.needing_human(),
        key=lambda r: (-SEVERITY_ORDER.get(r.severity, 0), -r.confidence, r.start),
    )
    add(_THIN)
    add(f"NOT FIXED AUTOMATICALLY ({len(deferred)})")
    add(_THIN)
    if not deferred:
        add("  Everything the critic found had a safe automatic fix.")
    for revision in deferred[:limit]:
        add(f"  [{revision.start:8.2f}s] {revision.issue:<24} "
            f"{revision.severity:<6} {revision.confidence:.0%}")
        if revision.visual_evidence:
            add(f"      saw   : {revision.visual_evidence[:180]}")
        add(f"      why not: {revision.status_reason[:180]}")
        if revision.transcript_evidence:
            add(f"      said  : \"{revision.transcript_evidence[:120]}\"")
        if revision.audio_evidence:
            add(f"      heard : {', '.join(revision.audio_evidence[:4])}")
        add("")
    if len(deferred) > limit:
        add(f"  ... and {len(deferred) - limit} more.")
        add("")

    # -- what would be applied ------------------------------------------
    accepted = sorted(revisions.accepted(), key=lambda r: r.start)
    add(_THIN)
    add(f"WOULD BE APPLIED ({len(accepted)})")
    add(_THIN)
    if not accepted:
        add("  Nothing. Every finding was kept as a recommendation.")
    for revision in accepted[:limit]:
        add(f"  [{revision.start:8.2f}s] {revision.issue:<24} "
            f"-> {revision.suggested_fix}")
        if revision.fix_detail:
            add(f"      fix   : {revision.fix_detail[:180]}")
        if revision.visual_evidence:
            add(f"      saw   : {revision.visual_evidence[:180]}")
        if revision.risks:
            add(f"      risk  : {', '.join(revision.risks)}")
        add(f"      ops   : " + ", ".join(
            str(op.get("op")) for op in revision.premiere_ops
        ))
        add("")
    if len(accepted) > limit:
        add(f"  ... and {len(accepted) - limit} more.")
        add("")

    rejected = revisions.rejected()
    if rejected:
        add(_THIN)
        add(f"REJECTED ({len(rejected)})")
        add(_THIN)
        for revision in rejected[:limit]:
            add(f"  [{revision.start:8.2f}s] {revision.issue:<24} "
                f"{revision.status_reason[:120]}")
        add("")

    # -- the plan --------------------------------------------------------
    if plan is not None:
        add(_THIN)
        add("REVISION PLAN")
        add(_THIN)
        add(f"  sequence        : {plan.sequence_name}")
        add(f"  operations      : {plan.operation_count}")
        add(f"  revisions in it : {len(plan.revision_ids)}")
        add(f"  dry run         : "
            f"{'passed' if plan.dry_run_passed else 'not run / FAILED'}")
        add(f"  executed        : {plan.executed}")
        add(f"  on scratch      : {plan.on_scratch}")
        if plan.dry_run_error:
            add(f"  error           : {plan.dry_run_error.get('error')}")
            if plan.dry_run_error.get("hint"):
                add(f"  hint            : {plan.dry_run_error['hint']}")
        for warning in plan.warnings:
            add(f"  ! {warning}")
        if plan.explanation:
            add("")
            add("  What it would do:")
            for line in plan.explanation[:limit]:
                add(f"    {line}")
        add("")

    add(_RULE)
    add("Nothing in this report has been applied. The rough cut's own report "
        "is untouched;")
    add("this is a separate second opinion on it.")
    add(_RULE)
    return "\n".join(lines)


def render_issues(
    revisions: RevisionSet, *, limit: int = 40, severity: str = ""
) -> str:
    """The short form: one line per finding, worst first.

    What ``review show-issues`` prints. Deliberately terse -- this is the view
    for scanning a pass, not for deciding on one.
    """
    entries = revisions.ranked()
    if severity:
        floor = SEVERITY_ORDER.get(severity, 0)
        entries = [
            entry for entry in entries
            if SEVERITY_ORDER.get(entry.severity, 0) >= floor
        ]

    lines = [
        f"{len(entries)} issue(s) in '{revisions.sequence_name}'"
        + (f" at {severity} severity or above" if severity else "")
        + ":",
    ]
    if revisions.mock:
        lines.append("  (mock critic -- metadata only, no picture was seen)")
    for entry in entries[:limit]:
        lines.append("  " + entry.summary())
    if len(entries) > limit:
        lines.append(f"  ... and {len(entries) - limit} more.")
    return "\n".join(lines)


def write(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
