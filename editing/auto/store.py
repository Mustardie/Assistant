"""Where a run lives on disk, and how it is read back.

One folder per run, under ``data/editing/auto/runs/<run_id>/``:

```
config.json      exactly what the run was invoked with
state.json       every stage result and gate, rewritten after each stage
checkpoints/     one file per completed stage, with artifact fingerprints
artifacts/       the run's own output_dir -- timelines, plans, reports
reports/         report.json and report.txt
logs/            one log per run, appended as stages go
```

**Each run is hermetic.** ``artifacts/`` is the pipeline's ``output_dir`` for
that run, so two runs over the same footage with different styles cannot
overwrite each other's plans, and deleting a run folder removes everything it
produced and nothing it did not.

The one deliberate exception is the **cache**, which stays shared at the normal
``data/editing/cache``. Visual analysis is hundreds of model calls; making it
per-run would mean paying for it again on every run, which is the single worst
thing this package could do to someone's afternoon.

**A run is never silently overwritten.** Starting a run whose ID already exists
and is complete is refused; resuming is a different, explicit verb.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

from editing.auto.schema import AutoCheckpoint, AutoRunConfig, AutoRunState
from editing.config import EditingConfig
from editing.errors import EditingError

CONFIG_NAME = "config.json"
STATE_NAME = "state.json"
CHECKPOINTS = "checkpoints"
ARTIFACTS = "artifacts"
REPORTS = "reports"
LOGS = "logs"

#: Statuses a run can be in that mean "do not start over the top of this".
COMPLETE_STATUSES = frozenset({"complete"})


def runs_root(config: EditingConfig) -> Path:
    return config.output_dir / "auto" / "runs"


def run_dir(config: EditingConfig, run_id: str) -> Path:
    return runs_root(config) / run_id


def artifacts_dir(config: EditingConfig, run_id: str) -> Path:
    return run_dir(config, run_id) / ARTIFACTS


def run_config(config: EditingConfig, run_id: str) -> EditingConfig:
    """The ``EditingConfig`` a run's stages should use.

    Everything except ``output_dir`` is inherited, so the model backend, the
    ffmpeg paths and the Premiere switch all come from the caller. Only where
    the outputs land changes.
    """
    return replace(config, output_dir=artifacts_dir(config, run_id))


def create(
    config: EditingConfig,
    run: AutoRunConfig,
    run_id: str,
    *,
    force: bool = False,
) -> AutoRunState:
    """Make the run folder and its initial state. Refuses to clobber."""
    directory = run_dir(config, run_id)
    if directory.exists() and not force:
        existing = _try_load_state(directory)
        if existing is not None and existing.status in COMPLETE_STATUSES:
            raise EditingError(
                f"Run '{run_id}' already exists and completed",
                hint=f"Resume it with `auto resume --run {run_id}`, read it "
                     f"with `auto report --run {run_id}`, or start a fresh one "
                     "with --force-new-run.",
                detail={"run_dir": str(directory), "status": existing.status},
            )
        if existing is not None:
            # Incomplete: resuming is the right verb, but starting over the top
            # is allowed because nothing finished is at risk.
            return existing

    for name in (CHECKPOINTS, ARTIFACTS, REPORTS, LOGS):
        (directory / name).mkdir(parents=True, exist_ok=True)

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    run = replace(run, created_at=run.created_at or now)
    state = AutoRunState(
        run_id=run_id,
        config=run,
        created_at=now,
        updated_at=now,
        status="running",
        run_dir=str(directory.resolve()),
        artifacts_dir=str((directory / ARTIFACTS).resolve()),
    )
    write_config(config, run_id, run)
    save(config, state)
    return state


def write_config(
    config: EditingConfig, run_id: str, run: AutoRunConfig
) -> Path:
    target = run_dir(config, run_id) / CONFIG_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(run.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def save(config: EditingConfig, state: AutoRunState) -> Path:
    """Write the state atomically.

    Atomic because this is written after every stage, including the last one
    before someone hits Ctrl-C: a half-written ``state.json`` would turn a
    resumable run into a corrupted one.
    """
    target = run_dir(config, state.run_id) / STATE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def load(config: EditingConfig, run_id: str) -> AutoRunState:
    """Read a run's state, or say precisely what is wrong with it."""
    directory = run_dir(config, run_id)
    if not directory.exists():
        raise EditingError(
            f"No run named '{run_id}'",
            hint="List what exists with `auto list-runs`.",
            detail={"expected": str(directory)},
        )
    target = directory / STATE_NAME
    if not target.exists():
        raise EditingError(
            f"Run '{run_id}' has no state.json",
            hint=f"The folder exists but the run never started, or it was "
                 f"deleted. Remove it with `auto clean --run {run_id}` and "
                 "start again.",
            detail={"run_dir": str(directory)},
        )

    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Corrupted state is the one case where continuing is worse than
        # stopping: every later decision reads from it.
        raise EditingError(
            f"Run '{run_id}' has a corrupted state.json: {exc}",
            hint=f"Nothing can be resumed from it safely. Remove the run with "
                 f"`auto clean --run {run_id}` and start a fresh one.",
            detail={"path": str(target)},
        ) from None

    if not isinstance(document, dict) or not document.get("run_id"):
        raise EditingError(
            f"Run '{run_id}' has a state.json that is not a run record",
            hint=f"Remove it with `auto clean --run {run_id}` and start again.",
            detail={"path": str(target)},
        )

    state = AutoRunState.from_dict(document)
    state.run_dir = str(directory.resolve())
    state.artifacts_dir = str((directory / ARTIFACTS).resolve())
    return state


