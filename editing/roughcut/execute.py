"""Execution modes, and the guards around them.

Four modes, in increasing order of consequence:

``plan_only``   build the operation list. Nothing is validated or run.
``dry_run``     validate the plan offline. Still nothing runs.
``execute``     run the plan, but only after a dry run passes in the same call.
``execute_on_scratch``  the same, with the scratch-sequence guarantee asserted.

The guards exist because this is the first session where the system can damage
something a person cares about. Each one refuses rather than warns:

* **Execution requires an explicit mode.** There is no default that runs
  anything; ``build`` and ``dry_run`` are the only things a bare call does.
* **A dry run must pass first**, in the same invocation. A previously-recorded
  pass is not accepted — the plan may have been rebuilt since.
* **The target must be a scratch sequence.** Writing to the user's active
  sequence requires ``allow_active_sequence=True``, and even then the plan is
  checked for the create/activate pair that proves it made its own sequence.
* **A refusal is a result, not an exception.** The report says what it declined
  to do and why, so a caller gets the same shape whether or not it ran.
"""
from __future__ import annotations

import time
from typing import Optional, Sequence

from editing.errors import EditingError
from editing.roughcut.schema import ExecutionReport, RoughCutPlan

#: Frame rate used for offline validation, matching ``premiere.engine``'s
#: dry-run default so a plan validated here behaves identically there.
DRY_RUN_FPS = 30.0

MODES = ("plan_only", "dry_run", "execute", "execute_on_scratch")

#: Ops that prove the plan builds and targets its own sequence.
_SCRATCH_MARKERS = ("sequence.create", "sequence.activate")


def dry_run(plan: RoughCutPlan, *, fps: float = DRY_RUN_FPS) -> RoughCutPlan:
    """Validate the plan offline. Records the result on the plan.

    Never raises for an invalid plan: an invalid plan is a finding to report,
    and the caller still wants the placements that produced it.
    """
    plan.dry_run_passed = False
    plan.dry_run_error = None
    plan.explanation = []

    if not plan.ops:
        plan.dry_run_error = {
            "code": "empty_plan",
            "error": "There are no operations to validate.",
            "hint": "Nothing was selected for the cut. Check the timeline has "
                    "usable segments, or lower --keep-threshold.",
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


def targets_scratch_sequence(plan: RoughCutPlan) -> bool:
    """Whether the plan creates and activates its own sequence.

    Checked structurally rather than trusted from a flag: the guarantee that
    matters is what the operations actually do, and a plan assembled by hand
    could set ``on_scratch`` without earning it.
    """
    names = [op.get("op") for op in plan.ops]
    if not all(marker in names for marker in _SCRATCH_MARKERS):
        return False
    # The sequence must be created before anything is placed on a track.
    create = names.index("sequence.create")
    activate = names.index("sequence.activate")
    first_edit = next(
        (i for i, name in enumerate(names)
         if name in ("clip.append", "clip.insert", "clip.overwrite")),
        len(names),
    )
    return create < activate < first_edit


def run(
    plan: RoughCutPlan,
    *,
    mode: str = "plan_only",
    bridge=None,
    engine=None,
    allow_active_sequence: bool = False,
    fps: float = DRY_RUN_FPS,
) -> ExecutionReport:
    """Carry out a plan to the depth ``mode`` allows.

    ``engine`` is injectable so tests can drive the whole path without
    Premiere; when omitted, a real ``EditEngine`` is built only if the mode
    actually needs one.
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
        operations_attempted=0,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    if mode == "plan_only":
        report.warnings.append(
            "plan-only: the operation list was built but not validated. Run "
            "the dry run before trusting it."
        )
        report.elapsed = time.time() - started
        return report

    # Every mode past plan_only validates first, including the execute modes --
    # a stored pass from an earlier build is not evidence about this plan.
    dry_run(plan, fps=fps)
    report.dry_run_passed = plan.dry_run_passed

    if mode == "dry_run":
        report.error = plan.dry_run_error
        report.operations_attempted = 0
        report.elapsed = time.time() - started
        return report

    # -- execution paths ------------------------------------------------
    if not plan.dry_run_passed:
        report.refused_reason = (
            "The dry run did not pass, so nothing was executed."
        )
        report.error = plan.dry_run_error
        report.elapsed = time.time() - started
        return report

    on_scratch = targets_scratch_sequence(plan)
    if not on_scratch and not allow_active_sequence:
        report.refused_reason = (
            "This plan does not create and activate its own sequence, so "
            "running it would edit whatever sequence is currently open. "
            "Rebuild the plan, or pass --allow-active-sequence if you really "
            "mean to edit the active one."
        )
        report.on_scratch = False
        report.elapsed = time.time() - started
        return report
    report.on_scratch = on_scratch

    if engine is None:
        engine = _build_engine(bridge)
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
    if not report.executed:
        report.error = {
            "code": result.get("code", "execution_failed"),
            "error": result.get("error", "Premiere reported a failure."),
            "hint": result.get("hint", ""),
        }
    report.elapsed = time.time() - started
    return report


def _build_engine(bridge):
    """A real engine, or None when the Premiere layer is unreachable."""
    try:
        from premiere.bridge import bridge as default_bridge
        from premiere.engine import EditEngine
    except ImportError:  # pragma: no cover - premiere always ships here
        return None

    transport = bridge if bridge is not None else default_bridge
    try:
        if not transport.health().get("connected"):
            return None
    except Exception:  # noqa: BLE001
        return None
    return EditEngine(bridge=transport)


def summarise(report: ExecutionReport, plan: RoughCutPlan) -> dict:
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
        "cut_duration": round(plan.total_duration, 2),
        "clips": len(plan.placements),
        "markers": len(plan.markers),
        "unconverted": len(plan.unconverted),
    }
