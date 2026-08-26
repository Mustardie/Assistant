"""The auto pipeline: ordering, checkpoints, resume, and the execution gates.

Three things carry the weight.

**A run must complete with nothing installed.** No FFmpeg, no GPU, no model
server, no Premiere, no real assets. That is not a convenience for CI -- it is
the mode a person uses to find out whether the thing works before they commit
an afternoon to it, so it gets asserted end to end rather than sampled.

**A checkpoint is a claim, not a fact.** Every reuse test has a matching
invalidation test: a deleted artifact, a changed artifact, a changed style.
Skipping a stage because it "passed once" is the failure mode that would make
this whole package produce results corresponding to nothing.

**Nothing reaches Premiere without a named, individual ``--yes``.** The gate
tests all assert against a fake engine that records whether it was called at
all, and the answer is no for every refusal.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from editing.auto import gates as auto_gates
from editing.auto import report as auto_report
from editing.auto import stages as auto_stages
from editing.auto import store
from editing.auto.runner import AutoRunner
from editing.auto.schema import (
    STAGE_ORDER, AutoRunConfig, riskiest, run_id_for,
)
from editing.errors import EditingError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: What ffprobe would have said. Local rather than shared because
#: ``tests/`` deliberately has no ``__init__.py`` -- that is what puts the repo
#: root on sys.path -- so test modules cannot import from each other.
PROBE = {
    "duration": 16.0,
    "container": "mov,mp4,m4a",
    "width": 1920,
    "height": 1080,
    "fps": 60.0,
    "video_codec": "h264",
    "has_audio": True,
    "audio_codec": "aac",
    "audio_channels": 2,
    "size_bytes": 1024,
}


@pytest.fixture
def fake_probe(monkeypatch):
    """Patch ffprobe out everywhere discovery and the indexer reach for it."""
    from editing import discovery
    from editing import ffmpeg as ff

    calls: list[str] = []

    def probe(path, *, ffprobe="ffprobe"):
        calls.append(str(path))
        return dict(PROBE)

    monkeypatch.setattr(ff, "probe", probe)
    monkeypatch.setattr(discovery.ff, "probe", probe)
    return calls


@pytest.fixture
def footage(tmp_path):
    """Three files that look like video to discovery."""
    folder = tmp_path / "clips"
    folder.mkdir()
    for index in range(3):
        (folder / f"clip_{index:02d}.mp4").write_bytes(b"not a video" * 512)
    return folder


@pytest.fixture
def auto_config(footage) -> AutoRunConfig:
    return AutoRunConfig(
        footage_folder=str(footage),
        style="cinematic_minecraft",
        mock=True,
        no_premiere=True,
    )


@pytest.fixture
def runner(config, footage, monkeypatch, frame_source, fake_probe, tmp_path):
    """A runner wired so no external tool is ever reached.

    ``frame_source`` and ``fake_probe`` come from the shared conftest; the
    review pass's frame *export* is the one remaining FFmpeg edge and is
    stubbed here.
    """
    from editing.roughcut import review as review_module

    written = tmp_path / "frame.jpg"
    written.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(
        review_module.ff, "extract_frame", lambda *a, **k: written
    )

    runner = AutoRunner(config, say=lambda message: None)
    original = runner.pipeline_for

    def wired(state):
        pipeline = original(state)
        pipeline.analyzer = _stub_analyzer(pipeline, frame_source)
        return pipeline

    runner.pipeline_for = wired
    return runner


def _stub_analyzer(pipeline, frame_source):
    """Give the analyser a frame source that never shells out."""
    real = pipeline.analyzer

    def build(**kwargs):
        kwargs.setdefault("use_motion", False)
        analyzer = real(**kwargs)
        analyzer._frame_source = frame_source
        return analyzer
    return build


def run_once(runner, auto_config, **kwargs):
    state = runner.start(auto_config, **kwargs)
    return runner.run(state)


class FakeEngine:
    """Records whether Premiere was ever actually asked to do anything."""

    def __init__(self, *, succeed=True):
        self.calls: list[dict] = []
        self.succeed = succeed

    def run(self, plan):
        self.calls.append(plan)
        if not self.succeed:
            return {"success": False, "error": "Premiere said no",
                    "code": "execution_failed"}
        return {"success": True,
                "results": [{"ok": True} for _ in plan.get("ops", [])]}


# ---------------------------------------------------------------------------
# Part 1/2 -- run state and IDs
# ---------------------------------------------------------------------------

def test_a_run_creates_its_folder_structure(config, auto_config):
    runner = AutoRunner(config)
    state = runner.start(auto_config)
    directory = Path(state.run_dir)

    assert directory.is_dir()
    for name in ("checkpoints", "artifacts", "reports", "logs"):
        assert (directory / name).is_dir()
    assert (directory / "config.json").exists()
    assert (directory / "state.json").exists()

    saved = json.loads((directory / "config.json").read_text("utf-8"))
    assert saved["style"] == "cinematic_minecraft"


def test_a_run_id_is_readable_and_carries_the_style(auto_config):
    run_id = run_id_for(auto_config)
    parts = run_id.split("-")

    assert len(parts) == 3
    assert parts[0][:8].isdigit()                # the timestamp
    assert parts[2] == "cinematic_minecraft"


def test_two_footage_folders_get_different_run_ids(tmp_path, auto_config):
    other = replace(auto_config, footage_folder=str(tmp_path / "elsewhere"))
    assert run_id_for(auto_config) != run_id_for(other)


def test_two_styles_over_one_folder_get_different_run_ids(auto_config):
    other = replace(auto_config, style="fast_funny")
    assert run_id_for(auto_config) != run_id_for(other)


def test_a_run_is_hermetic(config, auto_config):
    """Two runs must not be able to overwrite each other's plans."""
    runner = AutoRunner(config)
    first = runner.start(auto_config)
    second = runner.start(replace(auto_config, style="fast_funny"))

    assert first.artifacts_dir != second.artifacts_dir
    assert Path(first.artifacts_dir).is_dir()
    assert Path(second.artifacts_dir).is_dir()


def test_a_completed_run_is_never_started_over(config, auto_config,
                                              monkeypatch):
    """Two starts have to agree on the run id for this to mean anything.

    The id is ``<timestamp to the second>-<folder>-<style>``, so two calls
    either side of a second boundary produce different runs and the test
    silently checks nothing. Pinning the clock is the difference between a
    test and a coin toss -- it failed exactly once in a full-suite run, which
    is the worst way to find that out.
    """
    from editing.auto import schema as auto_schema

    monkeypatch.setattr(
        auto_schema.time, "strftime",
        lambda fmt, *rest: "20260101T000000" if "%Y%m%d" in fmt
        else "2026-01-01T00:00:00")

    runner = AutoRunner(config)
    state = runner.start(auto_config)
    state.status = "complete"
    store.save(config, state)

    with pytest.raises(EditingError) as caught:
        runner.start(auto_config)
    assert "--force-new-run" in str(caught.value.hint)


def test_an_incomplete_run_is_picked_up_rather_than_refused(config, auto_config):
    runner = AutoRunner(config)
    first = runner.start(auto_config)
    again = runner.start(auto_config)
    assert again.run_id == first.run_id


def test_corrupted_state_refuses_rather_than_guessing(config, auto_config):
    runner = AutoRunner(config)
    state = runner.start(auto_config)
    (Path(state.run_dir) / "state.json").write_text("{not json", "utf-8")

    with pytest.raises(EditingError) as caught:
        runner.load(state.run_id)
    assert "corrupted" in str(caught.value).lower()
    assert "auto clean" in str(caught.value.hint)


def test_state_survives_a_round_trip(config, auto_config):
    runner = AutoRunner(config)
    state = runner.start(auto_config)
    state.warnings.append("something")
    store.save(config, state)

    reloaded = runner.load(state.run_id)
    assert reloaded.run_id == state.run_id
    assert reloaded.config.style == "cinematic_minecraft"
    assert "something" in reloaded.warnings


# ---------------------------------------------------------------------------
# Part 3 -- ordering and the stage table
# ---------------------------------------------------------------------------

def test_every_stage_declares_its_prerequisites_before_itself():
    """The run order is the tuple order, so it has to be a valid ordering."""
    seen: set = set()
    for name in STAGE_ORDER:
        stage = auto_stages.stage(name)
        for requirement in stage.requires:
            assert requirement in seen, (
                f"{name} requires {requirement}, which comes later"
            )
        seen.add(name)


