"""Execution modes for a revision plan, and the guards around them.

Deliberately the same three-part contract as ``roughcut.execute``: nothing runs
without an explicit mode, a dry run must pass **in the same call**, and a
refusal is a returned result rather than an exception. A reviewer who has read
the rough cut's guards has read these.

One guard is new, and it is the important one. A rough cut *creates* its own
scratch sequence, which is what proves it cannot touch the user's timeline. A
revision pass cannot do that -- it edits a sequence that already exists -- so
"did you make your own sandbox" is not a question it can answer. In its place:

* the plan's **first operation must be** ``sequence.activate`` naming the rough
  cut's sequence, so the target is fixed by the plan rather than inherited from
  whatever happens to be open;
* the rough cut being revised must itself have been scratch-safe;
* and every operation must be one of a **short allowlist**. A revision pass has
  no business importing media, saving the project, creating a sequence or
  removing a clip, so a plan containing any of those is refused rather than
  inspected further.

The allowlist is what makes the guarantee checkable. Anything that could reach
outside this sequence is simply not an operation a revision plan may contain.
"""
from __future__ import annotations

import time
from typing import Optional

from editing.critic.schema import RevisionPlan
from editing.errors import EditingError
from editing.roughcut.execute import DRY_RUN_FPS
from editing.roughcut.schema import ExecutionReport, RoughCutPlan
from editing.roughcut import execute as roughcut_execute

MODES = ("plan_only", "dry_run", "execute")

#: Everything a revision plan is permitted to do. Nothing here can affect a
#: sequence other than the activated one, or anything on disk.
ALLOWED_OPS = frozenset({
    "sequence.activate",   # exactly once, first, naming the target
    "property.reset",      # clear a zoom
    "animate",             # re-animate a gentler zoom
    "clip.trim",           # extend a hold / trim dead air
    "marker.add",
    "marker.remove",
})

#: Operations refused outright, with a reason worth printing. Everything not in
#: ``ALLOWED_OPS`` is refused; these get a specific explanation because they
#: are the plausible mistakes rather than the absurd ones.
_REFUSAL_REASONS = {
    "sequence.create": "a revision revises an existing sequence; it must "
                       "never create one",
    "project.import": "a revision must not bring new media into the project",
    "project.save": "a revision must not save the project on the user's behalf",
    "clip.remove": "removing clips is a re-edit, not a revision",
    "clip.append": "appending clips is a re-edit, not a revision",
    "clip.overwrite": "overwriting clips is a re-edit, not a revision",
    "clip.insert": "inserting clips is a re-edit, not a revision",
    "clip.speed": "retiming is the rough cut's job, not the revision pass's",
}


def dry_run(plan: RevisionPlan, *, fps: float = DRY_RUN_FPS) -> RevisionPlan:
    """Validate the revision plan offline. Records the result on the plan.

    Never raises for an invalid plan: an invalid plan is a finding to report,
    and the caller still wants the revisions that produced it.
    """
    plan.dry_run_passed = False
    plan.dry_run_error = None
    plan.explanation = []

    if not plan.ops:
        plan.dry_run_error = {
            "code": "empty_plan",
            "error": "There are no revision operations to validate.",
            "hint": "The critic either found nothing actionable or everything "
                    "it found was kept as a recommendation. Run "
                    "`review show-issues` to see what it did find.",
        }
        return plan

    forbidden = check_allowed(plan)
    if forbidden:
        plan.dry_run_error = {
            "code": "forbidden_operation",
            "error": forbidden,
            "hint": "A revision plan may only contain: "
                    + ", ".join(sorted(ALLOWED_OPS)),
        }
        return plan

    try:
        from premiere import validator
    except ImportError as exc:  # pragma: no cover - premiere always ships here
        plan.dry_run_error = {
            "code": "premiere_unavailable",
            "error": f"Could not import the Premiere validator: {exc}",
        }
        return plan

    try:
        validated = validator.validate_plan(plan.as_edit_plan(), fps=fps)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        to_dict = getattr(exc, "to_dict", None)
        plan.dry_run_error = (
            to_dict() if callable(to_dict)
            else {"code": "validation_error", "error": str(exc)}
        )
        return plan

    plan.dry_run_passed = True
    plan.explanation = validator.explain(validated)
    return plan


def check_allowed(plan: RevisionPlan) -> str:
    """The first forbidden operation's reason, or "" when all are allowed."""
    for index, op in enumerate(plan.ops):
        name = str(op.get("op") or "")
        if name in ALLOWED_OPS:
            continue
        reason = _REFUSAL_REASONS.get(
            name, "it is not one of the operations a revision may perform"
        )
        return (
            f"Operation {index + 1} is '{name}', which a revision plan may not "
            f"contain: {reason}."
        )
    return ""


