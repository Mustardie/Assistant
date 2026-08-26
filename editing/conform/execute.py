"""Execution modes for a conform plan, and the guards around them.

The same three-part contract as every other executable pass in this system:
nothing runs without an explicit mode, a dry run must pass **in the same call**,
and a refusal is a returned result rather than an exception.

The allowlist below is the widest in the system, and it is worth saying exactly
why each addition is safe.

``clip.overwrite`` places sound and music. Overwrite does not ripple, so it
cannot move anything the earlier passes measured -- and every one of them is
checked to be pointing at a track this layout does not protect.

``color.grade`` and ``transition.apply`` are the two that touch the programme
track, which nothing above the rough cut has been allowed to touch before.
Both are still non-destructive to the *cut*: a grade is an effect on a clip and
a transition consumes handle frames, neither moves a clip's position. They are
on the list because a colour treatment nobody can execute and a transition that
only exists as a plan were two of the specific things this pass exists to fix.

What stays off the list is the point of having one: no ``clip.remove``, no
``clip.insert``, no ``clip.move``, no ``clip.trim``, no ``project.save``, no
``sequence.create``. This pass cannot change what the cut *is*.
"""
from __future__ import annotations

import time
from typing import Optional, Sequence

from editing.conform.schema import ConformPlan
from editing.errors import EditingError
from editing.roughcut import execute as roughcut_execute
from editing.roughcut.execute import DRY_RUN_FPS, operation_succeeded
from editing.roughcut.schema import ExecutionReport, RoughCutPlan

MODES = ("plan_only", "dry_run", "execute")

#: Everything a conform plan is permitted to do.
ALLOWED_OPS = frozenset({
    "sequence.activate",   # exactly once, first, naming the target
    "track.add",           # the caption, treatment, overlay, sfx, music tracks
    "project.import",      # sound and music files
    "clip.overwrite",      # placing them; never insert, which ripples
    "text.create",         # captions, cards, labels
    "graphic.shape",       # callouts and plates
    "graphic.image",
    "animate",             # opacity fades, punch-ins, keyframed treatments
    "property.set",
    "clip.freeze",
    "clip.speed_ramp",
    "color.grade",
    "transition.apply",
    "audio.gain",
    "audio.fade",
    "audio.duck",
    "marker.add",
})

#: Operations that place something on a track, and therefore have to be checked
#: against the protected tracks rather than trusted from the builder.
_TRACK_OPS = ("clip.overwrite", "text.create", "graphic.shape", "graphic.image")