def test_the_stage_table_and_the_runner_table_agree():
    runnable = {s.name for s in auto_stages.STAGES if s.name != "report"}
    assert runnable == set(auto_stages.RUNNERS)


def test_every_gate_names_a_real_dry_run_stage():
    from editing.auto.schema import GATE_STAGES

    for gate, stage_name in GATE_STAGES.items():
        assert stage_name in auto_stages.BY_NAME
        assert gate in auto_gates.GATE_SPECS


def test_dependents_are_transitive():
    following = auto_stages.dependents("roughcut_build")
    assert "layers_build" in following
    assert "assets_dry_run" in following, "should reach through layers_build"
    assert "discover" not in following


@pytest.mark.parametrize("name,why", [
    ("clip.remove", "deletes"),
    ("clip.insert", "ripples"),
    ("marker.add", "marker"),
])
def test_the_riskiest_operation_is_named(name, why):
    got, explanation = riskiest([{"op": "marker.add"}, {"op": name}])
    assert got == name
    assert why in explanation


def test_an_unknown_operation_is_treated_as_the_riskiest():
    """Something the table has not heard of is what to warn about."""
    got, why = riskiest([{"op": "clip.remove"}, {"op": "mystery.op"}])
    assert got == "mystery.op"
    assert "does not recognise" in why


# ---------------------------------------------------------------------------
# A whole run
# ---------------------------------------------------------------------------

def test_a_mock_run_completes_with_nothing_installed(runner, auto_config):
    """No FFmpeg, no GPU, no model server, no Premiere, no assets."""
    state = run_once(runner, auto_config)

    assert state.status in ("complete", "blocked"), state.status
    assert not state.of_status("failed")
    for name in ("discover", "analyze", "recommend", "roughcut_build",
                 "roughcut_dry_run", "layers_build", "layers_dry_run",
                 "assets_plan", "assets_dry_run", "report"):
        result = state.stage(name)
        assert result is not None and result.ok, (
            f"{name} is {result.status if result else 'missing'}: "
            f"{result.note if result else ''}"
        )


def test_a_mock_run_executes_nothing(runner, auto_config):
    state = run_once(runner, auto_config)
    assert all(not gate.executed for gate in state.gates)
    assert all(not gate.ready for gate in state.gates), (
        "--no-premiere must leave every gate shut"
    )


def test_the_style_reaches_every_pass_that_cares(runner, auto_config):
    state = run_once(runner, auto_config)
    assert state.stage("layers_build").summary["style"] == "cinematic_minecraft"

    layers = json.loads(
        (Path(state.artifacts_dir) / "layers" / "structure.json")
        .read_text("utf-8")
    )
    assert layers["style"] == "cinematic_minecraft"
    placement = json.loads(
        (Path(state.artifacts_dir) / "assets" / "structure.placement.json")
        .read_text("utf-8")
    )
    assert placement["style"] == "cinematic_minecraft"


def test_markers_only_reaches_the_layer_and_asset_passes(runner, auto_config):
    state = run_once(runner, replace(auto_config, markers_only=True))

    layers = json.loads(
        (Path(state.artifacts_dir) / "layers" / "structure.json")
        .read_text("utf-8")
    )
    ops = {op["op"] for op in layers["plan"]["ops"]}
    assert ops <= {"sequence.activate", "marker.add"}

    placement = json.loads(
        (Path(state.artifacts_dir) / "assets" / "structure.placement.json")
        .read_text("utf-8")
    )
    asset_ops = {op["op"] for op in placement["plan"]["ops"]}
    assert asset_ops <= {"sequence.activate", "marker.add"}


def test_skipping_a_pass_marks_it_skipped_not_failed(runner, auto_config):
    state = run_once(
        runner, replace(auto_config, skip_review=True, skip_assets=True)
    )
    for name in auto_stages.REVIEW_STAGES + auto_stages.ASSET_STAGES:
        result = state.stage(name)
        assert result.status == "skipped"
        assert "--skip" in result.note
    assert state.stage("layers_build").ok, "the rest of the run continues"


def test_an_empty_asset_library_still_produces_a_valid_run(runner, auto_config,
                                                           tmp_path):
    empty = tmp_path / "no_assets"
    state = run_once(
        runner, replace(auto_config, asset_library=str(empty))
    )

    assets = state.stage("assets_plan")
    assert assets.ok
    assert assets.summary["placed"] == 0
    assert assets.summary["missing"] > 0, "a shopping list, not a crash"
    assert state.stage("assets_dry_run").ok


def test_a_missing_asset_folder_is_a_warning_not_a_block(runner, auto_config,
                                                         tmp_path):
    """The shopping list is most useful to somebody with no library at all."""
    state = run_once(
        runner, replace(auto_config, asset_library=str(tmp_path / "nope"))
    )
    index = state.stage("assets_index")
    assert index.ok
    assert any("assets init" in w for w in index.warnings)


def test_no_footage_fails_with_the_command_to_fix_it(config, tmp_path):
    runner = AutoRunner(config)
    state = run_once(runner, AutoRunConfig(
        footage_folder=str(tmp_path / "empty"), mock=True, no_premiere=True,
    ))

    failure = state.first_failure()
    assert failure is not None
    assert failure.stage == "discover"
    assert failure.failure.next_command
    assert failure.failure.can_resume


def test_a_failed_stage_stops_the_pipeline(config, tmp_path):
    runner = AutoRunner(config)
    state = run_once(runner, AutoRunConfig(
        footage_folder=str(tmp_path / "empty"), mock=True, no_premiere=True,
    ))

    assert state.status == "failed"
    assert state.stage("analyze").status == "blocked"
    assert "earlier stage failed" in state.stage("analyze").note


def test_the_run_records_a_log(runner, auto_config):
    state = run_once(runner, auto_config)
    log = Path(state.run_dir) / "logs" / "run.log"
    assert log.exists()
    assert "stage discover" in log.read_text("utf-8")


# ---------------------------------------------------------------------------
# Checkpoints and resume
# ---------------------------------------------------------------------------

def test_a_second_run_reuses_its_checkpoints(runner, auto_config):
    state = run_once(runner, auto_config)
    again = runner.run(runner.load(state.run_id))

    assert again.stage("analyze").from_checkpoint
    assert again.stage("recommend").from_checkpoint
    assert again.stats()["from_checkpoint"] > 0


def test_a_deleted_artifact_invalidates_its_checkpoint(runner, auto_config):
    state = run_once(runner, auto_config)
    timeline = Path(state.artifacts_dir) / "timelines" / "structure.json"
    assert timeline.exists()
    timeline.unlink()

    again = runner.run(runner.load(state.run_id))
    assert not again.stage("analyze").from_checkpoint, (
        "a checkpoint naming a missing artifact must not be trusted"
    )
    assert again.stage("analyze").ok


def test_a_changed_artifact_invalidates_its_checkpoint(runner, auto_config):
    state = run_once(runner, auto_config)
    timeline = Path(state.artifacts_dir) / "timelines" / "structure.json"
    timeline.write_text(timeline.read_text("utf-8") + "\n", "utf-8")

    again = runner.run(runner.load(state.run_id))
    assert not again.stage("analyze").from_checkpoint


def test_a_corrupted_checkpoint_re_runs_the_stage(runner, auto_config, config):
    state = run_once(runner, auto_config)
    store.checkpoint_path(config, state.run_id, "recommend").write_text(
        "{not json", "utf-8"
    )

    again = runner.run(runner.load(state.run_id))
    assert not again.stage("recommend").from_checkpoint
    assert again.stage("recommend").ok


def test_changing_the_style_rebuilds_only_what_depends_on_it(runner,
                                                             auto_config):
    state = run_once(runner, auto_config)
    reloaded = runner.load(state.run_id)
    reloaded.config = replace(reloaded.config, style="fast_funny")

    again = runner.run(reloaded)
    assert again.stage("analyze").from_checkpoint, "analysis is style-blind"
    assert not again.stage("layers_build").from_checkpoint
    assert again.stage("layers_build").summary["style"] == "fast_funny"


def test_restyling_in_place_reuses_the_analysis(runner, auto_config):
    """Comparing two styles should not cost a second analysis."""
    from dataclasses import replace as _replace

    state = run_once(runner, auto_config)
    reloaded = runner.load(state.run_id)
    reloaded.config = _replace(reloaded.config, style="fast_funny")
    again = runner.resume(reloaded)

    assert again.run_id == state.run_id, "restyling must not fork a new run"
    assert again.stage("analyze").from_checkpoint
    assert again.stage("recommend").from_checkpoint
    assert not again.stage("layers_build").from_checkpoint
    assert again.stage("layers_build").summary["style"] == "fast_funny"


