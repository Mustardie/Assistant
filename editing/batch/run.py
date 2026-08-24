"""Running one batch.

    discover candidates -> decide about each -> run it -> record -> next

Sequential, deliberately. Two runs at once would contend for the same shared
analysis cache and the same GPU, and the failure mode of a parallel batch --
two half-finished runs and no way to tell which log belongs to which -- is
worse than the wall-clock it saves.

## The four decisions per folder

``skip``     a completed run exists and neither --force nor --resume was given
``resume``   an incomplete run exists and --resume was given
``run``      anything else
``plan``     --dry-run: say which of the above it would have been, do nothing

## Failure handling

A folder that raises is caught, recorded with the exception, and the batch
moves to the next one. The only things that stop a batch early are a limit
being reached and a keyboard interrupt -- and the interrupt still leaves a
written summary, because the summary is rewritten after every folder rather
than at the end.
"""
from __future__ import annotations

import logging
import time
import traceback
from typing import Callable, Optional, Sequence

from editing.auto.runner import AutoRunner
from editing.auto.schema import AutoRunConfig
from editing.batch import discover as discover_module
from editing.batch import report as report_module
from editing.batch import store
from editing.batch.schema import (
    BatchCandidate, BatchConfig, BatchEntry, BatchSummary, batch_id_for, now,
)
from editing.config import EditingConfig
from editing.errors import EditingError

logger = logging.getLogger("nova.editing.batch.run")

Reporter = Callable[[str], None]

#: What to do with one folder. Returned by :func:`decide`, which is a pure
#: function so the policy can be tested without running anything.
DECISIONS = ("run", "resume", "skip", "force")


def _quiet(_message: str) -> None:
    return None


def run_config_for(
    batch: BatchConfig, candidate: BatchCandidate
) -> AutoRunConfig:
    """The run configuration one folder gets.

    Every field except the folder comes from the batch, because a batch
    applies one configuration to many folders. A batch where two episodes were
    edited differently for reasons nobody recorded is not a batch.
    """
    return AutoRunConfig(
        footage_folder=candidate.folder,
        style=batch.style,
        name=batch.name,
        mock=batch.mock,
        no_premiere=batch.no_premiere,
        recursive=batch.recursive,
        transcribe=batch.transcribe,
        director=batch.director,
        retention_cut=batch.retention_cut,
        # A batch that reshapes the cut should apply it: report-only across
        # forty folders produces forty decisions nobody asked for and no edit.
        retention_mode="retention" if batch.retention_cut else "report_only",
        render_proxy=batch.render_proxy,
        captions=batch.captions,
        audio_polish=batch.audio_polish,
    )


def decide(batch: BatchConfig, candidate: BatchCandidate) -> tuple:
    """What to do with one folder, and why. Pure.

    Returns ``(decision, reason, skip_code)``.
    """
    if not candidate.video_files:
        return "skip", "no video files in this folder", "no_video_files"

    completed = candidate.completed_run
    incomplete = candidate.incomplete_run

    if batch.only_new and candidate.existing_runs:
        return (
            "skip",
            f"--only-new was set and this folder already has "
            f"{len(candidate.existing_runs)} run(s)",
            "not_new",
        )
    if completed and batch.force:
        return (
            "force",
            f"run {completed} already completed; --force starts a new one "
            "beside it rather than overwriting it",
            "",
        )
    if completed:
        return (
            "skip",
            f"run {completed} already completed. --force runs it again in a "
            "new folder",
            "already_completed",
        )
    if incomplete and batch.resume:
        return "resume", f"continuing run {incomplete}", ""
    if incomplete and not batch.force:
        return (
            "skip",
            f"run {incomplete} exists and did not finish. --resume continues "
            "it, --force starts a new one",
            "already_failed",
        )
    return "run", "", ""


