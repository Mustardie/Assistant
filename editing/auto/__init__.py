"""One command for the whole pipeline, with checkpoints and gated execution.

    auto run --folder <folder> --style <preset>
        -> a run folder with its own artifacts
        -> sixteen stages in dependency order, checkpointed
        -> plans and offline dry runs for all four executable passes
        -> execution gates, each needing its own explicit --yes

    schema.py   AutoRunConfig, AutoStage, AutoStageResult, AutoCheckpoint,
                AutoRunState, AutoExecutionGate, AutoFailure, AutoRunReport
    store.py    the run folder, run IDs, durable state, listing and cleaning
    stages.py   the pipeline as a table, plus one runner per stage
    runner.py   the orchestrator: ordering, checkpoint validation, resume
    gates.py    what may be executed, why not, and executing exactly one thing
    report.py   the JSON and human-readable run reports

Six sessions produced about forty commands. This package exists so nobody has
to remember which of them come in which order, and it adds no editing
behaviour of its own.

Three properties it is built around.

**Planning and execution are separate verbs.** ``auto run`` builds every plan
and validates it offline. It never touches Premiere. The four passes that could
are exposed as named gates and executed one at a time, each with its own
``--yes``, because a rough cut and an asset placement carry genuinely different
risk and one switch for both would mean approving the riskier by approving the
safer.

**A checkpoint is verified before it is trusted.** Recording that a stage
passed is not enough: its artifacts must still exist, still match their
fingerprints, and still have been built from the same configuration. Changing
``--style`` therefore rebuilds the style and asset passes and leaves the
expensive analysis alone, with no flag to remember.

**A failure is a record with a command attached.** Every stopping point carries
what failed, why, whether the run can resume, and the exact next thing to type.
A traceback reaching a user is a bug in this package.
"""
from editing.auto.gates import (
    GATE_SPECS, compute_gates, execute, gate_names,
)
from editing.auto.report import (
    build_report, render, render_failure, render_status, write_reports,
)
from editing.auto.runner import AutoRunner, build_run_pipeline
from editing.auto.schema import (
    GATE_STAGES, STAGE_ORDER, AutoCheckpoint, AutoExecutionGate, AutoFailure,
    AutoRunConfig, AutoRunReport, AutoRunState, AutoStage, AutoStageResult,
    riskiest, run_id_for,
)
from editing.auto.stages import (
    ASSET_STAGES, BY_NAME, REVIEW_STAGES, RUNNERS, STAGES, StageBlocked,
    dependents, stage,
)
from editing.auto.store import (
    artifacts_dir, clean, create, list_runs, load, run_dir, save,
)

__all__ = [
    # schema
    "AutoRunConfig", "AutoRunState", "AutoStage", "AutoStageResult",
    "AutoCheckpoint", "AutoRunReport", "AutoExecutionGate", "AutoFailure",
    "STAGE_ORDER", "GATE_STAGES", "run_id_for", "riskiest",
    # stages
    "STAGES", "BY_NAME", "RUNNERS", "REVIEW_STAGES", "ASSET_STAGES",
    "StageBlocked", "stage", "dependents",
    # store
    "create", "load", "save", "list_runs", "clean", "run_dir", "artifacts_dir",
    # runner
    "AutoRunner", "build_run_pipeline",
    # gates
    "compute_gates", "execute", "gate_names", "GATE_SPECS",
    # report
    "build_report", "write_reports", "render", "render_status",
    "render_failure",
]