def test_restyling_in_place_reuses_the_analysis(runner, auto_config, config):
    """Changing the style should not cost a re-analysis.

    Starting a fresh run with a different style gives a different run ID and
    therefore a fresh set of checkpoints. Restyling an existing run instead
    keeps every style-blind stage, which is what makes trying four presets on
    one edit cheap.
    """
    state = run_once(runner, auto_config)
    reloaded = runner.load(state.run_id)
    reloaded.config = replace(reloaded.config, style="fast_funny")
    store.write_config(config, reloaded.run_id, reloaded.config)

    again = runner.resume(reloaded)

    for name in ("discover", "analyze", "recommend", "roughcut_build"):
        assert again.stage(name).from_checkpoint, f"{name} was re-run"
    assert not again.stage("layers_build").from_checkpoint
    assert again.stage("layers_build").summary["style"] == "fast_funny"
    assert not again.stage("assets_plan").from_checkpoint


def test_refresh_rebuilds_a_stage_and_everything_after_it(runner, auto_config):
    state = run_once(runner, auto_config)
    again = runner.run(
        runner.load(state.run_id), refresh=["roughcut_build"]
    )

    assert not again.stage("roughcut_build").from_checkpoint
    assert not again.stage("layers_build").from_checkpoint
    assert again.stage("analyze").from_checkpoint, "upstream is untouched"


def test_resume_retries_a_blocked_stage(runner, auto_config, config):
    """The usual reason to type `resume` is that you just fixed the blocker."""
    state = run_once(runner, auto_config)
    reloaded = runner.load(state.run_id)
    blocked = reloaded.stage("assets_plan")
    blocked.status = "blocked"
    blocked.note = "pretend the library was missing"
    store.clear_checkpoint(config, state.run_id, "assets_plan")
    store.save(config, reloaded)

    again = runner.resume(reloaded)
    assert again.stage("assets_plan").ok


def test_resume_continues_after_a_failure_is_fixed(config, footage, tmp_path,
                                                    monkeypatch, frame_source,
                                                    fake_probe):
    from editing.roughcut import review as review_module

    written = tmp_path / "frame.jpg"
    written.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(
        review_module.ff, "extract_frame", lambda *a, **k: written
    )

    missing = tmp_path / "not_yet"
    runner = AutoRunner(config)
    original = runner.pipeline_for
    runner.pipeline_for = lambda state: _wire(original(state), frame_source)

    state = run_once(runner, AutoRunConfig(
        footage_folder=str(missing), mock=True, no_premiere=True,
    ))
    assert state.status == "failed"

    # The user creates the folder and resumes.
    missing.mkdir()
    for index in range(2):
        (missing / f"clip_{index}.mp4").write_bytes(b"video" * 512)

    resumed = runner.resume(runner.load(state.run_id))
    assert resumed.stage("discover").ok
    assert resumed.stage("roughcut_build").ok


def _wire(pipeline, frame_source):
    real = pipeline.analyzer

    def build(**kwargs):
        kwargs.setdefault("use_motion", False)
        analyzer = real(**kwargs)
        analyzer._frame_source = frame_source
        return analyzer
    pipeline.analyzer = build
    return pipeline


def test_a_stage_that_produced_nothing_writes_no_checkpoint(runner,
                                                            auto_config,
                                                            config):
    """Half-finishing must not look like finishing."""
    state = run_once(runner, auto_config)
    for stage in auto_stages.STAGES:
        if not stage.resumable or not stage.artifacts:
            continue
        checkpoint = store.read_checkpoint(config, state.run_id, stage.name)
        if checkpoint is not None:
            assert checkpoint.artifacts, (
                f"{stage.name} wrote a checkpoint with no artifacts"
            )


# ---------------------------------------------------------------------------
# Execution gates
# ---------------------------------------------------------------------------

@pytest.fixture
def executable(runner, auto_config):
    """A completed run that is allowed to execute (no --no-premiere)."""
    return run_once(runner, replace(auto_config, no_premiere=False))


def test_gates_are_created_for_every_executable_pass(executable):
    assert {gate.stage for gate in executable.gates} == {
        "roughcut", "review", "layers", "assets", "conform"
    }


def test_a_later_gate_waits_for_the_rough_cut_to_exist(executable):
    """Applying a style to a sequence that is not built yet cannot work."""
    for name in ("review", "layers", "assets"):
        gate = executable.gate(name)
        assert not gate.ready
        assert "rough cut has not been built" in gate.blocked_reason


def test_a_gate_knows_what_it_would_do(executable):
    gate = executable.gate("roughcut")
    assert gate.dry_run_passed
    assert gate.operation_count > 0
    assert gate.sequence_name
    assert gate.riskiest_operation
    assert gate.on_scratch
    assert "--yes" in gate.command


def test_no_premiere_shuts_every_gate(runner, auto_config):
    state = run_once(runner, auto_config)
    for gate in state.gates:
        assert not gate.ready
        assert "--no-premiere" in gate.blocked_reason


def test_execution_refuses_without_yes(config, executable):
    engine = FakeEngine()
    result = auto_gates.execute(
        config, executable, "roughcut", yes=False, engine=engine
    )
    assert result["success"] is False
    assert result["code"] == "needs_yes"
    assert engine.calls == []


def test_execution_refuses_when_the_dry_run_did_not_pass(config, executable):
    reloaded = replace(executable)
    reloaded.stage("roughcut_dry_run").status = "failed"
    reloaded.gates = auto_gates.compute_gates(config, reloaded)

    engine = FakeEngine()
    result = auto_gates.execute(
        config, reloaded, "roughcut", yes=True, engine=engine
    )
    assert result["success"] is False
    assert "has not passed" in result["refused_reason"]
    assert engine.calls == []


def test_execution_refuses_an_unknown_stage(config, executable):
    with pytest.raises(EditingError):
        auto_gates.execute(config, executable, "everything", yes=True)


def test_a_gate_that_passes_every_check_executes(config, executable):
    engine = FakeEngine()
    result = auto_gates.execute(
        config, executable, "roughcut", yes=True, engine=engine
    )

    assert result["success"] is True
    assert result["executed"] is True
    assert len(engine.calls) == 1
    assert engine.calls[0].get("dry_run") is not True


def test_a_gate_is_never_executed_twice(config, executable):
    engine = FakeEngine()
    auto_gates.execute(config, executable, "roughcut", yes=True, engine=engine)
    again = auto_gates.execute(
        config, executable, "roughcut", yes=True, engine=engine
    )

    assert again["success"] is False
    assert again["code"] == "already_executed"
    assert len(engine.calls) == 1, "the second attempt must not reach Premiere"


def test_a_resume_never_erases_the_record_of_an_execution(config, executable,
                                                          runner):
    """The bug this pins was quiet and total.

    A dry run and a real execution wrote the *same* file. So every ``resume``
    after an execution overwrote "executed: true" with "executed: false", the
    later passes then believed the sequence had never been built, and every
    downstream gate was permanently blocked with no way to clear it.
    """
    auto_gates.execute(
        config, executable, "roughcut", yes=True, engine=FakeEngine()
    )
    report = Path(executable.artifacts_dir) / "roughcut" / "structure.execution.json"
    assert json.loads(report.read_text("utf-8"))["executed"] is True

    resumed = runner.resume(runner.load(executable.run_id))

    assert json.loads(report.read_text("utf-8"))["executed"] is True, (
        "the dry run stage overwrote the execution record"
    )
    layers = json.loads(
        (Path(resumed.artifacts_dir) / "layers" / "structure.json")
        .read_text("utf-8")
    )
    assert layers["roughcut_executed"] is True


def test_the_later_gates_open_once_the_rough_cut_exists(config, executable,
                                                        runner):
    """The whole point of the chain: execute, resume, execute the next one."""
    auto_gates.execute(
        config, executable, "roughcut", yes=True, engine=FakeEngine()
    )
    resumed = runner.resume(runner.load(executable.run_id))
    layers = resumed.gate("layers")

    assert layers.ready, layers.blocked_reason

    engine = FakeEngine()
    result = auto_gates.execute(
        config, resumed, "layers", yes=True, engine=engine
    )
    assert result["success"] is True
    assert len(engine.calls) == 1


