"""Execution gates: the only way anything reaches Premiere.

An automated run plans and validates. It never executes. The four things that
*could* execute are exposed as gates, and each is a separate, explicit act:

```
auto execute-stage roughcut --run <id> --yes
auto execute-stage review   --run <id> --yes
auto execute-stage layers   --run <id> --yes
auto execute-stage assets   --run <id> --yes
```

There is deliberately no ``--execute-everything``. The four passes have
genuinely different risk profiles — the rough cut builds a sequence, the
revision pass can ripple timing, the style pass is additive, the asset pass
places clips — and collapsing them into one switch would mean approving the
riskiest by approving the safest.

A gate is **computed, never stored as a decision**. It is read fresh from the
plan on disk and the run state every time it is asked for, because the answer
depends on files that can change between the question and the answer. What *is*
stored is the record that a gate was executed, and by which run.

The refusals are all pre-flight. Each underlying executor has its own guards
(a dry run in the same call, a structural scratch check, an operation
allowlist), and those still run afterwards — this layer exists so that the
common reasons get an explanation with a command attached instead of a refusal
buried three calls down.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from editing.auto import store
from editing.auto.schema import (
    GATE_STAGES, AutoExecutionGate, AutoRunState, riskiest,
)
from editing.config import EditingConfig
from editing.errors import EditingError

#: Gate -> (label, loader name, executor name, mode).
GATE_SPECS = {
    "roughcut": (
        "Build the rough cut sequence in Premiere",
        "load_rough_cut", "run_rough_cut", "execute_on_scratch",
    ),
    "review": (
        "Apply the critic's safe revisions",
        "load_revision_plan", "run_revisions", "execute",
    ),
    "layers": (
        "Apply the style: captions, emphasis, cards, markers",
        "load_layers", "run_layers", "execute",
    ),
    "assets": (
        "Place music, effects and graphics from the library",
        "load_asset_plan", "run_assets", "execute",
    ),
}

#: Gates whose plan embeds a belief about whether the rough cut has been built
#: in Premiere. Executing the rough cut makes those plans stale.
DEPENDS_ON_ROUGHCUT_EXECUTION = ("review", "layers", "assets")


def gate_names() -> list[str]:
    return list(GATE_SPECS)


def compute_gates(
    config: EditingConfig, state: AutoRunState
) -> list[AutoExecutionGate]:
    """Every gate, recomputed from what is on disk right now."""
    previous = {gate.stage: gate for gate in state.gates}
    return [
        _compute_one(config, state, name, previous.get(name))
        for name in GATE_SPECS
    ]


def _compute_one(
    config: EditingConfig,
    state: AutoRunState,
    name: str,
    previous: Optional[AutoExecutionGate],
) -> AutoExecutionGate:
    label, loader, _executor, _mode = GATE_SPECS[name]
    dry_run_stage = GATE_STAGES[name]

    gate = AutoExecutionGate(
        stage=name,
        label=label,
        dry_run_stage=dry_run_stage,
        command=(
            f"python -m editing.cli auto execute-stage {name} "
            f"--run {state.run_id} --yes"
        ),
    )
    if previous is not None:
        gate.executed = previous.executed
        gate.executed_at = previous.executed_at
        gate.operations_succeeded = previous.operations_succeeded

    # -- did the dry run stage pass in this run? --------------------------
    stage_result = state.stage(dry_run_stage)
    if stage_result is None or not stage_result.ok:
        gate.blocked_reason = (
            f"the {dry_run_stage} stage has not passed in this run"
            + (f" (it is {stage_result.status})" if stage_result else "")
            + f". Run: python -m editing.cli auto resume --run {state.run_id}"
        )
        return gate
    if stage_result.summary.get("empty"):
        gate.blocked_reason = (
            "there is nothing to execute: this pass produced no operations. "
            "That is a normal outcome, not a failure."
        )
        return gate

    # -- read the plan ----------------------------------------------------
    plan = _load_plan(config, state, loader)
    if plan is None:
        gate.blocked_reason = (
            f"no plan file was found for this stage. Run: "
            f"python -m editing.cli auto resume --run {state.run_id}"
        )
        return gate

    gate.plan_path = _plan_path(config, state, name)
    gate.plan_exists = True
    gate.sequence_name = getattr(plan, "sequence_name", "")
    gate.operation_count = len(getattr(plan, "ops", []) or [])
    gate.dry_run_passed = bool(getattr(plan, "dry_run_passed", False))
    worst, why = riskiest(getattr(plan, "ops", []) or [])
    gate.riskiest_operation, gate.riskiest_why = worst, why

    ok, reason = _scratch_safe(config, state, name, plan)
    gate.on_scratch = ok
    gate.scratch_reason = "" if ok else reason

    # -- the remaining blockers, cheapest first ---------------------------
    if gate.operation_count == 0:
        gate.blocked_reason = (
            "the plan contains no operations, so there is nothing to execute."
        )
        return gate
    if not gate.dry_run_passed:
        gate.blocked_reason = (
            "the plan's dry run has not passed. Run: "
            f"python -m editing.cli auto resume --run {state.run_id}"
        )
        return gate
    if not ok:
        gate.blocked_reason = reason
        return gate
    if state.config.no_premiere:
        gate.blocked_reason = (
            "this run was created with --no-premiere, so it may not execute "
            "anything. Start a run without that flag to execute."
        )
        return gate

    stale = _stale_reason(state, name, plan)
    if stale:
        gate.blocked_reason = stale
        return gate

    gate.ready = True
    return gate


def _load_plan(config: EditingConfig, state: AutoRunState, loader: str):
    """The plan for a gate, or None. Never raises."""
    from editing.auto.runner import build_run_pipeline

    try:
        pipeline = build_run_pipeline(config, state.run_id, state.config)
        return getattr(pipeline, loader)(name=state.config.name)
    except Exception:  # noqa: BLE001 - a missing plan is a blocked gate
        return None


def _plan_path(config: EditingConfig, state: AutoRunState, name: str) -> str:
    artifacts = Path(state.artifacts_dir or store.artifacts_dir(
        config, state.run_id
    ))
    run_name = state.config.name
    return str({
        "roughcut": artifacts / "roughcut" / f"{run_name}.json",
        "review": artifacts / "critic" / f"{run_name}.revision-plan.json",
        "layers": artifacts / "layers" / f"{run_name}.json",
        "assets": artifacts / "assets" / f"{run_name}.placement.json",
    }[name])


def _scratch_safe(
    config: EditingConfig, state: AutoRunState, name: str, plan
) -> tuple:
    """Whether this plan provably stays on the rough cut's scratch sequence.

    Delegated to each pass's own structural check rather than re-implemented,
    so the gate cannot drift out of step with the guard that will actually run.
    """
    from editing.auto.runner import build_run_pipeline

    if name == "roughcut":
        from editing.roughcut import execute as roughcut_execute

        ok = roughcut_execute.targets_scratch_sequence(plan)
        return ok, "" if ok else (
            "the plan does not create and activate its own sequence, so "
            "running it would edit whichever sequence is open."
        )

    try:
        pipeline = build_run_pipeline(config, state.run_id, state.config)
        roughcut = pipeline.load_rough_cut(name=state.config.name)
    except Exception:  # noqa: BLE001
        roughcut = None

    module = {
        "review": "editing.critic.execute",
        "layers": "editing.style.execute",
        "assets": "editing.assets.execute",
    }[name]
    executor = __import__(module, fromlist=["targets_scratch_sequence"])
    return executor.targets_scratch_sequence(plan, roughcut)


def _stale_reason(state: AutoRunState, name: str, plan) -> str:
    """Whether this plan's belief about the rough cut is out of date.

    The later passes each embed one fact: whether the rough cut sequence
    actually exists in Premiere. Their executors refuse on exactly that
    ground, so the honest thing is to say so here, with the command that fixes
    it, rather than let somebody hit a confusing refusal two steps later.

    Read from the plan rather than compared by timestamp. An earlier version
    compared the build time against the execution time, and both are recorded
    to the second -- so a plan rebuilt in the same second as the execution
    looked fresh when it was not.
    """
    if name not in DEPENDS_ON_ROUGHCUT_EXECUTION:
        return ""
    if getattr(plan, "roughcut_executed", True):
        return ""

    build_stage = {
        "review": "review_plan",
        "layers": "layers_build",
        "assets": "assets_plan",
    }[name]
    roughcut_gate = state.gate("roughcut")

    if roughcut_gate is not None and roughcut_gate.executed:
        return (
            "this plan was built before the rough cut was executed, so it "
            "still records the sequence as not existing and would be refused. "
            f"Rebuild it: python -m editing.cli auto resume "
            f"--run {state.run_id} --refresh {build_stage}"
        )
    return (
        "the rough cut has not been built in Premiere yet, so there is no "
        "sequence to apply this to. Run: python -m editing.cli auto "
        f"execute-stage roughcut --run {state.run_id} --yes"
    )


# ---------------------------------------------------------------------------
# Executing one gate
# ---------------------------------------------------------------------------

def execute(
    config: EditingConfig,
    state: AutoRunState,
    name: str,
    *,
    yes: bool = False,
    allow_active_sequence: bool = False,
    engine=None,
    bridge=None,
    say=None,
) -> dict:
    """Run one gate against Premiere. Refuses loudly and returns a result.

    Never raises for a refusal: a refusal is an outcome with a reason and a
    command, and the caller wants the same shape whether or not anything ran.
    """
    say = say or (lambda message: None)
    if name not in GATE_SPECS:
        raise EditingError(
            f"Unknown execution stage '{name}'",
            hint="Choose one of: " + ", ".join(gate_names()),
        )

    if not yes:
        return _refused(
            name, state,
            "executing writes to Premiere, so it needs an explicit "
            "confirmation.",
            code="needs_yes",
            command=(
                f"python -m editing.cli auto execute-stage {name} "
                f"--run {state.run_id} --yes"
            ),
        )

    gate = next(
        (g for g in compute_gates(config, state) if g.stage == name), None
    )
    if gate is None:  # pragma: no cover - GATE_SPECS covers every name
        return _refused(name, state, "no such gate.", code="unknown_gate")

    if gate.executed:
        return _refused(
            name, state,
            f"this stage was already executed at {gate.executed_at}. Running "
            "it again would place a second copy of everything.",
            code="already_executed",
            command=f"python -m editing.cli auto report --run {state.run_id}",
        )
    if not gate.ready:
        return _refused(
            name, state, gate.blocked_reason, code="gate_blocked",
            command=gate.command,
        )

    # -- everything checkable offline has passed; go to the host ---------
    from editing.auto.runner import build_run_pipeline

    _label, loader, executor, mode = GATE_SPECS[name]
    pipeline = build_run_pipeline(
        config, state.run_id, state.config, say=say, bridge=bridge
    )
    plan = getattr(pipeline, loader)(name=state.config.name)

    say(f"Executing {name} on '{gate.sequence_name}' "
        f"({gate.operation_count} operation(s))...")
    store.append_log(
        config, state.run_id,
        f"gate {name}: executing {gate.operation_count} operation(s)",
    )

    report = getattr(pipeline, executor)(
        plan, mode=mode, name=state.config.name,
        allow_active_sequence=allow_active_sequence, engine=engine,
    )

    gate.executed = bool(report.executed)
    gate.executed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    gate.operations_succeeded = report.operations_succeeded
    _store_gate(state, gate)

    if report.executed and name == "roughcut":
        _invalidate_after_roughcut(config, state)

    store.append_log(
        config, state.run_id,
        f"gate {name}: executed={report.executed} "
        f"{report.operations_succeeded}/{report.operations_attempted}"
        + (f" refused: {report.refused_reason}" if report.refused_reason else ""),
    )
    store.save(config, state)

    refused, next_command = _explain_refusal(state, name, gate, report)
    return {
        "success": bool(report.executed),
        "stage": name,
        "run_id": state.run_id,
        "executed": report.executed,
        "sequence": gate.sequence_name,
        "operations_attempted": report.operations_attempted,
        "operations_succeeded": report.operations_succeeded,
        "on_scratch": report.on_scratch,
        "dry_run_passed": report.dry_run_passed,
        "refused_reason": refused,
        "error": report.error,
        "next_command": next_command,
    }


def _explain_refusal(state, name, gate, report) -> tuple:
    """Turn a bare refusal into something a person can act on.

    "Premiere unavailable" tells someone nothing they did not already suspect.
    What they need is the two things to do about it and the command to type
    afterwards, which this layer knows and the executor does not.
    """
    if not report.refused_reason:
        return "", _next_after(state, name, report)

    code = (report.error or {}).get("code", "")
    if code == "bridge_unavailable":
        return (
            f"{gate.label} is blocked because the Premiere Bridge is "
            "unreachable. Start Premiere, open the Nova Premiere Bridge panel "
            "(Window > Extensions), then run the command below.",
            gate.command,
        )
    if "does not create and activate" in report.refused_reason or (
        "scratch" in report.refused_reason
    ):
        return (
            report.refused_reason
            + f" Rebuild the plan with: python -m editing.cli auto resume "
              f"--run {state.run_id} --refresh {GATE_STAGES[name]}",
            f"python -m editing.cli auto show-gates --run {state.run_id}",
        )
    return report.refused_reason, _next_after(state, name, report)


def _refused(
    name: str, state: AutoRunState, why: str, *, code: str, command: str = ""
) -> dict:
    return {
        "success": False,
        "stage": name,
        "run_id": state.run_id,
        "executed": False,
        "refused_reason": why,
        "code": code,
        "next_command": command
        or f"python -m editing.cli auto show-gates --run {state.run_id}",
    }


def _store_gate(state: AutoRunState, gate: AutoExecutionGate) -> None:
    for index, existing in enumerate(state.gates):
        if existing.stage == gate.stage:
            state.gates[index] = gate
            return
    state.gates.append(gate)


def _invalidate_after_roughcut(
    config: EditingConfig, state: AutoRunState
) -> None:
    """The later plans now believe something that is no longer true.

    They were built while the rough cut existed only as a plan, and they each
    recorded that. Clearing their checkpoints means the next ``resume``
    rebuilds them with the sequence genuinely in Premiere, which is the
    difference between the later gates working and being refused.
    """
    for stage in ("review_plan", "review_dry_run", "layers_build",
                  "layers_dry_run", "assets_plan", "assets_dry_run"):
        store.clear_checkpoint(config, state.run_id, stage)
    state.warnings.append(
        "The rough cut was executed, so the review, style and asset plans were "
        f"marked stale. Rebuild them with: python -m editing.cli auto resume "
        f"--run {state.run_id}"
    )


def _next_after(state: AutoRunState, name: str, report) -> str:
    if report.refused_reason or not report.executed:
        return f"python -m editing.cli auto show-gates --run {state.run_id}"
    order = list(GATE_SPECS)
    index = order.index(name)
    if name == "roughcut":
        return (
            f"python -m editing.cli auto resume --run {state.run_id}   "
            "(rebuilds the later plans now the sequence exists)"
        )
    if index + 1 < len(order):
        following = order[index + 1]
        return (
            f"python -m editing.cli auto execute-stage {following} "
            f"--run {state.run_id} --yes"
        )
    return f"python -m editing.cli auto report --run {state.run_id}"
