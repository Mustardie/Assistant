"""Execution modes for a layered plan, and the guards around them.

The same contract as the rough cut and the critic: nothing runs without an
explicit mode, a dry run must pass **in the same call**, and a refusal is a
returned result rather than an exception.

The allowlist here is the tightest of the three, and it is the reason this
layer is the safest one to run:

    sequence.activate  track.add  animate  audio.fade  text.create  marker.add

There is no ``clip.*`` operation on that list. A style pass therefore **cannot
change timing** — it cannot trim, retime, move, split or remove a clip, and it
cannot alter the rough cut's assembly at all. Everything it does is additive
and lands on top of a layout that stays exactly where Session 3 computed it.

Two consequences worth stating, because they are what make this layer easy to
trust:

* **No operation ripples**, so no marker, overlay or zoom can end up describing
  a frame that moved out from under it. The Session 4 revision pass had to
  compute ripple corrections offline and warn that they were unverified; this
  one has nothing to correct.
* **The pass is reversible by hand.** Every overlay lands on one added track,
  and every marker carries its item ID. Deleting the track and the markers
  restores the rough cut, which is not true of a trim.
"""
from __future__ import annotations

import time
from typing import Optional

from editing.errors import EditingError
from editing.roughcut import execute as roughcut_execute
from editing.roughcut.execute import DRY_RUN_FPS
from editing.roughcut.schema import ExecutionReport, RoughCutPlan
from editing.style.schema import LayeredEditPlan

MODES = ("plan_only", "dry_run", "execute")

#: Everything a layered plan is permitted to do. Additive operations only.
ALLOWED_OPS = frozenset({
    "sequence.activate",   # exactly once, first, naming the target
    "track.add",           # the overlay track
    "animate",             # Motion > Scale for punches and pushes
    "audio.fade",          # level keyframes at the head and tail
    "text.create",         # captions and cards
    "marker.add",          # every placeholder
})

#: Refusals worth an explanation rather than a shrug. These are the plausible
#: mistakes; everything else outside the allowlist gets the generic message.
_REFUSAL_REASONS = {
    "sequence.create": "a style pass styles an existing sequence; it must "
                       "never create one",
    "project.import": "a style pass must not bring new media into the project",
    "project.save": "a style pass must not save the project on the user's behalf",
    "clip.trim": "a style pass must not change timing; trimming is the rough "
                 "cut's job",
    "clip.speed": "a style pass must not change timing; retiming is the rough "
                  "cut's job",
    "clip.remove": "removing clips is a re-edit, not a style pass",
    "clip.append": "appending clips is a re-edit, not a style pass",
    "clip.move": "moving clips is a re-edit, not a style pass",
    "marker.remove": "a style pass adds notes; removing the rough cut's own "
                     "markers is not its decision",
    "track.remove": "a style pass adds a track; removing one could take "
                    "someone's work with it",
}


def dry_run(plan: LayeredEditPlan, *, fps: float = DRY_RUN_FPS) -> LayeredEditPlan:
    """Validate the layered plan offline. Records the result on the plan.

    Never raises for an invalid plan: an invalid plan is a finding to report,
    and the caller still wants the items that produced it.
    """
    plan.dry_run_passed = False
    plan.dry_run_error = None
    plan.explanation = []

    if not plan.ops:
        plan.dry_run_error = {
            "code": "empty_plan",
            "error": "There are no layer operations to validate.",
            "hint": "Either every candidate was held back by this style, or "
                    "the rough cut has nothing to style. Run "
                    "`layers show-deferred` to see which.",
        }
        return plan

    forbidden = check_allowed(plan)
    if forbidden:
        plan.dry_run_error = {
            "code": "forbidden_operation",
            "error": forbidden,
            "hint": "A layered plan may only contain: "
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


def check_allowed(plan: LayeredEditPlan) -> str:
    """The first forbidden operation's reason, or "" when all are allowed."""
    for index, op in enumerate(plan.ops):
        name = str(op.get("op") or "")
        if name in ALLOWED_OPS:
            continue
        reason = _REFUSAL_REASONS.get(
            name, "it is not one of the operations a style pass may perform"
        )
        return (
            f"Operation {index + 1} is '{name}', which a layered plan may not "
            f"contain: {reason}."
        )
    return ""


def changes_timing(plan: LayeredEditPlan) -> bool:
    """Whether anything in this plan could move a clip. Always False by design.

    Kept as a function rather than a comment so the guarantee is assertable:
    if a future operation is added to the allowlist that ripples, this starts
    returning True and the test that pins it fails.
    """
    rippling = {"clip.trim", "clip.speed", "clip.remove", "clip.move",
                "clip.insert", "clip.split", "gap.remove"}
    return any(str(op.get("op")) in rippling for op in plan.ops)


def targets_scratch_sequence(
    plan: LayeredEditPlan, roughcut: Optional[RoughCutPlan] = None
) -> tuple:
    """Whether this plan provably stays inside the rough cut's scratch sequence.

    Returns ``(ok, reason)``; ``reason`` is empty when ok. Structural rather
    than trusted from a flag, for the same reason the other two sessions check
    it structurally: a plan assembled by hand could set ``on_scratch`` without
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
            f"The plan activates '{first.get('name')}' but claims to style "
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
                f"plan styles '{plan.sequence_name}'."
            )
        if not roughcut_execute.targets_scratch_sequence(roughcut):
            return False, (
                "The rough cut being styled does not itself create and "
                "activate its own scratch sequence, so styling it is not "
                "provably safe."
            )
    return True, ""


def run(
    plan: LayeredEditPlan,
    *,
    mode: str = "plan_only",
    roughcut: Optional[RoughCutPlan] = None,
    bridge=None,
    engine=None,
    allow_active_sequence: bool = False,
    fps: float = DRY_RUN_FPS,
) -> ExecutionReport:
    """Carry a layered plan out to the depth ``mode`` allows."""
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
            "plan-only: the layer operations were built but not validated. "
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
            "you really mean to style whatever is open."
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


def summarise(report: ExecutionReport, plan: LayeredEditPlan) -> dict:
    """One dict describing what happened, for the CLI and the saved report."""
    density = plan.density()
    return {
        "mode": report.mode,
        "executed": report.executed,
        "on_scratch": report.on_scratch,
        "sequence": plan.sequence_name,
        "style": plan.style,
        "dry_run_passed": report.dry_run_passed,
        "operations": plan.operation_count,
        "succeeded": report.operations_succeeded,
        "refused_reason": report.refused_reason,
        "planned": density["planned"],
        "edits_per_minute": density["edits_per_minute"],
        "deferred": len(plan.deferred()),
    }