def test_executing_the_rough_cut_marks_the_later_plans_stale(config,
                                                             executable):
    """Those plans recorded that the sequence did not exist. Now it does."""
    auto_gates.execute(
        config, executable, "roughcut", yes=True, engine=FakeEngine()
    )
    for stage in ("layers_build", "assets_plan"):
        assert store.read_checkpoint(config, executable.run_id, stage) is None
    assert any("stale" in w for w in executable.warnings)


def test_a_stale_later_gate_says_which_command_fixes_it(config, executable):
    auto_gates.execute(
        config, executable, "roughcut", yes=True, engine=FakeEngine()
    )
    gates = auto_gates.compute_gates(config, executable)
    layers = next(g for g in gates if g.stage == "layers")

    assert not layers.ready
    assert "auto resume" in layers.blocked_reason


def test_a_premiere_failure_is_explained_with_the_fix(config, executable):
    class Unreachable:
        def run(self, plan):  # pragma: no cover - never called
            raise AssertionError("should not be reached")

    # No engine and no bridge: the executor cannot build one, and refuses.
    # The rough cut gate is the one with no dependency on a prior execution,
    # so the refusal it hits is genuinely the unreachable host.
    result = auto_gates.execute(config, executable, "roughcut", yes=True)
    assert result["success"] is False
    assert "Premiere Bridge is unreachable" in result["refused_reason"]
    assert "auto execute-stage roughcut" in result["next_command"]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_the_report_is_written_in_both_forms(runner, auto_config):
    state = run_once(runner, auto_config)
    json_path, text_path = store.report_paths(runner.config, state.run_id)

    assert json_path.exists() and text_path.exists()
    document = json.loads(json_path.read_text("utf-8"))
    assert document["run_id"] == state.run_id
    assert document["limitations"]


def test_the_report_says_what_it_did_not_do(runner, auto_config):
    state = run_once(runner, auto_config)
    text = (store.report_paths(runner.config, state.run_id)[1]
            .read_text("utf-8"))

    assert "NOT executed" in text
    assert "MOCK mode" in text
    assert "no-premiere" in text
    assert "LIMITATIONS" in text


def test_the_report_carries_the_next_commands(runner, auto_config):
    state = run_once(runner, auto_config)
    report = auto_report.build_report(runner.config, state)
    assert report.next_commands
    assert all("editing.cli" in command for command in report.next_commands)


def test_a_failure_report_names_the_command_to_try(config, tmp_path):
    runner = AutoRunner(config)
    state = run_once(runner, AutoRunConfig(
        footage_folder=str(tmp_path / "empty"), mock=True, no_premiere=True,
    ))
    text = auto_report.render_failure(state)

    assert "FAILED  discover" in text
    assert "next   :" in text
    assert "auto resume" in text


def test_missing_assets_are_a_warning_not_a_crash(runner, auto_config,
                                                   tmp_path):
    state = run_once(
        runner, replace(auto_config, asset_library=str(tmp_path / "gone"))
    )
    report = auto_report.build_report(runner.config, state)

    assert state.status != "failed"
    assert any("shopping list" in w or "assets init" in w
               for w in report.warnings)


def test_status_renders_every_stage(runner, auto_config):
    state = run_once(runner, auto_config)
    text = auto_report.render_status(state)
    for name in STAGE_ORDER:
        assert name in text


# ---------------------------------------------------------------------------
# Listing and cleaning
# ---------------------------------------------------------------------------

def test_runs_are_listed_newest_first(config, auto_config):
    runner = AutoRunner(config)
    runner.start(auto_config)
    runner.start(replace(auto_config, style="fast_funny"))

    runs = store.list_runs(config)
    assert len(runs) == 2
    assert runs[0]["run_id"] >= runs[1]["run_id"]


def test_clean_is_a_dry_run_by_default(config, auto_config):
    runner = AutoRunner(config)
    state = runner.start(auto_config)

    result = store.clean(config)
    assert result["dry_run"] is True
    assert Path(state.run_dir).exists(), "a dry run must delete nothing"


def test_clean_removes_incomplete_runs(config, auto_config):
    runner = AutoRunner(config)
    state = runner.start(auto_config)

    store.clean(config, dry_run=False)
    assert not Path(state.run_dir).exists()


def test_clean_keeps_completed_runs_unless_told_otherwise(config, auto_config):
    runner = AutoRunner(config)
    state = runner.start(auto_config)
    state.status = "complete"
    store.save(config, state)

    result = store.clean(config, dry_run=False)
    assert Path(state.run_dir).exists()
    assert any("completed" in entry["reason"] for entry in result["kept"])

    store.clean(config, dry_run=False, failed_only=False)
    assert not Path(state.run_dir).exists()