def targets_scratch_sequence(
    plan: RevisionPlan, roughcut: Optional[RoughCutPlan] = None
) -> tuple[bool, str]:
    """Whether this plan provably stays inside the rough cut's scratch sequence.

    Returns ``(ok, reason)``; ``reason`` is empty when ok. Structural, not
    trusted from a flag -- the same stance ``roughcut.execute`` takes, for the
    same reason: a plan assembled by hand could set ``on_scratch`` without
    having earned it.
    """
    if not plan.ops:
        return False, "The plan has no operations."

    first = plan.ops[0]
    if str(first.get("op")) != "sequence.activate":
        return False, (
            "The plan does not begin by activating a sequence, so it would "
            "edit whichever sequence happens to be open."
        )
    if str(first.get("name") or "") != plan.sequence_name:
        return False, (
            f"The plan activates '{first.get('name')}' but claims to revise "
            f"'{plan.sequence_name}'."
        )

    later = [
        op for op in plan.ops[1:] if str(op.get("op")) == "sequence.activate"
    ]
    if later:
        return False, (
            "The plan activates a second sequence part way through, so its "
            "later operations would land somewhere else."
        )

    forbidden = check_allowed(plan)
    if forbidden:
        return False, forbidden

    if roughcut is not None:
        if roughcut.sequence_name != plan.sequence_name:
            return False, (
                f"The rough cut builds '{roughcut.sequence_name}' but this "
                f"plan revises '{plan.sequence_name}'."
            )
        if not roughcut_execute.targets_scratch_sequence(roughcut):
            return False, (
                "The rough cut being revised does not itself create and "
                "activate its own scratch sequence, so revising it is not "
                "provably safe."
            )
    return True, ""


def run(
    plan: RevisionPlan,
    *,
    mode: str = "plan_only",
    roughcut: Optional[RoughCutPlan] = None,
    bridge=None,
    engine=None,
    allow_active_sequence: bool = False,
    fps: float = DRY_RUN_FPS,
) -> ExecutionReport:
    """Carry a revision plan out to the depth ``mode`` allows.

    ``engine`` is injectable so the whole path is drivable in tests without
    Premiere; a real one is built only when the mode actually needs it.
    """
    if mode not in MODES:
        raise EditingError(
            f"Unknown execution mode '{mode}'",
            hint="Use one of: " + ", ".join(MODES),
        )

    started = time.time()
    report = ExecutionReport(
        mode=mode,
        sequence_name=plan.sequence_name,
        on_scratch=plan.on_scratch,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    if mode == "plan_only":
        report.warnings.append(
            "plan-only: the revision operations were built but not validated. "
            "Run the dry run before trusting them."
        )
        report.elapsed = time.time() - started
        return report

    # Both remaining modes validate first, including execute -- a stored pass
    # from an earlier build is not evidence about this plan.
    dry_run(plan, fps=fps)
    report.dry_run_passed = plan.dry_run_passed

    if mode == "dry_run":
        report.error = plan.dry_run_error
        report.elapsed = time.time() - started
        return report

    if not plan.dry_run_passed:
        report.refused_reason = (
            "The dry run did not pass, so nothing was executed."
        )
        report.error = plan.dry_run_error
        report.elapsed = time.time() - started
        return report

    ok, reason = targets_scratch_sequence(plan, roughcut)
    if not ok and not allow_active_sequence:
        report.refused_reason = (
            reason + " Rebuild the plan, or pass --allow-active-sequence if "
            "you really mean to edit whatever is open."
        )
        report.on_scratch = False
        report.elapsed = time.time() - started
        return report
    report.on_scratch = ok

    if not plan.roughcut_executed and not allow_active_sequence:
        report.refused_reason = (
            f"There is no record of the rough cut '{plan.sequence_name}' "
            "having been executed, so the sequence this plan activates "
            "probably does not exist. Run `roughcut execute --yes` first."
        )
        report.elapsed = time.time() - started
        return report

    if engine is None:
        engine = roughcut_execute._build_engine(bridge)
        if engine is None:
            report.refused_reason = (
                "Could not reach Premiere, so nothing was executed."
            )
            report.error = {
                "code": "bridge_unavailable",
                "error": "The Premiere bridge is not available.",
                "hint": "Open Premiere with the Nova Premiere Bridge panel, "
                        "then retry.",
            }
            report.elapsed = time.time() - started
            return report

    report.operations_attempted = len(plan.ops)
    try:
        result = engine.run(plan.as_edit_plan(dry_run=False))
    except Exception as exc:  # noqa: BLE001 - an execution failure is a result
        to_dict = getattr(exc, "to_dict", None)
        report.error = (
            to_dict() if callable(to_dict)
            else {"code": "execution_error", "error": str(exc)}
        )
        report.elapsed = time.time() - started
        return report

    report.executed = bool(result.get("success"))
    report.results = list(result.get("results") or [])
    report.operations_succeeded = sum(
        1 for entry in report.results if entry.get("ok")
    )
    if report.executed:
        plan.executed = True
    else:
        report.error = {
            "code": result.get("code", "execution_failed"),
            "error": result.get("error", "Premiere reported a failure."),
            "hint": result.get("hint", ""),
        }
    report.elapsed = time.time() - started
    return report


def summarise(report: ExecutionReport, plan: RevisionPlan) -> dict:
    """One dict describing what happened, for the CLI and the saved report."""
    return {
        "mode": report.mode,
        "executed": report.executed,
        "on_scratch": report.on_scratch,
        "sequence": plan.sequence_name,
        "dry_run_passed": report.dry_run_passed,
        "operations": plan.operation_count,
        "succeeded": report.operations_succeeded,
        "refused_reason": report.refused_reason,
        "revisions_applied": len(plan.revision_ids),
        "not_applied": len(plan.not_applied),
    }
