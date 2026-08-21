"""Execution modes for an asset placement plan, and the guards around them.

The same contract as the three passes before it: nothing runs without an
explicit mode, a dry run must pass **in the same call**, and a refusal is a
returned result rather than an exception.

This is the first pass that places *clips*, so it is the first whose allowlist
contains a ``clip.*`` operation, and that widening needs its own guard. Two
things make it safe:

**Only ``clip.overwrite``, never ``clip.insert``.** Insert ripples; overwrite
does not. So nothing this plan does can move a clip that was already on the
timeline, and every position computed by Sessions 3–5 stays exactly where those
sessions put it.

**Never V1 or A1.** Those are the rough cut's own tracks. Every asset lands on
a track this plan adds, checked structurally on every single operation rather
than trusted from the compiler. An overwrite is only "additive" because of
where it points, so where it points is the thing worth checking.

Together those two mean the pass is still reversible by hand: delete the added
tracks and the markers, and the cut is exactly as it was.

One guard is new. ``project.import`` brings files into the user's project, so
every path in it must live **inside the library root**. A plan that imports
from somewhere else is refused outright — not because the compiler would do
that, but because this is the last place to notice if something ever did.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from editing.assets.place import PROTECTED_TRACKS
from editing.assets.schema import AssetPlacementPlan
from editing.errors import EditingError
from editing.roughcut import execute as roughcut_execute
from editing.roughcut.execute import DRY_RUN_FPS
from editing.roughcut.schema import ExecutionReport, RoughCutPlan

MODES = ("plan_only", "dry_run", "execute")

#: Everything an asset plan may do.
ALLOWED_OPS = frozenset({
    "sequence.activate",   # exactly once, first, naming the target
    "project.import",      # library files only, into one bin
    "track.add",           # the tracks this pass writes to
    "clip.overwrite",      # place an asset -- never clip.insert, which ripples
    "graphic.image",       # image overlays
    "audio.gain",
    "audio.fade",
    "audio.duck",
    "marker.add",
})

#: Operations that place a clip and therefore need a track check.
_TRACK_OPS = frozenset({"clip.overwrite", "graphic.image"})

#: Refusals worth an explanation rather than a shrug.
_REFUSAL_REASONS = {
    "clip.insert": "insert ripples every later clip; asset placement uses "
                   "clip.overwrite on its own tracks so nothing moves",
    "sequence.create": "an asset pass places assets on an existing sequence; "
                       "it must never create one",
    "project.save": "an asset pass must not save the project on the user's "
                    "behalf",
    "clip.trim": "an asset pass must not change timing",
    "clip.speed": "an asset pass must not change timing",
    "clip.remove": "removing clips is a re-edit, not an asset pass",
    "clip.append": "appending to a track this pass does not own would land on "
                   "the rough cut's own assembly",
    "clip.move": "moving clips is a re-edit, not an asset pass",
    "track.remove": "an asset pass adds tracks; removing one could take "
                    "someone's work with it",
    "marker.remove": "an asset pass adds notes; removing the earlier passes' "
                     "markers is not its decision",
}


def dry_run(
    plan: AssetPlacementPlan, *, fps: float = DRY_RUN_FPS
) -> AssetPlacementPlan:
    """Validate the asset plan offline. Records the result on the plan.

    Never raises for an invalid plan: an invalid plan is a finding to report,
    and the caller still wants the placements that produced it.
    """
    plan.dry_run_passed = False
    plan.dry_run_error = None
    plan.explanation = []

    if not plan.ops:
        plan.dry_run_error = {
            "code": "empty_plan",
            "error": "There are no asset operations to validate.",
            "hint": "Either the layered edit has no placeholders an asset "
                    "could fill, or the library is empty. Run `assets "
                    "show-missing` to see which.",
        }
        return plan

    forbidden = check_allowed(plan)
    if forbidden:
        plan.dry_run_error = {
            "code": "forbidden_operation",
            "error": forbidden,
            "hint": "An asset plan may only contain: "
                    + ", ".join(sorted(ALLOWED_OPS)),
        }
        return plan

    tracks = check_tracks(plan)
    if tracks:
        plan.dry_run_error = {
            "code": "protected_track",
            "error": tracks,
            "hint": "Assets are placed on tracks this plan adds. V1 and A1 "
                    "belong to the rough cut.",
        }
        return plan

    imports = check_imports(plan)
    if imports:
        plan.dry_run_error = {
            "code": "import_outside_library",
            "error": imports,
            "hint": "An asset plan may only import files from inside its own "
                    "library root.",
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


def check_allowed(plan: AssetPlacementPlan) -> str:
    """The first forbidden operation's reason, or "" when all are allowed."""
    for index, op in enumerate(plan.ops):
        name = str(op.get("op") or "")
        if name in ALLOWED_OPS:
            continue
        reason = _REFUSAL_REASONS.get(
            name, "it is not one of the operations an asset pass may perform"
        )
        return (
            f"Operation {index + 1} is '{name}', which an asset plan may not "
            f"contain: {reason}."
        )
    return ""