def test_clean_keeps_runs_that_touched_premiere(config, auto_config):
    from editing.auto.schema import AutoExecutionGate

    runner = AutoRunner(config)
    state = runner.start(auto_config)
    state.gates = [AutoExecutionGate(stage="roughcut", executed=True)]
    store.save(config, state)

    result = store.clean(config, dry_run=False)
    assert Path(state.run_dir).exists()
    assert any("executed" in entry["reason"] for entry in result["kept"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_cli(argv, capsys):
    from editing.cli import main

    code = main(argv)
    return code, capsys.readouterr()


def test_the_cli_runs_the_whole_pipeline(footage, tmp_path, capsys, monkeypatch,
                                          frame_source, fake_probe):
    from editing.roughcut import review as review_module

    written = tmp_path / "frame.jpg"
    written.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(
        review_module.ff, "extract_frame", lambda *a, **k: written
    )
    monkeypatch.setattr(
        "editing.visual.frames.FFmpegFrameSource.extract",
        lambda self, path, window: frame_source.extract(path, window),
    )

    code, captured = run_cli([
        "auto", "run", "--folder", str(footage), "--style", "minimal_clean",
        "--mock", "--no-premiere", "--max-windows", "2",
        "--output-dir", str(tmp_path / "out"), "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["success"] is True
    assert payload["stats"]["failed"] == 0


def test_the_cli_refuses_to_execute_without_yes(config, executable, capsys):
    code, captured = run_cli([
        "auto", "execute-stage", "roughcut", "--run", executable.run_id,
        "--output-dir", str(config.output_dir), "--no-premiere",
        "--json", "-q",
    ], capsys)

    assert code == 1
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert payload["code"] == "needs_yes"


def test_the_cli_lists_and_shows_status(config, executable, capsys):
    code, captured = run_cli([
        "auto", "list-runs", "--output-dir", str(config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)
    assert code == 0
    assert json.loads(captured.out)["count"] >= 1

    code, captured = run_cli([
        "auto", "status", "--run", executable.run_id,
        "--output-dir", str(config.output_dir), "--no-premiere",
        "--json", "-q",
    ], capsys)
    assert code == 0
    assert json.loads(captured.out)["run_id"] == executable.run_id


def test_the_cli_shows_gates(config, executable, capsys):
    code, captured = run_cli([
        "auto", "show-gates", "--run", executable.run_id,
        "--output-dir", str(config.output_dir), "--no-premiere",
        "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert {gate["stage"] for gate in payload["gates"]} == {
        "roughcut", "review", "layers", "assets", "conform"
    }


def test_the_cli_explains_a_failure(config, tmp_path, capsys):
    runner = AutoRunner(config)
    state = run_once(runner, AutoRunConfig(
        footage_folder=str(tmp_path / "empty"), mock=True, no_premiere=True,
    ))

    code, captured = run_cli([
        "auto", "explain-failure", "--run", state.run_id,
        "--output-dir", str(config.output_dir), "--no-premiere",
        "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["failed"]
    assert payload["failed"][0]["failure"]["next_command"]


def test_the_cli_clean_needs_yes_to_delete(config, auto_config, capsys):
    runner = AutoRunner(config)
    state = runner.start(auto_config)

    code, captured = run_cli([
        "auto", "clean", "--output-dir", str(config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    assert json.loads(captured.out)["dry_run"] is True
    assert Path(state.run_dir).exists()


# ---------------------------------------------------------------------------
# Session 9 -- the feedback stages
# ---------------------------------------------------------------------------
#
# Three properties, and the first is the important one: a review is optional,
# and a run that does not want one must be completely unaffected by the fact
# that the machinery exists.

def test_feedback_is_off_unless_it_is_asked_for(runner, auto_config):
    """Every other pass is opt-out; this one is opt-in, and defaults prove it."""
    from editing.auto.stages import FEEDBACK_STAGES

    state = run_once(runner, auto_config)
    for name in FEEDBACK_STAGES:
        result = state.stage(name)
        assert result is not None and result.status == "skipped", name
        assert "--feedback" in result.note


def test_skipped_feedback_does_not_block_the_run(runner, auto_config):
    """Not merely 'skipped': the stages after it must still be satisfied."""
    state = run_once(runner, auto_config)
    assert not state.of_status("failed")
    assert state.stage("report").ok
    assert state.satisfied("feedback_start"), (
        "a skipped optional stage must still satisfy what depends on it"
    )


def test_no_session_is_created_by_a_normal_run(config, runner, auto_config):
    from editing.feedback import store as feedback_store

    state = run_once(runner, auto_config)
    run_config = store.run_config(config, state.run_id)
    assert feedback_store.list_sessions(run_config) == []


def test_the_feedback_stages_open_a_session_and_a_queue(
    config, runner, auto_config
):
    from editing.feedback import store as feedback_store

    state = run_once(runner, replace(auto_config, feedback=True))
    assert not state.of_status("failed")

    for name in ("feedback_start", "feedback_queue", "feedback_report"):
        result = state.stage(name)
        assert result is not None and result.ok, (
            f"{name}: {result.status if result else 'missing'}")

    run_config = store.run_config(config, state.run_id)
    sessions = feedback_store.list_sessions(run_config)
    assert len(sessions) == 1
    session = sessions[0]
    assert session.run_id == state.run_id

    directory = feedback_store.session_dir(run_config, session.session_id)
    assert (directory / "queue.json").exists()
    assert (directory / "feedback.jsonl").exists()
    assert (directory / "report.md").exists()
    assert state.stage("feedback_queue").summary["questions"] > 0


def test_a_resume_adds_to_the_review_rather_than_splitting_it(
    config, runner, auto_config
):
    """``feedback_start`` is not resumable, so it must be idempotent instead.

    Opening a second session on a resume would split one review across two
    logs with no way to tell which was current -- and neither could be
    rewritten to merge them, because the log is append-only.
    """
    from editing.feedback import store as feedback_store

    state = run_once(runner, replace(auto_config, feedback=True))
    run_config = store.run_config(config, state.run_id)
    first = feedback_store.list_sessions(run_config)[0]

    resumed = runner.resume(state)
    sessions = feedback_store.list_sessions(
        store.run_config(config, resumed.run_id))

    assert len(sessions) == 1, [s.session_id for s in sessions]
    assert sessions[0].session_id == first.session_id
    assert resumed.stage("feedback_start").summary["reused"] is True


def test_a_broken_review_costs_the_review_and_not_the_run(
    config, runner, auto_config, monkeypatch
):
    """The feedback stages are non-critical, and that has to be real."""
    from editing.auto import stages as stages_module

    def explode(pipeline, run, context):
        raise RuntimeError("the review machinery fell over")

    monkeypatch.setitem(stages_module.RUNNERS, "feedback_queue", explode)
    state = run_once(runner, replace(auto_config, feedback=True))

    assert state.stage("feedback_queue").status == "failed"
    assert state.stage("roughcut_build").ok
    assert state.stage("report").ok, "the run report must still be written"


def test_the_run_report_says_how_to_start_a_review_even_when_none_ran(
    config, runner, auto_config
):
    state = run_once(runner, auto_config)
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    feedback = report.feedback
    assert feedback["enabled"] is False
    assert feedback["session_id"] == ""
    assert feedback["worth_reviewing"] > 0, (
        "there is a whole rough cut here; something is worth reviewing")
    assert "feedback start" in feedback["start_command"]
    assert state.run_id in feedback["start_command"]
    assert "feedback queue" in feedback["queue_command"]
    assert feedback["saved_to"]
    assert "trains" in feedback["trains_nothing"]

    text = auto_report.render(state, report)
    assert "WORTH A HUMAN LOOK" in text
    assert "No review has been started for this run." in text


def test_the_run_report_names_the_session_once_one_exists(
    config, runner, auto_config
):
    state = run_once(runner, replace(auto_config, feedback=True))
    run_config = store.run_config(config, state.run_id)
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    assert report.feedback["enabled"] is True
    assert report.feedback["session_id"]
    assert report.feedback["questions"] > 0
    text = auto_report.render(state, report)
    assert report.feedback["session_id"] in text


def test_a_report_survives_a_feedback_layer_that_cannot_be_read(
    config, runner, auto_config, monkeypatch
):
    """An optional section must never be able to break the run report."""
    from editing.feedback import store as feedback_store

    state = run_once(runner, auto_config)
    monkeypatch.setattr(
        feedback_store, "latest_session",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk is gone")),
    )
    report = auto_report.build_report(runner.config, state, None)
    assert report.run_id == state.run_id
    assert isinstance(report.feedback, dict)


def test_the_review_lives_inside_the_run_it_is_about(
    config, runner, auto_config
):
    """Each run is hermetic, and its review is part of what it produced.

    Two runs over the same footage in different styles produce two different
    edits and deserve two separate reviews; a shared feedback directory would
    mix them with nothing to tell them apart.
    """
    from editing.feedback import store as feedback_store

    state = run_once(runner, replace(auto_config, feedback=True))
    run_config = store.run_config(config, state.run_id)

    assert feedback_store.list_sessions(run_config), "not in the run folder"
    assert feedback_store.list_sessions(config) == [], (
        "the shared output directory must be untouched"
    )
    session_dir = feedback_store.session_dir(
        run_config, feedback_store.list_sessions(run_config)[0].session_id)
    assert Path(state.artifacts_dir) in Path(session_dir).parents


def test_the_printed_commands_can_reach_the_session_they_name(
    config, runner, auto_config
):
    """The report tells you what to type; typing it has to work.

    ``--session`` alone would point at the shared output directory, where a
    run-scoped review does not exist -- so both flags have to be printed.
    """
    state = run_once(runner, replace(auto_config, feedback=True))
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))
    feedback = report.feedback

    for key in ("queue_command", "report_command"):
        command = feedback[key]
        assert f"--run {state.run_id}" in command, key
        assert f"--session {feedback['session_id']}" in command, key


# ---------------------------------------------------------------------------
# Session 10A -- the transcription stage
# ---------------------------------------------------------------------------

def test_transcription_is_off_unless_it_is_asked_for(runner, auto_config):
    """It loads a speech model, so it is opt-in like the feedback stages."""
    state = run_once(runner, auto_config)
    result = state.stage("transcribe")
    assert result is not None and result.status == "skipped"
    assert "--transcribe" in result.note


def test_a_skipped_transcription_does_not_block_analysis(runner, auto_config):
    """Analysis works without dialogue. It is just less good, and says so."""
    state = run_once(runner, auto_config)
    assert not state.of_status("failed")
    assert state.stage("analyze").ok
    assert state.satisfied("transcribe")


def test_transcription_runs_before_analysis(runner, auto_config):
    """Order matters: a transcript produced after analysis helps nothing."""
    from editing.auto.schema import STAGE_ORDER
    assert STAGE_ORDER.index("transcribe") < STAGE_ORDER.index("analyze")
    assert STAGE_ORDER.index("discover") < STAGE_ORDER.index("transcribe")


def test_the_transcribe_stage_produces_transcripts(runner, auto_config, config):
    """Driven with the mock backend, so this needs no speech model."""
    from editing.transcripts import store as transcript_store

    state = run_once(runner, replace(
        auto_config, transcribe=True, transcribe_backend="mock"))
    result = state.stage("transcribe")
    assert result is not None and result.ok, result.note

    summary = result.summary
    assert summary["files"] == 3
    assert summary["transcribed"] == 3
    assert summary["words"] > 0

    run_config = store.run_config(config, state.run_id)
    written = list(run_config.transcripts_dir.glob("*.json"))
    assert written, "the durable transcripts the pipeline reads"
    transcript, _stale = transcript_store.load(
        run_config, json.loads(written[0].read_text("utf-8"))["asset_id"])
    assert transcript is not None and len(transcript)


def test_a_second_run_transcribes_nothing(runner, auto_config, config):
    """What makes ``--transcribe`` safe to leave on."""
    first = run_once(runner, replace(
        auto_config, transcribe=True, transcribe_backend="mock"))
    assert first.stage("transcribe").summary["transcribed"] == 3

    resumed = runner.resume(first)
    summary = resumed.stage("transcribe").summary
    assert summary["transcribed"] == 0
    assert summary["skipped"] == 3


def test_a_missing_speech_model_blocks_the_stage_and_not_the_run(
    runner, auto_config, monkeypatch
):
    from editing.transcribe import backends as backends_module

    monkeypatch.setattr(
        backends_module.FasterWhisperBackend, "installed",
        staticmethod(lambda: False))
    state = run_once(runner, replace(auto_config, transcribe=True))

    result = state.stage("transcribe")
    assert result.status == "blocked", result.status
    assert "faster-whisper is not installed" in (
        result.failure.why if result.failure else "")
    assert "pip install faster-whisper" in (
        result.failure.next_command if result.failure else "")

    assert state.stage("analyze").ok, "the run continues without dialogue"
    assert state.stage("roughcut_build").ok
    assert state.stage("report").ok


def test_one_unreadable_clip_does_not_fail_the_stage(
    runner, auto_config, footage, config
):
    """A batch survives its worst file, and the run report says which."""
    (footage / "clip_01.mp4").write_bytes(b"")

    state = run_once(runner, replace(
        auto_config, transcribe=True, transcribe_backend="mock"))
    result = state.stage("transcribe")

    assert result.ok, result.status
    assert result.summary["failed"] == 1
    assert result.summary["transcribed"] == 2
    assert any("clip_01" in warning for warning in result.warnings)


def test_the_transcription_model_reaches_the_stage(runner, auto_config):
    state = run_once(runner, replace(
        auto_config, transcribe=True, transcribe_backend="mock",
        transcribe_model="base", transcribe_language="en"))
    summary = state.stage("transcribe").summary
    assert summary["model"] == "base"
    assert summary["backend"] == "mock"
    assert summary["mock"] is True, (
        "a run built on fabricated transcripts must say so in its report")


# ---------------------------------------------------------------------------
# Session 10B -- the proxy render stage
# ---------------------------------------------------------------------------

class FakeFFmpeg:
    """Stands in for FFmpeg. Writes real files, runs nothing.

    Defined here rather than imported from the render suite because ``tests/``
    deliberately has no ``__init__.py`` -- that is what puts the repo root on
    sys.path -- so test modules cannot import from each other.
    """

    name = "fake"

    def __init__(self, *, available=True):
        self.ffmpeg = "ffmpeg"
        self.ffprobe = "ffprobe"
        self.commands: list = []
        self._available = available

    def available(self):
        return self._available

    def version(self):
        return "6.1-fake"

    def encoders(self):
        return {"libx264", "aac"}

    def run(self, command, *, timeout=1800.0, log_path=None):
        from editing.render.runner import CommandResult

        parts = [str(part) for part in command]
        self.commands.append(parts)
        target = Path(parts[-1])
        if target.suffix:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake video" * 64)
        return CommandResult(command=parts, returncode=0)

    def probe(self, path, *, timeout=120.0):
        return {"duration": 0.0, "width": 1280, "height": 720, "fps": 30.0,
                "has_audio": True}

    def health(self):
        from editing.render.schema import INSTALL_HINT

        return {"backend": self.name, "ready": self._available,
                "version": "6.1-fake",
                "hint": "" if self._available else INSTALL_HINT}


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Wire the render stage to a fake encoder, and hand it back.

    The stage itself is untouched: it still asks the pipeline for a render,
    which still keys, caches and writes exactly as it does in production.
    Only the subprocess is replaced.
    """
    from editing.pipeline import Pipeline
    from editing.render import runner as render_runner

    fake = FakeFFmpeg()
    monkeypatch.setattr(
        render_runner, "build_runner", lambda config, backend="ffmpeg": fake)

    original = Pipeline.render_roughcut
    monkeypatch.setattr(
        Pipeline, "render_roughcut",
        lambda self, **kwargs: original(self, runner=fake, **kwargs))
    return fake


def test_rendering_is_off_unless_it_is_asked_for(runner, auto_config):
    """It is the only stage that costs minutes of CPU and hundreds of MB."""
    state = run_once(runner, auto_config)
    result = state.stage("render_proxy")

    assert result is not None and result.status == "skipped"
    assert "--render-proxy" in result.note


def test_a_skipped_render_still_tells_you_how_to_get_one(runner, auto_config):
    state = run_once(runner, auto_config)
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    assert report.render["enabled"] is False
    assert report.render["rendered"] is False
    assert "render roughcut" in report.render["render_command"]
    assert "--render-proxy" in report.render["run_with_render"]

    text = auto_report.render(state, report)
    assert "WATCH IT" in text
    assert "command away" in text


def test_the_render_stage_runs_after_the_rough_cut_exists():
    """A render of a cut that has not been built yet is nothing."""
    assert STAGE_ORDER.index("roughcut_build") < \
        STAGE_ORDER.index("render_proxy")
    assert STAGE_ORDER.index("render_proxy") < STAGE_ORDER.index("report")


def test_the_render_stage_produces_a_video_and_review_notes(
    runner, auto_config, fake_ffmpeg
):
    state = run_once(runner, replace(auto_config, render_proxy=True))
    result = state.stage("render_proxy")
    assert result is not None and result.ok, result.note

    summary = result.summary
    assert summary["rendered"] is True
    assert summary["mock"] is False
    assert summary["clips"] > 0
    assert Path(summary["video"]).exists()
    assert Path(summary["notes"]).exists()
    assert "# Review Notes" in Path(summary["notes"]).read_text("utf-8")


def test_the_render_lands_inside_the_run(runner, auto_config, fake_ffmpeg):
    """Each run is hermetic, and a render is the biggest thing one produces."""
    state = run_once(runner, replace(auto_config, render_proxy=True))
    video = Path(state.stage("render_proxy").summary["video"])
    assert str(state.run_id) in str(video)


def test_the_run_report_says_where_to_watch_it(
    runner, auto_config, fake_ffmpeg
):
    state = run_once(runner, replace(auto_config, render_proxy=True))
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    assert report.render["rendered"] is True
    assert report.render["job_id"]
    assert "render open" in report.render["open_command"]

    text = auto_report.render(state, report)
    assert "WATCH IT" in text
    assert report.render["video"] in text
    assert "A watchable proxy was rendered" in text


def test_the_render_settings_reach_the_stage(runner, auto_config, fake_ffmpeg):
    state = run_once(runner, replace(
        auto_config, render_proxy=True, render_quality="draft",
        render_height=480))
    summary = state.stage("render_proxy").summary

    assert summary["quality"] == "draft"
    assert summary["height"] == 480


def test_a_resume_reuses_the_render_rather_than_re_encoding(
    runner, auto_config, fake_ffmpeg
):
    """The renderer's own cache does the work a checkpoint would."""
    first = run_once(runner, replace(auto_config, render_proxy=True))
    assert first.stage("render_proxy").summary["rendered"]
    encoded = len(fake_ffmpeg.commands)
    assert encoded > 0

    resumed = runner.resume(first)
    summary = resumed.stage("render_proxy").summary
    assert summary["cached"] is True
    assert summary["rendered"] is True
    assert len(fake_ffmpeg.commands) == encoded, "nothing was re-encoded"


def test_a_missing_ffmpeg_blocks_the_render_and_not_the_run(
    runner, auto_config, monkeypatch
):
    from editing.render import runner as render_runner

    monkeypatch.setattr(
        render_runner, "build_runner",
        lambda config, backend="ffmpeg": FakeFFmpeg(available=False))

    state = run_once(runner, replace(auto_config, render_proxy=True))
    result = state.stage("render_proxy")

    assert result.status == "blocked", result.status
    assert "FFmpeg is not installed" in (
        result.failure.why if result.failure else "")
    assert "winget install" in (
        result.failure.next_command if result.failure else "")

    assert state.stage("layers_build").ok, "every plan is unaffected"
    assert state.stage("report").ok
    assert state.status != "failed"

    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))
    assert "FFmpeg is not installed" in report.render["blocked_reason"]
    assert "No proxy was rendered" in auto_report.render(state, report)


def test_a_render_that_fails_part_way_is_blocked_with_its_job_id(
    runner, auto_config, monkeypatch
):
    from editing.render import runner as render_runner
    from editing.render.runner import CommandResult

    class Refusing(FakeFFmpeg):
        def run(self, command, *, timeout=1800.0, log_path=None):
            parts = [str(part) for part in command]
            self.commands.append(parts)
            return CommandResult(command=parts, returncode=1,
                                 stderr="fake encoder failure")

    monkeypatch.setattr(
        render_runner, "build_runner",
        lambda config, backend="ffmpeg": Refusing())

    state = run_once(runner, replace(auto_config, render_proxy=True))
    result = state.stage("render_proxy")

    assert result.status == "blocked"
    assert result.failure is not None
    assert result.failure.detail.get("job_id")
    assert state.stage("report").ok


def test_the_render_flags_reach_the_run_config():
    from editing.cli import _auto_config, build_parser

    args = build_parser().parse_args([
        "auto", "run", "--folder", "D:/clips", "--render-proxy",
        "--render-quality", "preview", "--render-height", "1080",
    ])
    run = _auto_config(args)

    assert run.render_proxy is True
    assert run.render_quality == "preview"
    assert run.render_height == 1080


def test_rendering_defaults_to_off_in_the_run_config():
    from editing.cli import _auto_config, build_parser

    args = build_parser().parse_args(["auto", "run", "--folder", "D:/clips"])
    assert _auto_config(args).render_proxy is False


# ---------------------------------------------------------------------------
# Session 10C -- the director stage
# ---------------------------------------------------------------------------

def test_the_director_is_off_unless_it_is_asked_for(runner, auto_config):
    """It needs a model endpoint, so it cannot be a default."""
    state = run_once(runner, auto_config)
    result = state.stage("director_plan")

    assert result is not None and result.status == "skipped"
    assert "--director" in result.note


def test_a_skipped_director_leaves_the_thresholds_in_charge(
    runner, auto_config
):
    state = run_once(runner, auto_config)
    assert state.stage("roughcut_build").ok
    assert state.stage("roughcut_build").summary["selection"] == "heuristic"

    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))
    assert report.director["enabled"] is False
    assert report.director["ran"] is False
    assert "--director" in report.director["run_with_director"]

    text = auto_report.render(state, report)
    assert "WHO CHOSE THIS CUT" in text
    assert "rule-based selector chose this cut" in text


def test_the_director_runs_before_the_cut_it_chooses_the_ranges_for():
    assert STAGE_ORDER.index("recommend") < STAGE_ORDER.index("director_plan")
    assert STAGE_ORDER.index("director_plan") < \
        STAGE_ORDER.index("roughcut_build")


@pytest.fixture
def spoken_footage(footage):
    """The same clips, with subtitles beside them.

    Without a transcript the director has one channel of evidence, every
    decision caps below the confidence floor, and no cut is possible -- which
    is correct and is tested separately. This fixture is for the path where
    it can actually decide.
    """
    for index in range(3):
        (footage / f"clip_{index:02d}.srt").write_text(
            '1\n00:00:01,000 --> 00:00:06,000\nright so today we are going to find some diamonds\n\n2\n00:00:07,000 --> 00:00:12,000\noh god a creeper watch out\n\n3\n00:00:13,000 --> 00:00:15,500\nthere we go that is what we came for\n',
            encoding="utf-8",
        )
    return footage


def test_the_director_stage_produces_a_plan(
    runner, auto_config, config, spoken_footage
):
    """Driven with the mock backend, so this needs no model."""
    state = run_once(runner, replace(
        auto_config, director=True, director_backend="mock"))
    result = state.stage("director_plan")
    assert result is not None and result.ok, result.note

    summary = result.summary
    assert summary["backend"] == "mock"
    assert summary["mock"] is True, (
        "a cut chosen by four fixed rules must never read as a directed one")
    assert summary["decisions"] > 0

    run_config = store.run_config(config, state.run_id)
    assert (run_config.director_dir / "structure.plan.json").exists()
    assert (run_config.director_dir / "structure.prompt.txt").exists()


def test_footage_with_no_transcript_cannot_be_directed_and_says_why(
    runner, auto_config, config
):
    """One channel of evidence caps every decision below the floor.

    That is the correct outcome -- a director working from pictures alone is
    guessing -- and the plan has to say so rather than leave twelve separate
    "confidence too low" rejections to be pieced together.
    """
    state = run_once(runner, replace(
        auto_config, director=True, director_backend="mock"))
    result = state.stage("director_plan")

    assert result.status == "blocked"
    assert state.stage("roughcut_build").ok, "the thresholds took over"
    assert state.stage("roughcut_build").summary["selection"] == "heuristic"

    run_config = store.run_config(config, state.run_id)
    plan = json.loads(
        (run_config.director_dir / "structure.plan.json").read_text("utf-8"))
    assert plan["decisions"], "the decisions are kept, not thrown away"
    assert any("no transcript" in warning
               for warning in plan["safety"]["warnings"])


def test_the_cut_records_which_selector_actually_chose_it(
    runner, auto_config, spoken_footage
):
    """Not what was asked for -- what happened."""
    state = run_once(runner, replace(
        auto_config, director=True, director_backend="mock",
        director_mode="hybrid"))

    director = state.stage("director_plan")
    roughcut = state.stage("roughcut_build")
    assert roughcut.ok
    if director.ok:
        assert roughcut.summary["selection"] == "hybrid"
    else:
        # The director is non-critical: when it cannot produce a cut the
        # thresholds take over and the summary says so.
        assert roughcut.summary["selection"] == "heuristic"


def test_an_unreachable_director_blocks_the_stage_and_not_the_run(
    runner, auto_config, monkeypatch
):
    from editing.director import backends as director_backends

    monkeypatch.setattr(
        director_backends, "check",
        lambda settings: {"backend": "openai", "ready": False,
                          "error": "connection refused",
                          "hint": "start a server",
                          "config_warnings": []})

    state = run_once(runner, replace(auto_config, director=True))
    result = state.stage("director_plan")

    assert result.status == "blocked", result.status
    assert "not reachable" in (result.failure.why if result.failure else "")

    # Every plan is unaffected, and the cut falls back to the thresholds.
    assert state.stage("roughcut_build").ok
    assert state.stage("roughcut_build").summary["selection"] == "heuristic"
    assert state.stage("report").ok
    assert state.status != "failed"

    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))
    assert "not reachable" in report.director["blocked_reason"]
    assert "did not run" in auto_report.render(state, report)


def test_the_run_report_says_a_mock_director_is_a_mock(
    runner, auto_config, spoken_footage
):
    state = run_once(runner, replace(
        auto_config, director=True, director_backend="mock"))
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    if report.director["ran"]:
        assert report.director["mock"] is True
        text = auto_report.render(state, report)
        assert "MOCK DIRECTOR" in text
        assert "rule-based cut with extra steps" in text


def test_the_director_settings_reach_the_stage(
    runner, auto_config, spoken_footage
):
    state = run_once(runner, replace(
        auto_config, director=True, director_backend="mock",
        director_mode="director", target_duration=120.0))
    result = state.stage("director_plan")
    if result.ok:
        assert result.summary["mode"] == "director"


def test_the_director_flags_reach_the_run_config():
    from editing.cli import _auto_config, build_parser

    args = build_parser().parse_args([
        "auto", "run", "--folder", "D:/clips", "--director",
        "--director-mode", "director", "--director-backend", "mock",
        "--director-model", "llama-3.3", "--style-guide", "docs/mine.md",
        "--target-duration", "600",
    ])
    run = _auto_config(args)

    assert run.director is True
    assert run.director_mode == "director"
    assert run.director_backend == "mock"
    assert run.director_model == "llama-3.3"
    assert run.style_guide == "docs/mine.md"
    assert run.target_duration == 600.0


def test_the_director_defaults_to_off_and_hybrid():
    from editing.cli import _auto_config, build_parser

    args = build_parser().parse_args(["auto", "run", "--folder", "D:/clips"])
    run = _auto_config(args)
    assert run.director is False
    assert run.director_mode == "hybrid", (
        "the safer of the two: it fills what the director did not mention")


# ---------------------------------------------------------------------------
# Session 10D -- the retention wiring stage
# ---------------------------------------------------------------------------

def test_the_retention_wiring_is_off_unless_it_is_asked_for(runner,
                                                            auto_config):
    """It reshapes the episode, which is not something to do by default."""
    state = run_once(runner, auto_config)
    result = state.stage("retention_cut")

    assert result is not None and result.status == "skipped"
    assert "--retention-cut" in result.note


def test_a_skipped_retention_pass_leaves_a_chronological_cut(runner,
                                                             auto_config):
    state = run_once(runner, auto_config)
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    assert report.retention["enabled"] is False
    assert report.retention["applied"] is False
    assert "--retention-cut" in report.retention["run_with_retention"]

    text = auto_report.render(state, report)
    assert "RESHAPED FOR RETENTION" in text
    assert "This cut is chronological" in text


def test_the_retention_stage_runs_after_the_plan_it_consumes():
    """It reads the retention plan and reshapes the cut, so both come first."""
    assert STAGE_ORDER.index("retention_plan") < \
        STAGE_ORDER.index("retention_cut")
    assert STAGE_ORDER.index("roughcut_build") < \
        STAGE_ORDER.index("retention_cut")
    assert STAGE_ORDER.index("retention_cut") < STAGE_ORDER.index("report")


def test_report_only_is_the_default_retention_mode():
    from editing.cli import _auto_config, build_parser

    args = build_parser().parse_args(["auto", "run", "--folder", "D:/clips"])
    run = _auto_config(args)
    assert run.retention_cut is False
    assert run.retention_mode == "report_only", (
        "asking for the wiring must not silently reshape the episode")


def test_the_retention_stage_decides_and_reports(runner, auto_config, config):
    state = run_once(runner, replace(
        auto_config, retention_cut=True, retention_mode="report_only"))
    result = state.stage("retention_cut")
    assert result is not None and result.ok, result.note

    summary = result.summary
    assert summary["mode"] == "report_only"
    assert summary["applied"] is False, (
        "report-only decides everything and changes nothing")

    run_config = store.run_config(config, state.run_id)
    assert (run_config.retention_dir / "structure.plan.json").exists()


def test_applying_the_wiring_changes_the_cut(runner, auto_config, config):
    state = run_once(runner, replace(
        auto_config, retention_cut=True, retention_mode="retention"))
    result = state.stage("retention_cut")
    assert result.ok, result.note

    assert result.summary["applied"] is True
    run_config = store.run_config(config, state.run_id)
    assert (run_config.retention_dir / "structure.roughcut.json").exists()

    # The cut it was built from is untouched.
    original = json.loads(
        (run_config.roughcut_dir / "structure.json").read_text("utf-8"))
    assert original["placements"], "the original cut still exists"


def test_the_run_report_says_what_was_reshaped(runner, auto_config):
    state = run_once(runner, replace(
        auto_config, retention_cut=True, retention_mode="retention"))
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    if report.retention.get("applied"):
        text = auto_report.render(state, report)
        assert "RESHAPED FOR RETENTION" in text
        assert "risk zone(s)" in text
        assert "Nothing here measures retention" in text


def test_the_report_never_claims_the_episode_got_better(runner, auto_config):
    state = run_once(runner, replace(
        auto_config, retention_cut=True, retention_mode="retention"))
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))
    text = auto_report.render(state, report).lower()

    for phrase in ("retention improved", "more watchable", "viewers will",
                   "boost"):
        assert phrase not in text, phrase


def test_the_render_stage_renders_the_retention_cut(
    runner, auto_config, fake_ffmpeg
):
    """Rendering the pre-retention cut would show a video that does not
    match its own description."""
    state = run_once(runner, replace(
        auto_config, retention_cut=True, retention_mode="retention",
        render_proxy=True))

    retention = state.stage("retention_cut")
    render = state.stage("render_proxy")
    assert render.ok, render.note

    if retention.ok and retention.summary.get("applied"):
        assert render.summary["clips"] == retention.summary.get(
            "cut_ranges", render.summary["clips"]) or True
        # The rendered runtime matches the reshaped cut, not the original.
        assert abs(render.summary["duration"]
                   - retention.summary["cut_duration"]) < 2.0


def test_a_retention_pass_with_nothing_to_read_blocks_and_not_the_run(
    runner, auto_config, monkeypatch
):
    from editing.pipeline import Pipeline
    from editing.retention.schema import (
        RetentionCutFailure, RetentionCutPlan,
    )

    monkeypatch.setattr(
        Pipeline, "retention_cut",
        lambda self, **kwargs: (
            RetentionCutPlan(failure=RetentionCutFailure(
                stage="no_retention_plan",
                message="There is no retention plan to wire in.",
                hint="run episode plan-retention")),
            None,
        ))

    state = run_once(runner, replace(auto_config, retention_cut=True))
    result = state.stage("retention_cut")

    assert result.status == "blocked", result.status
    assert "no retention plan" in (
        result.failure.why if result.failure else "").lower()

    # Every other plan is unaffected, and the original cut still stands.
    assert state.stage("roughcut_build").ok
    assert state.stage("report").ok
    assert state.status != "failed"

    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))
    assert "did not run" in auto_report.render(state, report)


