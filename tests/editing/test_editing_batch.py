"""Batch mode: discovery, the four decisions, and surviving a bad folder.

Three properties carry the weight.

**A dry run creates nothing.** It is the command a person types first, over a
library they care about, and if it made forty run folders nobody would type it
twice. The test asserts on the filesystem, not on the summary.

**Nothing is ever overwritten.** A completed run is skipped; ``--force`` gives
the folder a *new* run beside the old one. There is no path through this
package that writes over finished work, and that is asserted by watching what
the executor is asked to do.

**One failure does not stop the batch.** The most useful property of an
overnight run is that it is still going in the morning, so a folder that raises
is recorded and the next one starts.

Nothing here runs a real pipeline: the executor is injected, and the folders
are empty files with video extensions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from editing.auto import store as auto_store
from editing.auto.schema import AutoRunConfig, AutoRunState, AutoStageResult
from editing.batch import discover as discover_module
from editing.batch import report as batch_report
from editing.batch import run as batch_run
from editing.batch import store as batch_store
from editing.batch.schema import (
    BatchCandidate, BatchConfig, BatchSummary, batch_id_for,
)
from editing.errors import FootageError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def library(tmp_path) -> Path:
    """Three episode folders, one empty folder, and one folder to skip."""
    root = tmp_path / "clips"
    for name in ("ep01", "ep02", "ep03"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "a.mp4").write_bytes(b"not a video" * 64)
        (folder / "b.mp4").write_bytes(b"not a video" * 64)
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "readme.txt").write_text("no footage here", "utf-8")
    render = root / "render"
    render.mkdir()
    (render / "proxy.mp4").write_bytes(b"a proxy" * 64)
    return root


def make_state(run_id: str = "run-1", *, status: str = "complete",
               stages=()) -> AutoRunState:
    state = AutoRunState(run_id=run_id, status=status)
    for name, stage_status, summary in stages:
        state.stages.append(AutoStageResult(
            stage=name, status=stage_status, summary=dict(summary or {})))
    return state


def recording_execute(states=None, *, fail_on=()):
    """An executor that records what it was asked to do."""
    calls: list = []

    def execute(run_config: AutoRunConfig, decision: str, run_id: str = ""):
        calls.append({"folder": run_config.footage_folder,
                      "decision": decision, "run_id": run_id,
                      "config": run_config})
        if any(token in run_config.footage_folder for token in fail_on):
            raise RuntimeError("that folder is cursed")
        if states:
            return states.pop(0)
        return make_state(f"run-{len(calls)}")

    execute.calls = calls
    return execute


def config_for(root: Path, **overrides) -> BatchConfig:
    return BatchConfig(root=str(root), style="cinematic_minecraft",
                       no_premiere=True, mock=True, **overrides)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discovery_finds_folders_holding_footage(library):
    found = discover_module.find_candidates(library)
    labels = {candidate.label for candidate in found}
    assert {"ep01", "ep02", "ep03"} <= labels


def test_a_folder_with_no_video_is_not_a_candidate(library):
    found = discover_module.find_candidates(library)
    assert "notes" not in {candidate.label for candidate in found}


def test_folders_this_system_writes_to_are_never_scanned(library):
    found = discover_module.find_candidates(library)
    assert "render" not in {candidate.label for candidate in found}


def test_a_parent_whose_clips_are_all_nested_is_not_itself_a_candidate(
    library
):
    """Otherwise the same footage is processed twice under two names."""
    found = discover_module.find_candidates(library)
    assert library.name not in {candidate.label for candidate in found}


def test_discovery_counts_the_files_it_found(library):
    found = discover_module.find_candidates(library)
    assert all(candidate.video_files == 2 for candidate in found)


def test_discovery_is_not_recursive_when_told_not_to_be(library):
    found = discover_module.find_candidates(library, recursive=False)
    assert found == []


def test_a_missing_root_says_so_rather_than_returning_nothing(tmp_path):
    with pytest.raises(FootageError) as caught:
        discover_module.find_candidates(tmp_path / "nope")
    assert "--root" in (caught.value.hint or "")


def test_a_file_as_a_root_is_refused(library):
    with pytest.raises(FootageError) as caught:
        discover_module.find_candidates(library / "ep01" / "a.mp4")
    assert "auto run --folder" in (caught.value.hint or "")


def test_discovery_attaches_existing_runs(config, library):
    run = AutoRunConfig(footage_folder=str(library / "ep01"),
                        style="cinematic_minecraft")
    state = auto_store.create(config, run, "run-existing")
    state.status = "complete"
    auto_store.save(config, state)

    found = discover_module.find_candidates(library, config=config)
    ep01 = next(c for c in found if c.label == "ep01")
    assert ep01.completed_run == "run-existing"
    assert next(c for c in found if c.label == "ep02").completed_run == ""


# ---------------------------------------------------------------------------
# The decision, as a pure function
# ---------------------------------------------------------------------------

def candidate(**overrides) -> BatchCandidate:
    base = {"folder": "/clips/ep01", "label": "ep01", "video_files": 2}
    base.update(overrides)
    return BatchCandidate(**base)


def test_decide_only_ever_returns_a_named_decision():
    """The four are a closed set: a fifth would reach the loop unhandled."""
    cases = [
        (BatchConfig(), candidate()),
        (BatchConfig(), candidate(video_files=0)),
        (BatchConfig(only_new=True),
         candidate(existing_runs=[{"run_id": "r", "status": "failed"}])),
        (BatchConfig(force=True),
         candidate(existing_runs=[{"run_id": "r", "status": "complete"}])),
        (BatchConfig(resume=True),
         candidate(existing_runs=[{"run_id": "r", "status": "failed"}])),
        (BatchConfig(),
         candidate(existing_runs=[{"run_id": "r", "status": "complete"}])),
        (BatchConfig(),
         candidate(existing_runs=[{"run_id": "r", "status": "running"}])),
    ]
    for config, entry in cases:
        decision, _reason, _code = batch_run.decide(config, entry)
        assert decision in batch_run.DECISIONS


def test_a_fresh_folder_is_run():
    decision, _reason, _code = batch_run.decide(
        BatchConfig(), candidate())
    assert decision == "run"


def test_a_folder_with_no_video_is_skipped():
    decision, _reason, code = batch_run.decide(
        BatchConfig(), candidate(video_files=0))
    assert decision == "skip"
    assert code == "no_video_files"


def test_a_completed_run_is_skipped_by_default():
    decision, reason, code = batch_run.decide(
        BatchConfig(),
        candidate(existing_runs=[{"run_id": "r1", "status": "complete"}]))
    assert decision == "skip"
    assert code == "already_completed"
    assert "--force" in reason


def test_force_runs_a_completed_folder_again_in_a_new_run():
    decision, reason, _code = batch_run.decide(
        BatchConfig(force=True),
        candidate(existing_runs=[{"run_id": "r1", "status": "complete"}]))
    assert decision == "force"
    assert "rather than overwriting" in reason


def test_resume_continues_an_unfinished_run():
    decision, _reason, _code = batch_run.decide(
        BatchConfig(resume=True),
        candidate(existing_runs=[{"run_id": "r1", "status": "failed"}]))
    assert decision == "resume"


def test_an_unfinished_run_is_skipped_without_resume():
    decision, reason, code = batch_run.decide(
        BatchConfig(),
        candidate(existing_runs=[{"run_id": "r1", "status": "failed"}]))
    assert decision == "skip"
    assert code == "already_failed"
    assert "--resume" in reason


def test_only_new_skips_anything_that_has_ever_been_run():
    decision, _reason, code = batch_run.decide(
        BatchConfig(only_new=True),
        candidate(existing_runs=[{"run_id": "r1", "status": "failed"}]))
    assert decision == "skip"
    assert code == "not_new"


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def test_a_batch_processes_every_folder(config, library):
    execute = recording_execute()
    summary = batch_run.run_batch(
        config, config_for(library), execute=execute)
    assert len(summary.completed) == 3
    assert len(execute.calls) == 3


def test_a_dry_run_creates_nothing(config, library):
    execute = recording_execute()
    summary = batch_run.run_batch(
        config, config_for(library, dry_run=True), execute=execute)

    assert execute.calls == []
    assert len(summary.of_status("planned")) == 3
    assert not (config.output_dir / "auto" / "runs").exists()


def test_a_dry_run_says_what_it_would_have_done(config, library):
    summary = batch_run.run_batch(
        config, config_for(library, dry_run=True),
        execute=recording_execute())
    text = batch_report.render(summary)
    assert "DRY RUN -- NOTHING WAS CREATED" in text
    assert "ep01" in text


def test_a_completed_run_is_skipped_and_never_re_executed(config, library):
    run = AutoRunConfig(footage_folder=str(library / "ep01"),
                        style="cinematic_minecraft")
    state = auto_store.create(config, run, "run-existing")
    state.status = "complete"
    auto_store.save(config, state)

    execute = recording_execute()
    summary = batch_run.run_batch(
        config, config_for(library), execute=execute)

    touched = {call["folder"] for call in execute.calls}
    assert str(library / "ep01") not in touched
    skipped = summary.entry(str(library / "ep01"))
    assert skipped.status == "skipped"
    assert skipped.skip_reason == "already_completed"
    assert skipped.run_id == "run-existing"


def test_force_asks_for_a_new_run_rather_than_overwriting(config, library):
    run = AutoRunConfig(footage_folder=str(library / "ep01"),
                        style="cinematic_minecraft")
    state = auto_store.create(config, run, "run-existing")
    state.status = "complete"
    auto_store.save(config, state)

    execute = recording_execute()
    batch_run.run_batch(
        config, config_for(library, force=True), execute=execute)
    forced = [call for call in execute.calls
              if call["folder"] == str(library / "ep01")]
    assert forced and forced[0]["decision"] == "force"


def test_a_batch_continues_after_a_folder_fails(config, library):
    execute = recording_execute(fail_on=("ep02",))
    summary = batch_run.run_batch(
        config, config_for(library), execute=execute)

    assert len(summary.failed) == 1
    assert len(summary.completed) == 2
    assert summary.failed[0].label == "ep02"
    assert "cursed" in summary.failed[0].reason
    assert summary.status == "complete_with_failures"


def test_a_failure_is_recorded_with_a_note_rather_than_a_traceback(
    config, library
):
    summary = batch_run.run_batch(
        config, config_for(library),
        execute=recording_execute(fail_on=("ep02",)))
    entry = summary.failed[0]
    assert "Traceback" not in entry.reason
    assert entry.warnings


def test_the_limit_stops_after_that_many_folders(config, library):
    execute = recording_execute()
    summary = batch_run.run_batch(
        config, config_for(library, limit=2), execute=execute)
    assert len(execute.calls) == 2
    limited = [e for e in summary.skipped
               if e.skip_reason == "limit_reached"]
    assert len(limited) == 1


def test_every_folder_gets_the_same_configuration(config, library):
    execute = recording_execute()
    batch_run.run_batch(
        config,
        config_for(library, director=True, retention_cut=True,
                   render_proxy=True, captions="key_moments",
                   audio_polish="placeholders"),
        execute=execute)
    for call in execute.calls:
        run_config = call["config"]
        assert run_config.style == "cinematic_minecraft"
        assert run_config.director is True
        assert run_config.retention_cut is True
        assert run_config.render_proxy is True
        assert run_config.captions == "key_moments"
        assert run_config.audio_polish == "placeholders"
        assert run_config.no_premiere is True


def test_a_batch_that_reshapes_the_cut_actually_applies_it(config, library):
    """report_only across forty folders is forty decisions and no edit."""
    execute = recording_execute()
    batch_run.run_batch(
        config, config_for(library, retention_cut=True), execute=execute)
    assert execute.calls[0]["config"].retention_mode == "retention"


def test_a_batch_that_does_not_reshape_leaves_the_mode_alone(config, library):
    execute = recording_execute()
    batch_run.run_batch(config, config_for(library), execute=execute)
    assert execute.calls[0]["config"].retention_mode == "report_only"


def test_a_root_with_no_footage_is_a_warning_not_a_crash(config, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    summary = batch_run.run_batch(
        config, config_for(empty), execute=recording_execute())
    assert summary.entries == []
    assert any("directly contains video files" in w for w in summary.warnings)


# ---------------------------------------------------------------------------
# Recording what happened
# ---------------------------------------------------------------------------

def test_a_run_that_failed_stages_is_still_a_completed_entry(config, library):
    """The batch asked for it to be run, and it was."""
    states = [
        make_state("run-a", status="failed",
                   stages=[("analyze", "failed", {})]),
        make_state("run-b"), make_state("run-c"),
    ]
    summary = batch_run.run_batch(
        config, config_for(library), execute=recording_execute(states))
    entry = summary.entries[0]
    assert entry.status == "completed"
    assert entry.run_status == "failed"
    assert entry.stages_failed == 1
    assert entry.produced_an_edit is False


def test_the_entry_records_the_video_and_the_checks(config, library):
    states = [
        make_state("run-a", stages=[
            ("render_proxy", "passed", {"video": "/runs/a/render.mp4"}),
            ("reliability_gates", "passed",
             {"status": "warn", "blocking": 0}),
        ]),
        make_state("run-b"), make_state("run-c"),
    ]
    summary = batch_run.run_batch(
        config, config_for(library), execute=recording_execute(states))
    entry = summary.entries[0]
    assert entry.video_path == "/runs/a/render.mp4"
    assert entry.checks_status == "warn"
    assert entry.report_path


def test_a_run_whose_checks_say_it_is_unusable_is_flagged(config, library):
    states = [
        make_state("run-a", stages=[
            ("reliability_gates", "passed",
             {"status": "fail", "blocking": 1}),
        ]),
        make_state("run-b"), make_state("run-c"),
    ]
    summary = batch_run.run_batch(
        config, config_for(library), execute=recording_execute(states))
    assert summary.entries[0].checks_blocking == 1
    assert summary.stats()["with_blocking_checks"] == 1
    assert "WORTH LOOKING AT" in batch_report.render(summary)


# ---------------------------------------------------------------------------
# Storage and reporting
# ---------------------------------------------------------------------------

def test_the_summary_is_written_as_it_goes(config, library):
    summary = batch_run.run_batch(
        config, config_for(library), execute=recording_execute())
    target = batch_store.summary_path(config, summary.batch_id)
    assert target.exists()
    assert batch_store.report_path(config, summary.batch_id).exists()


def test_a_summary_survives_a_round_trip(config, library):
    summary = batch_run.run_batch(
        config, config_for(library), execute=recording_execute())
    restored = batch_store.load(config, summary.batch_id)
    assert restored.stats() == summary.stats()
    assert [e.folder for e in restored.entries] == \
        [e.folder for e in summary.entries]


def test_batches_are_listed_newest_first(config, library):
    first = batch_run.run_batch(
        config, config_for(library), execute=recording_execute())
    second = batch_run.run_batch(
        config, config_for(library, force=True), execute=recording_execute())
    listed = batch_store.list_batches(config)
    assert len(listed) >= 2
    ids = [entry["batch_id"] for entry in listed]
    assert ids.index(second.batch_id) <= ids.index(first.batch_id)


def test_a_missing_batch_says_how_to_list_them(config):
    from editing.errors import EditingError

    with pytest.raises(EditingError) as caught:
        batch_store.load(config, "nope")
    assert "list-batches" in (caught.value.hint or "")


def test_two_batches_over_one_root_get_different_ids(tmp_path):
    import time

    config = BatchConfig(root=str(tmp_path))
    first = batch_id_for(config, when=time.time())
    second = batch_id_for(config, when=time.time() + 61)
    assert first != second


def test_the_report_leads_with_failures(config, library):
    summary = batch_run.run_batch(
        config, config_for(library), execute=recording_execute(
            fail_on=("ep02",)))
    text = batch_report.render(summary)
    assert "FAILED (1)" in text
    assert text.index("FAILED (1)") < text.index("EVERY FOLDER")


def test_the_report_never_claims_the_edits_are_good(config, library):
    summary = batch_run.run_batch(
        config, config_for(library), execute=recording_execute())
    text = batch_report.render(summary).lower()
    assert "not that the edit is any good" in text
    assert "guaranteed" not in text


def test_the_report_offers_a_resume_after_a_failure(config, library):
    summary = batch_run.run_batch(
        config, config_for(library), execute=recording_execute(
            fail_on=("ep02",)))
    commands = " ".join(batch_report.next_commands(summary))
    assert "--resume" in commands


def test_a_summary_with_no_entries_still_renders():
    summary = BatchSummary(batch_id="b1", config=BatchConfig(root="/nope"))
    assert "BATCH -- b1" in batch_report.render(summary)