def run_batch(
    config: EditingConfig,
    batch: BatchConfig,
    *,
    runner: Optional[AutoRunner] = None,
    execute: Optional[Callable] = None,
    say: Reporter = _quiet,
    candidates: Optional[Sequence[BatchCandidate]] = None,
) -> BatchSummary:
    """Process every candidate folder under ``batch.root``.

    ``execute`` is injectable and is the only thing that touches a run: it
    takes ``(run_config, decision, existing_run_id)`` and returns an
    ``AutoRunState``. Tests pass a fake; production passes
    :func:`default_execute`, which is an ``AutoRunner``.
    """
    started = time.time()
    batch = _validated(batch)
    summary = BatchSummary(
        batch_id=_unique_batch_id(config, batch),
        config=batch,
        started_at=now(),
        status="running",
        warnings=list(batch.warnings),
    )

    if candidates is None:
        candidates = discover_module.find_candidates(
            batch.root, recursive=batch.recursive, config=config)

    say(f"[batch] {len(candidates)} folder(s) under {batch.root}")
    if not candidates:
        summary.warnings.append(
            f"no folder under {batch.root} directly contains video files. "
            "Sub-folders are searched; a folder whose clips are all one level "
            "down is not itself a candidate."
        )

    if execute is None:
        execute = default_execute(config, runner=runner, say=say)

    store.save(config, summary)
    processed = 0

    for candidate in candidates:
        entry = BatchEntry(
            folder=candidate.folder,
            label=candidate.label,
            video_files=candidate.video_files,
        )
        summary.entries.append(entry)

        if batch.limit and processed >= batch.limit:
            entry.status = "skipped"
            entry.skip_reason = "limit_reached"
            entry.reason = f"--limit {batch.limit} was reached"
            store.save(config, summary)
            continue

        decision, reason, skip_code = decide(batch, candidate)
        if decision == "skip":
            entry.status = "skipped"
            entry.skip_reason = skip_code or "unknown"
            entry.reason = reason
            entry.run_id = (candidate.completed_run
                            or candidate.incomplete_run or "")
            say(f"[batch] skip {candidate.label}: {reason}")
            store.append_log(
                config, summary.batch_id, f"skip {candidate.folder}: {reason}")
            store.save(config, summary)
            continue

        if batch.dry_run:
            entry.status = "planned"
            entry.reason = reason or f"would {decision} this folder"
            entry.run_id = (candidate.incomplete_run
                            if decision == "resume" else "")
            processed += 1
            say(f"[batch] would {decision} {candidate.label}")
            store.save(config, summary)
            continue

        processed += 1
        _process(config, summary, entry, batch, candidate, decision,
                 execute, say)
        store.save(config, summary)

    summary.status = "complete" if not summary.failed else "complete_with_failures"
    summary.ended_at = now()
    summary.elapsed = time.time() - started
    store.save(config, summary)
    store.save_report(
        config, summary.batch_id, report_module.render(summary))
    say(f"[batch] {summary.stats()}")
    return summary


def _process(
    config, summary, entry, batch, candidate, decision, execute, say
) -> None:
    """One folder, with its failure caught and recorded."""
    entry.status = "running"
    entry.started_at = now()
    started = time.time()
    say(f"[batch] {decision} {candidate.label} ({candidate.video_files} file(s))")
    store.append_log(
        config, summary.batch_id, f"{decision} {candidate.folder}")

    try:
        state = execute(
            run_config_for(batch, candidate), decision,
            candidate.incomplete_run or candidate.completed_run or "",
        )
    except EditingError as exc:
        _fail(config, summary, entry, started, exc.message, hint=exc.hint)
        return
    except KeyboardInterrupt:
        # Re-raised: a person pressing Ctrl-C means the batch, not this folder.
        entry.status = "failed"
        entry.reason = "interrupted"
        entry.ended_at = now()
        summary.status = "interrupted"
        store.save(config, summary)
        raise
    except Exception as exc:  # noqa: BLE001 - one bad folder is not the batch
        logger.debug("Folder %s raised:\n%s",
                     candidate.folder, traceback.format_exc())
        _fail(config, summary, entry, started,
              f"{type(exc).__name__}: {exc}",
              hint="This is a bug rather than a configuration problem; the "
                   "run's own log has the traceback.")
        return

    entry.elapsed = time.time() - started
    entry.ended_at = now()
    _record(config, entry, state)
    say(f"[batch] {candidate.label}: {entry.status} "
        f"({entry.run_status}) in {entry.elapsed:.0f}s")