def dry_run(plan: ConformPlan, *, fps: float = DRY_RUN_FPS) -> ConformPlan:
    """Validate the plan offline. Records the result on the plan."""
    plan.dry_run_passed = False
    plan.dry_run_error = None
    plan.explanation = []

    if not plan.ops:
        plan.dry_run_error = {
            "code": "empty_plan",
            "error": "There are no operations to validate.",
            "hint": "Nothing survived the earlier passes. Check that the "
                    "caption, audio and visual passes produced accepted "
                    "decisions, or run with --conform full.",
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


def targets_scratch_sequence(plan: ConformPlan,
                             rough_cut: Optional[RoughCutPlan] = None) -> tuple:
    """Whether this plan provably stays inside the rough cut's own sequence.

    Three structural checks, in the order that makes a failure most
    informative: the target is fixed by the plan, every operation is on the
    allowlist, and nothing writes to a protected track.
    """
    ops = list(plan.ops or [])
    if not ops:
        return False, "the plan contains no operations."

    first = ops[0]
    if first.get("op") != "sequence.activate":
        return False, (
            "the plan's first operation is "
            f"'{first.get('op')}' rather than sequence.activate, so it would "
            "act on whichever sequence happens to be open."
        )
    if plan.sequence_name and first.get("name") != plan.sequence_name:
        return False, (
            f"the plan activates '{first.get('name')}' but says its sequence "
            f"is '{plan.sequence_name}'."
        )
    extra_activations = [op for op in ops[1:] if op.get("op") == "sequence.activate"]
    if extra_activations:
        return False, (
            "the plan activates a sequence more than once, so what it edits "
            "after the first switch cannot be checked here."
        )

    illegal = sorted({op.get("op", "?") for op in ops
                      if op.get("op") not in ALLOWED_OPS})
    if illegal:
        return False, (
            "these operations are not permitted in a conform plan: "
            + ", ".join(illegal)
            + ". This pass may add to a cut, never change what it is."
        )

    layout = plan.layout
    for index, op in enumerate(ops):
        if op.get("op") not in _TRACK_OPS:
            continue
        track = op.get("track") or (op.get("clip") or {}).get("track")
        if track and layout.is_protected(track):
            return False, (
                f"operation {index} places something on {track}, which belongs "
                "to the rough cut. Nothing this pass adds may land there."
            )

    if rough_cut is not None:
        if not roughcut_execute.targets_scratch_sequence(rough_cut):
            return False, (
                "the rough cut this applies to was not built on its own "
                "scratch sequence, so nothing here can be proven safe."
            )
        if rough_cut.sequence_name and plan.sequence_name != rough_cut.sequence_name:
            return False, (
                f"this plan targets '{plan.sequence_name}' but the rough cut "
                f"built '{rough_cut.sequence_name}'."
            )
    return True, ""


def run(
    plan: ConformPlan,
    *,
    mode: str = "plan_only",
    rough_cut: Optional[RoughCutPlan] = None,
    bridge=None,
    engine=None,
    allow_active_sequence: bool = False,
    fps: float = DRY_RUN_FPS,
) -> ExecutionReport:
    """Carry out a conform plan to the depth ``mode`` allows.

    ``allow_active_sequence`` relaxes one refusal and one only: a plan whose
    structural checks pass but whose rough cut cannot be confirmed as
    scratch-safe. It cannot let the plan write to a protected track, run an
    operation off the allowlist, or act on a sequence it did not name -- those
    are what make this pass additive, and a flag that could switch them off
    would make the guarantee worthless.
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
        on_scratch=False,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    if mode == "plan_only":
        report.warnings.append(
            "plan-only: the operation list was built but not validated."
        )
        report.elapsed = time.time() - started
        return report

    dry_run(plan, fps=fps)
    report.dry_run_passed = plan.dry_run_passed

    if mode == "dry_run":
        report.error = plan.dry_run_error
        report.elapsed = time.time() - started
        return report

    if not plan.dry_run_passed:
        report.refused_reason = "The dry run did not pass, so nothing was executed."
        report.error = plan.dry_run_error
        report.elapsed = time.time() - started
        return report

    safe, reason = targets_scratch_sequence(plan, rough_cut)
    report.on_scratch = safe
    if not safe and allow_active_sequence and _only_cut_provenance(plan):
        report.warnings.append(
            "--allow-active-sequence was set, so this ran despite " + reason
        )
    elif not safe:
        report.refused_reason = reason
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
                "hint": "Open Premiere with the Nova Premiere Bridge panel "
                        "(python -m scripts.premiere_host start), then retry.",
            }
            report.elapsed = time.time() - started
            return report

    report.operations_attempted = len(plan.ops)
    try:
        # ``continue`` rather than ``abort``: a conform plan is a stack of
        # independent additions, and one caption that fails to render is no
        # reason to leave the music, the mix and the grade unapplied. The
        # per-operation results say exactly what did and did not land.
        result = engine.run({**plan.as_edit_plan(dry_run=False),
                             "on_error": "continue"})
    except Exception as exc:  # noqa: BLE001 - an execution failure is a result
        to_dict = getattr(exc, "to_dict", None)
        report.error = (
            to_dict() if callable(to_dict)
            else {"code": "execution_error", "error": str(exc)}
        )
        report.elapsed = time.time() - started
        return report

    report.results = list(result.get("results") or [])
    report.operations_succeeded = sum(
        1 for entry in report.results if operation_succeeded(entry)
    )
    report.executed = report.operations_succeeded > 0
    failures = [entry for entry in report.results
                if not operation_succeeded(entry)]
    if failures:
        report.warnings.append(
            f"{len(failures)} of {len(report.results)} operations failed. "
            "The rest were applied."
        )
        first = failures[0]
        report.error = {
            "code": first.get("code", "operation_failed"),
            "error": (f"{first.get('op')} (operation "
                      f"{first.get('index', 0) + 1}) failed: "
                      f"{first.get('error')}"),
            "hint": first.get("hint", ""),
            "detail": {
                "failed": [
                    {"op": entry.get("op"), "index": entry.get("index"),
                     "error": entry.get("error")}
                    for entry in failures[:20]
                ],
            },
        }
    report.elapsed = time.time() - started
    return report


def _only_cut_provenance(plan: ConformPlan) -> bool:
    """Whether the refusal is about the *rough cut*, not about this plan.

    The structural checks on the plan itself -- one activate, an allowlisted
    operation set, nothing on a protected track -- are the guarantee. Those are
    never waived. What ``--allow-active-sequence`` may waive is only the
    separate question of whether the cut underneath was proven scratch-safe,
    which is unanswerable for a sequence somebody built by hand.
    """
    safe, _reason = targets_scratch_sequence(plan, rough_cut=None)
    return safe


def summarise(report: ExecutionReport, plan: ConformPlan) -> dict:
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
        "contributions": dict(plan.contributions),
        "unconverted": len(plan.unconverted),
        "colour": plan.color.look if plan.color.applied else "none",
        "music": plan.music.asset_name if plan.music.placed else "none",
        "transitions": len([t for t in plan.transitions if t.applied]),
    }


def executed_by_layer(report: ExecutionReport, plan: ConformPlan) -> dict:
    """Which layers actually landed, counted from the per-operation results.

    This is the number that answers the question the whole pass exists for --
    "did the captions become anything" -- and it is counted from what Premiere
    said, not from what the plan intended.
    """
    by_op: dict = {}
    for entry in report.results or ():
        name = entry.get("op", "?")
        ok = operation_succeeded(entry)
        bucket = by_op.setdefault(name, {"ok": 0, "failed": 0})
        bucket["ok" if ok else "failed"] += 1
    return by_op