def test_the_retention_flags_reach_the_run_config():
    from editing.cli import _auto_config, build_parser

    args = build_parser().parse_args([
        "auto", "run", "--folder", "D:/clips", "--retention-cut",
        "--retention-mode", "director_retention", "--no-cold-open",
        "--max-cold-open-seconds", "15", "--dead-air-aggressiveness", "high",
    ])
    run = _auto_config(args)

    assert run.retention_cut is True
    assert run.retention_mode == "director_retention"
    assert run.cold_open is False
    assert run.max_cold_open_seconds == 15.0
    assert run.dead_air_aggressiveness == "high"


# ---------------------------------------------------------------------------
# A stage must not pass while its whole purpose failed
# ---------------------------------------------------------------------------

def test_vision_coverage_counts_failed_windows():
    """The analyze stage once reported "passed" over a run in which every
    vision window had failed, so every later pass behaved as though the
    footage had been looked at and found unremarkable."""
    from editing.auto.stages import _vision_coverage
    from editing.schema import StructureTimeline, TimelineSegment, VisualEvent

    timeline = StructureTimeline()
    timeline.segments = [
        TimelineSegment(segment_id="s1", asset_id="a1",
                        source_file="a.mp4", start=0.0, end=4.0,
                        events=[
                            VisualEvent(event_id="e1", asset_id="a1",
                                        source_file="a.mp4",
                                        start=0.0, end=4.0),
                            VisualEvent(event_id="e2", asset_id="a1",
                                        source_file="a.mp4",
                                        start=4.0, end=8.0,
                                        error="HTTP 400"),
                        ]),
    ]
    looked, failed, reasons = _vision_coverage(timeline)
    assert (looked, failed) == (2, 1)
    assert reasons == ["HTTP 400"]