def check_tracks(plan: AssetPlacementPlan) -> str:
    """Whether anything is aimed at a track the rough cut owns.

    Checked per operation rather than per placement: an overwrite is additive
    only because of where it points, so where it points is what matters.
    """
    for index, op in enumerate(plan.ops):
        if str(op.get("op")) not in _TRACK_OPS:
            continue
        track = str(op.get("track") or "").strip().upper()
        if not track:
            return (
                f"Operation {index + 1} places a clip without naming a track, "
                "so it would land on whichever track is targeted."
            )
        if track in PROTECTED_TRACKS:
            return (
                f"Operation {index + 1} places a clip on {track}, which "
                "belongs to the rough cut."
            )
    return ""


def check_imports(plan: AssetPlacementPlan) -> str:
    """Whether every imported path is inside the library root."""
    root = (plan.library_root or "").strip()
    if not root:
        return ""
    try:
        base = Path(root).resolve()
    except OSError:  # pragma: no cover - resolve rarely fails
        return ""

    for index, op in enumerate(plan.ops):
        if str(op.get("op")) != "project.import":
            continue
        for path in (op.get("paths") or []):
            try:
                candidate = Path(str(path)).resolve()
            except OSError:
                return f"Operation {index + 1} imports an unreadable path: {path}"
            if not _inside(candidate, base):
                return (
                    f"Operation {index + 1} imports {path}, which is outside "
                    f"the asset library at {base}."
                )
    return ""


def _inside(path: Path, base: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(base)]) == str(base)
    except ValueError:
        # Different drives on Windows: definitively not inside.
        return False


def targets_scratch_sequence(
    plan: AssetPlacementPlan, roughcut: Optional[RoughCutPlan] = None
) -> tuple:
    """Whether this plan provably stays inside the rough cut's scratch sequence.

    Returns ``(ok, reason)``; ``reason`` is empty when ok.
    """
    if not plan.ops:
        return False, "The plan has no operations."

    first = plan.ops[0]
    if str(first.get("op")) != "sequence.activate":
        return False, (
            "The plan does not begin by activating a sequence, so it would "
            "place assets on whichever sequence happens to be open."
        )
    if str(first.get("name") or "") != plan.sequence_name:
        return False, (
            f"The plan activates '{first.get('name')}' but claims to place "
            f"assets on '{plan.sequence_name}'."
        )

    later = [
        op for op in plan.ops[1:] if str(op.get("op")) == "sequence.activate"
    ]
    if later:
        return False, (
            "The plan activates a second sequence part way through, so its "
            "later operations would land somewhere else."
        )

    for check in (check_allowed, check_tracks, check_imports):
        problem = check(plan)
        if problem:
            return False, problem

    if roughcut is not None:
        if roughcut.sequence_name != plan.sequence_name:
            return False, (
                f"The rough cut builds '{roughcut.sequence_name}' but this "
                f"plan places assets on '{plan.sequence_name}'."
            )
        if not roughcut_execute.targets_scratch_sequence(roughcut):
            return False, (
                "The rough cut being scored does not itself create and "
                "activate its own scratch sequence, so placing assets on it "
                "is not provably safe."
            )
    return True, ""


def run(
    plan: AssetPlacementPlan,
    *,
    mode: str = "plan_only",
    roughcut: Optional[RoughCutPlan] = None,
    bridge=None,
    engine=None,
    allow_active_sequence: bool = False,
    fps: float = DRY_RUN_FPS,
) -> ExecutionReport:
    """Carry an asset plan out to the depth ``mode`` allows."""
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
            "plan-only: the asset operations were built but not validated. "
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
            "you really mean to place assets on whatever is open."
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

    missing = _missing_files(plan)
    if missing:
        report.refused_reason = (
            f"{len(missing)} asset file(s) this plan places are no longer on "
            f"disk, starting with {missing[0]}. Re-index the library."
        )
        report.error = {
            "code": "asset_missing",
            "error": "An asset file referenced by the plan is gone.",
            "hint": "Run `assets index` and rebuild the plan.",
            "detail": {"missing": missing[:10]},
        }
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


def _missing_files(plan: AssetPlacementPlan) -> list[str]:
    """Asset files the plan expects that are not on disk right now.

    Checked at execution rather than at compile time: a plan built this
    morning and run this evening can refer to a file that has moved, and
    Premiere's error for a missing import is much less clear than this one.
    """
    missing: list[str] = []
    for path in plan.assets_used():
        try:
            if not Path(path).exists():
                missing.append(path)
        except OSError:
            missing.append(path)
    return missing


def summarise(report: ExecutionReport, plan: AssetPlacementPlan) -> dict:
    """One dict describing what happened, for the CLI and the saved report."""
    stats = plan.stats()
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
        "placed": stats["placed"],
        "missing": stats["missing"],
        "distinct_assets": stats["distinct_assets"],
    }