def _try_load_state(directory: Path) -> Optional[AutoRunState]:
    target = directory / STATE_NAME
    if not target.exists():
        return None
    try:
        return AutoRunState.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def checkpoint_path(config: EditingConfig, run_id: str, stage: str) -> Path:
    return run_dir(config, run_id) / CHECKPOINTS / f"{stage}.json"


def write_checkpoint(
    config: EditingConfig, run_id: str, checkpoint: AutoCheckpoint
) -> Path:
    target = checkpoint_path(config, run_id, checkpoint.stage)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def read_checkpoint(
    config: EditingConfig, run_id: str, stage: str
) -> Optional[AutoCheckpoint]:
    """A stage's checkpoint, or None.

    An unreadable checkpoint returns None rather than raising: the correct
    response to "I cannot tell whether this stage is done" is to do it again,
    which is exactly what None causes.
    """
    target = checkpoint_path(config, run_id, stage)
    if not target.exists():
        return None
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("stage") != stage:
        return None
    return AutoCheckpoint.from_dict(document)


def clear_checkpoint(config: EditingConfig, run_id: str, stage: str) -> bool:
    target = checkpoint_path(config, run_id, stage)
    try:
        target.unlink()
        return True
    except OSError:
        return False


def fingerprint_file(path: Path) -> str:
    """Cheap identity for an artifact: size and mtime.

    Not a content hash. These are files this system wrote seconds ago and is
    checking are still the ones it wrote; size plus mtime catches deletion,
    truncation, and a rebuild by hand, which is the whole set of things that
    actually happens between two stages of one run.
    """
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_size}:{int(stat.st_mtime)}"


# ---------------------------------------------------------------------------
# Listing and cleaning
# ---------------------------------------------------------------------------

def list_runs(config: EditingConfig, *, limit: int = 25) -> list[dict]:
    """Recent runs, newest first, without fully loading each one."""
    root = runs_root(config)
    if not root.exists():
        return []

    out: list[dict] = []
    for directory in sorted(root.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        state = _try_load_state(directory)
        if state is None:
            out.append({
                "run_id": directory.name,
                "status": "unreadable",
                "note": "no readable state.json",
                "run_dir": str(directory),
            })
            continue
        stats = state.stats()
        out.append({
            "run_id": state.run_id or directory.name,
            "status": state.status,
            "style": state.config.style,
            "folder": state.config.footage_folder,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "passed": stats["passed"],
            "failed": stats["failed"],
            "blocked": stats["blocked"],
            "gates_executed": stats["gates_executed"],
            "mock": state.config.mock,
            "run_dir": str(directory),
        })
        if len(out) >= limit:
            break
    return out


def clean(
    config: EditingConfig,
    *,
    run_id: Optional[str] = None,
    failed_only: bool = True,
    dry_run: bool = True,
) -> dict:
    """Remove runs. Refuses to delete completed work unless told to.

    ``failed_only`` is the default because the destructive mistake this guards
    against is obvious and irreversible: clearing out a completed run's
    artifacts while a Premiere sequence built from them is still open.
    """
    root = runs_root(config)
    result: dict = {"removed": [], "kept": [], "dry_run": dry_run}
    if not root.exists():
        return result

    targets = (
        [root / run_id] if run_id
        else [entry for entry in sorted(root.iterdir()) if entry.is_dir()]
    )

    for directory in targets:
        if not directory.exists():
            result["kept"].append({
                "run_id": directory.name, "reason": "does not exist",
            })
            continue
        state = _try_load_state(directory)
        status = state.status if state is not None else "unreadable"

        if failed_only and status in COMPLETE_STATUSES:
            result["kept"].append({
                "run_id": directory.name,
                "reason": "completed; pass --all to remove it anyway",
            })
            continue
        if failed_only and state is not None and any(
            gate.executed for gate in state.gates
        ):
            result["kept"].append({
                "run_id": directory.name,
                "reason": "has executed stages against Premiere; pass --all "
                          "to remove it anyway",
            })
            continue

        result["removed"].append({"run_id": directory.name, "status": status})
        if not dry_run:
            shutil.rmtree(directory, ignore_errors=True)
    return result


def append_log(config: EditingConfig, run_id: str, message: str) -> None:
    """One line into the run's log. Never raises."""
    target = run_dir(config, run_id) / LOGS / "run.log"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {message}\n")
    except OSError:
        pass


def log_path(config: EditingConfig, run_id: str) -> Path:
    return run_dir(config, run_id) / LOGS / "run.log"


def report_paths(config: EditingConfig, run_id: str) -> tuple:
    base = run_dir(config, run_id) / REPORTS
    return base / "report.json", base / "report.txt"