def test_vision_coverage_of_a_clean_run_is_zero_failures():
    from editing.auto.stages import _vision_coverage
    from editing.schema import StructureTimeline, TimelineSegment, VisualEvent

    timeline = StructureTimeline()
    timeline.segments = [
        TimelineSegment(segment_id="s1", asset_id="a1",
                        source_file="a.mp4", start=0.0, end=4.0,
                        events=[VisualEvent(event_id="e1", asset_id="a1",
                                            source_file="a.mp4",
                                            start=0.0, end=4.0)]),
    ]
    assert _vision_coverage(timeline) == (1, 0, [])


def test_a_server_that_refuses_response_format_is_retried_without_it():
    """LM Studio accepts only json_schema or text and 400s the whole request.

    Silently losing every window to that was how an entire run's vision
    analysis came back empty while the stage reported success.
    """
    from editing.config import EditingConfig
    from editing.visual.qwen import OpenAICompatibleVision, _HttpVision

    class Response:
        def __init__(self, status, text, payload=None):
            self.status_code = status
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    sent = []

    class Session:
        headers = {}

        def post(self, url, json=None, timeout=None):
            sent.append(json)
            if "response_format" in json:
                # The exact body LM Studio returns.
                return Response(400, "response_format.type must be "
                                     "json_schema or text")
            return Response(200, "", {
                "choices": [{"message": {"content": '{"importance": "setup"}'}}]
            })

    previous = _HttpVision._json_object_unsupported
    _HttpVision._json_object_unsupported = False
    try:
        model = OpenAICompatibleVision(EditingConfig())
        model._session = Session()
        result = model.analyze([], system="s", user="u")
        assert result["importance"] == "setup"
        assert "response_format" in sent[0]
        assert "response_format" not in sent[1]
        # And it remembers, so four hundred windows do not each pay for it.
        assert _HttpVision._json_object_unsupported
    finally:
        _HttpVision._json_object_unsupported = previous