def _fail(config, summary, entry, started, why: str, *, hint: str = "") -> None:
    entry.status = "failed"
    entry.reason = why[:600]
    if hint:
        entry.warnings.append(hint)
    entry.elapsed = time.time() - started
    entry.ended_at = now()
    store.append_log(config, summary.batch_id, f"FAILED {entry.folder}: {why}")


def _record(config, entry, state) -> None:
    """Everything worth keeping about a finished run.

    A run whose own stages failed is still a *completed* batch entry: the batch
    asked for it to be run, and it was. Whether the edit is any good is what
    the stage counts and the checks are for.
    """
    from editing.auto import store as auto_store
    from editing.review import store as review_store

    entry.status = "completed"
    entry.run_id = getattr(state, "run_id", "")
    entry.run_status = getattr(state, "status", "")

    stats = state.stats() if hasattr(state, "stats") else {}
    entry.stages_passed = int(stats.get("passed") or 0)
    entry.stages_failed = int(stats.get("failed") or 0)
    entry.stages_blocked = int(stats.get("blocked") or 0)

    checks = state.stage("reliability_gates") if hasattr(state, "stage") else None
    if checks is not None and checks.summary:
        entry.checks_status = str(checks.summary.get("status") or "")
        entry.checks_blocking = int(checks.summary.get("blocking") or 0)

    render = state.stage("render_proxy") if hasattr(state, "stage") else None
    if render is not None and render.summary:
        entry.video_path = str(render.summary.get("video") or "")

    if entry.run_id:
        entry.report_path = str(
            auto_store.report_paths(config, entry.run_id)[1])
        index = review_store.index_path(config, entry.run_id)
        entry.review_index = str(index) if index.exists() else ""

    entry.warnings = [
        f"[{result.stage}] {warning}"
        for result in getattr(state, "stages", [])
        for warning in result.warnings
    ][:20]


def default_execute(
    config: EditingConfig,
    *,
    runner: Optional[AutoRunner] = None,
    say: Reporter = _quiet,
) -> Callable:
    """The real executor: one ``AutoRunner`` per batch, one run per folder."""
    engine = runner or AutoRunner(config, say=say)

    def execute(run_config: AutoRunConfig, decision: str, run_id: str = ""):
        if decision == "resume" and run_id:
            return engine.resume(engine.load(run_id))
        state = engine.start(run_config, force_new_run=(decision == "force"))
        return engine.run(state)

    return execute


def _unique_batch_id(config: EditingConfig, batch: BatchConfig) -> str:
    """A batch ID no existing batch already has.

    The ID carries a per-second timestamp, so two batches started inside one
    second would land in the same folder and the second would overwrite the
    first's summary. "Nothing is ever overwritten" has to hold for the batch's
    own record as well as for the runs it makes, so this waits for the next
    second rather than clobbering.
    """
    batch_id = batch_id_for(batch)
    while store.batch_dir(config, batch_id).exists():
        time.sleep(1.0)
        batch_id = batch_id_for(batch)
    return batch_id


def _validated(batch: BatchConfig) -> BatchConfig:
    from dataclasses import replace

    return replace(
        batch,
        limit=max(0, int(batch.limit or 0)),
        style=str(batch.style or "minimal_clean"),
        name=str(batch.name or "structure"),
        created_at=batch.created_at or now(),
    )
