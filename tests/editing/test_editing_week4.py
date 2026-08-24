"""The Week 4 stages, end to end through the auto pipeline.

Caption polish, audio polish, the reliability checks and the review package,
asserted where a unit test cannot see: that they are genuinely off by default,
that they read the cut *this run* produced rather than an earlier one, and that
a run with all of them on completes on a machine with nothing installed.

The fixtures are rebuilt here rather than imported, because ``tests/``
deliberately has no ``__init__.py`` -- that is what puts the repo root on
sys.path -- so test modules cannot import from each other.

**Nothing in this file needs FFmpeg, a GPU, a model server, Premiere, Whisper
or real footage.** The two external edges are stubbed: frame extraction and
ffprobe.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from editing.auto import report as auto_report
from editing.auto import stages as auto_stages
from editing.auto.runner import AutoRunner
from editing.auto.schema import STAGE_ORDER, AutoRunConfig
from editing.visual.frames import ExtractedFrames


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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

#: Three lines with a stated objective, a named threat and a payoff, so the
#: caption pass has something it can legitimately find. Without a transcript
#: it correctly finds nothing, which is tested in the unit suite.
SRT = (
    "1\n00:00:01,000 --> 00:00:06,000\n"
    "right so today we are going to find some diamonds\n\n"
    "2\n00:00:07,000 --> 00:00:12,000\n"
    "oh god a creeper watch out\n\n"
    "3\n00:00:13,000 --> 00:00:15,500\n"
    "there we go that is what we came for\n"
)


@pytest.fixture
def fake_probe(monkeypatch):
    """Patch ffprobe out everywhere discovery reaches for it."""
    from editing import discovery
    from editing import ffmpeg as ff

    def probe(path, *, ffprobe="ffprobe"):
        return dict(PROBE)

    monkeypatch.setattr(ff, "probe", probe)
    monkeypatch.setattr(discovery.ff, "probe", probe)
    return probe


@pytest.fixture
def footage(tmp_path):
    """Three files that look like video to discovery, with subtitles."""
    folder = tmp_path / "clips"
    folder.mkdir()
    for index in range(3):
        (folder / f"clip_{index:02d}.mp4").write_bytes(b"not a video" * 512)
        (folder / f"clip_{index:02d}.srt").write_text(SRT, encoding="utf-8")
    return folder


@pytest.fixture
def auto_config(footage) -> AutoRunConfig:
    return AutoRunConfig(
        footage_folder=str(footage),
        style="cinematic_minecraft",
        mock=True,
        no_premiere=True,
    )


class StubFrameSource:
    """Stands in for FFmpeg frame extraction."""

    def __init__(self, frame_path):
        self.frame_path = frame_path

    def extract(self, path, window) -> ExtractedFrames:
        return ExtractedFrames(
            window=window,
            times=list(window.frame_times),
            paths=[self.frame_path] * len(window.frame_times),
            directory=None,
        )


@pytest.fixture
def runner(config, footage, monkeypatch, fake_probe, tmp_path):
    """A runner wired so no external tool is ever reached."""
    from editing.roughcut import review as review_module

    written = tmp_path / "frame.jpg"
    written.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(
        review_module.ff, "extract_frame", lambda *a, **k: written)

    engine = AutoRunner(config, say=lambda message: None)
    original = engine.pipeline_for

    def wired(state):
        pipeline = original(state)
        real = pipeline.analyzer

        def build(**kwargs):
            kwargs.setdefault("use_motion", False)
            analyzer = real(**kwargs)
            analyzer._frame_source = StubFrameSource(written)
            return analyzer

        pipeline.analyzer = build
        return pipeline

    engine.pipeline_for = wired
    return engine


def run_once(runner, auto_config, **kwargs):
    state = runner.start(auto_config, **kwargs)
    return runner.run(state)


def polished(auto_config, **overrides) -> AutoRunConfig:
    return replace(
        auto_config, captions="key_moments", audio_polish="placeholders",
        **overrides)


# ---------------------------------------------------------------------------
# Ordering and shape
# ---------------------------------------------------------------------------

def test_the_polish_stages_run_after_the_cut_they_read():
    """Captions against the pre-retention cut would be at the wrong moment."""
    assert STAGE_ORDER.index("retention_cut") < \
        STAGE_ORDER.index("caption_polish")
    assert STAGE_ORDER.index("caption_polish") < \
        STAGE_ORDER.index("render_proxy")
    assert STAGE_ORDER.index("render_proxy") < \
        STAGE_ORDER.index("reliability_gates")
    assert STAGE_ORDER.index("reliability_gates") < \
        STAGE_ORDER.index("review_package")
    assert STAGE_ORDER.index("review_package") < STAGE_ORDER.index("report")


def test_no_new_stage_requires_premiere_ffmpeg_or_a_model():
    for name in ("caption_polish", "audio_polish", "reliability_gates",
                 "review_package"):
        stage = auto_stages.stage(name)
        assert stage.requires_premiere is False
        assert stage.requires_ffmpeg is False
        assert stage.requires_model is False
        # Non-critical: losing the captions costs the captions.
        assert stage.critical is False


def test_the_checks_and_the_package_are_never_checkpointed():
    """A cached "everything passed" over a deleted video is the one answer
    these stages must never give."""
    assert auto_stages.stage("reliability_gates").resumable is False
    assert auto_stages.stage("review_package").resumable is False


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------

def test_polish_is_off_unless_it_is_asked_for(runner, auto_config):
    state = run_once(runner, auto_config)

    captions = state.stage("caption_polish")
    audio = state.stage("audio_polish")
    assert captions is not None and captions.status == "skipped"
    assert "--captions" in captions.note
    assert audio is not None and audio.status == "skipped"
    assert "--audio-polish" in audio.note


def test_a_run_with_polish_disabled_still_completes(runner, auto_config):
    state = run_once(runner, auto_config)
    assert state.status in ("complete", "blocked"), state.stats()
    assert state.stage("reliability_gates").ok
    assert state.stage("review_package").ok


def test_turning_the_review_package_off_skips_it(runner, auto_config):
    state = run_once(runner, replace(auto_config, review_package=False))
    result = state.stage("review_package")
    assert result.status == "skipped"
    assert "--no-review-package" in result.note


# ---------------------------------------------------------------------------
# On
# ---------------------------------------------------------------------------

def test_a_run_with_polish_enabled_completes_with_nothing_installed(
    runner, auto_config
):
    state = run_once(runner, polished(auto_config))
    assert state.status in ("complete", "blocked"), state.stats()

    captions = state.stage("caption_polish")
    audio = state.stage("audio_polish")
    assert captions.ok, captions.note
    assert audio.ok, audio.note
    assert captions.summary["considered"] > 0
    assert audio.summary["considered"] > 0


def test_polish_reads_the_retention_cut_when_there_is_one(runner, auto_config):
    state = run_once(runner, polished(
        auto_config, retention_cut=True, retention_mode="retention"))
    assert state.stage("caption_polish").summary["base"] == "retention"
    assert state.stage("audio_polish").summary["base"] == "retention"


def test_polish_reads_the_rough_cut_when_nothing_reshaped_it(
    runner, auto_config
):
    state = run_once(runner, polished(auto_config))
    assert state.stage("caption_polish").summary["base"] == "roughcut"
    assert state.stage("audio_polish").summary["base"] == "roughcut"


def test_the_caption_stage_never_claims_to_be_in_the_video(
    runner, auto_config
):
    state = run_once(runner, replace(auto_config, captions="key_moments"))
    assert state.stage("caption_polish").summary["burned_in"] is False


def test_placeholder_audio_never_claims_to_play(runner, auto_config):
    state = run_once(runner, replace(auto_config, audio_polish="placeholders"))
    assert state.stage("audio_polish").summary["plays_anything"] is False


def test_the_polish_plans_land_inside_the_run(runner, auto_config):
    """Each run is hermetic, and a plan is about one run's cut."""
    state = run_once(runner, polished(auto_config))
    for stage in ("caption_polish", "audio_polish"):
        outputs = state.stage(stage).outputs
        assert outputs
        for path in outputs:
            assert state.run_id in path
            assert Path(path).exists()


def test_every_caption_names_a_key_moment_and_a_reason(runner, auto_config):
    from editing.polish import store as polish_store

    state = run_once(runner, polished(auto_config))
    pipeline = runner.pipeline_for(state)
    plan = polish_store.load_captions(pipeline.config, name="structure")
    for decision in plan.accepted:
        assert decision.moment
        assert decision.reason
    for decision in plan.rejected:
        assert decision.reject_reason


# ---------------------------------------------------------------------------
# The reliability checks
# ---------------------------------------------------------------------------

def test_the_checks_run_and_write_both_forms(runner, auto_config):
    state = run_once(runner, auto_config)
    checks = state.stage("reliability_gates")
    assert checks.ok, checks.note
    assert checks.summary["status"] in ("pass", "warn", "fail")
    assert checks.outputs
    for path in checks.outputs:
        assert Path(path).exists()
    assert (Path(state.run_dir) / "reports" / "checks.json").exists()


def test_the_checks_never_fail_the_run(runner, auto_config):
    """A gate that says the output is unusable has said the useful thing;
    stopping the pipeline on top of that only costs the explanation."""
    state = run_once(runner, auto_config)
    assert state.stage("reliability_gates").status == "passed"


def test_the_checks_count_a_transcript_that_came_from_a_sidecar_file(
    runner, auto_config
):
    """A transcript arrives three ways, and only the timeline sees all three.

    Reading the Whisper stage alone reported "this run has no transcript" over
    an episode whose every line had been read from an .srt beside the footage.
    """
    state = run_once(runner, auto_config)
    checks = state.stage("reliability_gates")

    assert state.config.transcribe is False       # Whisper never ran
    assert "transcript" not in checks.summary["warnings"]


def test_the_checks_warn_about_a_run_that_never_heard_the_footage(
    runner, auto_config, footage
):
    """Footage with no words in it is a fact worth saying out loud."""
    for path in footage.glob("*.srt"):
        path.unlink()

    state = run_once(runner, auto_config)
    warnings = state.stage("reliability_gates").summary["warnings"]
    assert "transcript" in warnings


def test_a_mock_run_is_never_reported_as_unusable(runner, auto_config):
    state = run_once(runner, auto_config)
    assert state.stage("reliability_gates").summary["usable"] is True


def test_the_checks_see_the_polish_this_run_produced(runner, auto_config):
    """A density gate must not report "does not apply" over a run that ran."""
    from editing.reliability import run as reliability_run

    state = run_once(runner, polished(auto_config))
    report, inputs = reliability_run.check_run(runner.config, state)

    assert inputs.captions_enabled is True
    assert inputs.audio_enabled is True
    assert report.get("caption_density").status != "skipped"
    assert report.get("sfx_density").status != "skipped"


# ---------------------------------------------------------------------------
# The review package
# ---------------------------------------------------------------------------

def test_the_review_package_is_built_beside_the_run(runner, auto_config):
    from editing.review import store as review_store

    state = run_once(runner, auto_config)
    package = state.stage("review_package")
    assert package.ok, package.note

    index = review_store.index_path(runner.config, state.run_id)
    assert index.exists()
    # Beside the artifacts, not among them.
    assert index.parent.parent == Path(state.run_dir)


def test_the_review_index_answers_the_five_questions(runner, auto_config):
    from editing.review import store as review_store

    state = run_once(runner, auto_config)
    text = review_store.index_path(
        runner.config, state.run_id).read_text("utf-8")
    for heading in ("## 1. Watch this", "## 2. What changed",
                    "## 3. What to watch for", "## 4. Weak points",
                    "## 5. Needs you"):
        assert heading in text


def test_the_review_index_reports_a_settled_run_status(runner, auto_config):
    """"running" is true for about four milliseconds and misleading after."""
    from editing.review import store as review_store

    state = run_once(runner, auto_config)
    text = review_store.index_path(
        runner.config, state.run_id).read_text("utf-8")
    assert "run status **running**" not in text


def test_the_review_package_is_rebuilt_rather_than_reused(runner, auto_config):
    """It is a view over everything else, and a cached view is stale."""
    state = run_once(runner, auto_config)
    state = runner.run(state)
    assert state.stage("review_package").from_checkpoint is False


def test_the_review_package_carries_the_polish_this_run_produced(
    runner, auto_config
):
    from editing.review import store as review_store

    state = run_once(runner, polished(auto_config))
    package = review_store.load_package(runner.config, state.run_id)
    headline = " ".join(package.headline)
    assert "Captions:" in headline
    assert "Audio polish:" in headline


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def test_the_report_answers_the_thirteen_questions(runner, auto_config):
    state = run_once(runner, auto_config)
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    assert len(report.answers) == len(auto_report.QUESTIONS)
    for entry in report.answers:
        assert entry["question"]
        assert entry["answer"]

    text = auto_report.render(state, report)
    assert "WHAT THIS EDIT IS" in text
    assert "What footage was used?" in text
    assert "What should I watch manually?" in text


def test_the_report_carries_every_new_section(runner, auto_config):
    state = run_once(runner, auto_config)
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))
    text = auto_report.render(state, report)

    for heading in ("WHAT IS ON SCREEN", "WHAT YOU WOULD HEAR",
                    "RELIABILITY CHECKS", "THE REVIEW FOLDER"):
        assert heading in text


def test_the_report_says_when_polish_is_off_and_how_to_turn_it_on(
    runner, auto_config
):
    state = run_once(runner, auto_config)
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    assert report.captions["enabled"] is False
    assert report.audio["enabled"] is False
    assert "--captions key_moments" in report.captions["run_with_captions"]
    assert "--audio-polish placeholders" in report.audio["run_with_audio"]


def test_the_report_never_says_captions_are_in_the_video(runner, auto_config):
    state = run_once(runner, replace(auto_config, captions="key_moments"))
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))
    text = auto_report.render(state, report)

    assert report.captions["burned_in"] is False
    if report.captions["accepted"]:
        assert "NOT in the rendered video" in text


def test_the_report_never_claims_the_edit_improved(runner, auto_config):
    state = run_once(runner, polished(auto_config))
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))
    text = auto_report.render(state, report).lower()

    assert "retention improved" not in text
    assert "guaranteed" not in text
    assert "% better" not in text
    assert "worth a human look" in text


def test_the_report_points_at_the_review_folder(runner, auto_config):
    state = run_once(runner, auto_config)
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    assert report.review["exists"] is True
    assert "review summary" in " ".join(report.next_commands)


def test_the_report_shows_the_check_results(runner, auto_config):
    state = run_once(runner, auto_config)
    report = auto_report.build_report(
        runner.config, state, runner.pipeline_for(state))

    assert report.checks["ran"] is True
    assert report.checks["status"] in ("pass", "warn", "fail")
    assert "show-checks" in report.checks["command"]


# ---------------------------------------------------------------------------
# Export stays optional
# ---------------------------------------------------------------------------

def test_a_full_no_premiere_run_reaches_every_new_stage(runner, auto_config):
    """Proxy-only mode: no Premiere, no GPU, no model, no library."""
    state = run_once(runner, polished(
        auto_config, retention_cut=True, retention_mode="retention"))

    for name in ("caption_polish", "audio_polish", "reliability_gates",
                 "review_package"):
        assert state.stage(name).ok, state.stage(name).note
    assert all(not gate.executed for gate in state.gates)
    assert all(not gate.ready for gate in state.gates)


def test_nothing_is_executed_against_premiere_by_default(runner, auto_config):
    state = run_once(runner, polished(auto_config))
    assert all(not gate.executed for gate in state.gates)


# ---------------------------------------------------------------------------
# The caption sidecar, beside a rendered proxy
# ---------------------------------------------------------------------------


class FakeFFmpeg:
    """A fake encoder: builds the same commands, writes a placeholder file."""

    name = "fake"

    def __init__(self):
        self.ffmpeg = "ffmpeg"
        self.ffprobe = "ffprobe"
        self.commands: list = []

    def available(self):
        return True

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
        return {"backend": self.name, "ready": True, "version": "6.1-fake",
                "hint": ""}


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Wire the render stage to a fake encoder. The stage itself is untouched."""
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


def test_the_render_writes_a_caption_sidecar_beside_the_video(
    runner, auto_config, fake_ffmpeg
):
    """Captions are not burned in, so the .srt beside the file is the answer."""
    state = run_once(runner, replace(
        auto_config, captions="key_moments", render_proxy=True))

    render = state.stage("render_proxy")
    assert render.ok, render.note
    assert state.stage("caption_polish").summary["accepted"] > 0

    sidecar = render.summary.get("subtitles") or ""
    assert sidecar.endswith(".srt")
    assert Path(sidecar).exists()
    assert Path(sidecar).parent == Path(render.summary["video"]).parent
    assert "-->" in Path(sidecar).read_text("utf-8")


def test_no_sidecar_is_written_when_no_caption_was_earned(
    runner, auto_config, fake_ffmpeg
):
    """An empty .srt would suggest captions were tried and failed."""
    state = run_once(runner, replace(auto_config, render_proxy=True))
    assert state.stage("caption_polish").status == "skipped"
    assert not (state.stage("render_proxy").summary.get("subtitles") or "")


def test_the_review_package_points_at_the_video_it_rendered(
    runner, auto_config, fake_ffmpeg
):
    from editing.review import store as review_store

    state = run_once(runner, polished(auto_config, render_proxy=True))
    package = review_store.load_package(runner.config, state.run_id)

    assert package.video_exists is True
    assert package.item("video") is not None
    text = review_store.index_path(
        runner.config, state.run_id).read_text("utf-8")
    assert package.video in text


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

def test_the_run_command_from_the_brief_parses():
    from editing.cli import build_parser

    args = build_parser().parse_args([
        "auto", "run", "--folder", "E:/Clips/Test", "--director",
        "--retention-cut", "--render-proxy", "--captions", "key_moments",
        "--audio-polish", "placeholders", "--no-premiere",
    ])
    assert args.captions == "key_moments"
    assert args.audio_polish == "placeholders"
    assert args.no_premiere is True


def test_the_batch_command_from_the_brief_parses():
    from editing.cli import build_parser

    args = build_parser().parse_args([
        "auto", "batch", "--root", "E:/Clips", "--director",
        "--retention-cut", "--render-proxy", "--no-premiere", "--limit", "3",
    ])
    assert args.auto_command == "batch"
    assert args.root == "E:/Clips"
    assert args.limit == 3


def test_every_batch_option_from_the_brief_exists():
    from editing.cli import build_parser

    args = build_parser().parse_args([
        "auto", "batch", "--root", "E:/Clips", "--limit", "2", "--only-new",
        "--resume", "--force", "--dry-run", "--style", "fast_funny",
        "--director", "--retention-cut", "--render-proxy", "--no-premiere",
    ])
    assert (args.only_new, args.resume, args.force, args.dry_run) == \
        (True, True, True, True)
    assert args.style == "fast_funny"


def test_the_review_commands_from_the_brief_parse():
    from editing.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(
        ["review", "open-latest"]).review_command == "open-latest"
    assert parser.parse_args(
        ["review", "summary", "--latest"]).review_command == "summary"
    package = parser.parse_args(["review", "package", "--run", "r1"])
    assert package.review_command == "package"
    assert package.run == "r1"


def test_the_polish_commands_parse():
    from editing.cli import build_parser

    parser = build_parser()
    for command in ("captions", "audio", "show-rejected", "show-missing"):
        args = parser.parse_args(["polish", command])
        assert args.polish_command == command


def test_show_checks_parses():
    from editing.cli import build_parser

    args = build_parser().parse_args(
        ["auto", "show-checks", "--run", "r1", "--rebuild"])
    assert args.auto_command == "show-checks"
    assert args.rebuild is True


def test_the_polish_flags_reach_the_run_config():
    from editing.cli import _auto_config, build_parser

    args = build_parser().parse_args([
        "auto", "run", "--folder", "D:/clips",
        "--captions", "key_moments", "--max-captions-per-minute", "0.5",
        "--max-caption-seconds", "2.5", "--max-caption-words", "6",
        "--min-caption-confidence", "0.7", "--require-caption-confidence",
        "--audio-polish", "assets", "--max-sfx-per-minute", "1.5",
        "--no-music-bed", "--no-ducking",
    ])
    run = _auto_config(args)

    assert run.captions == "key_moments"
    assert run.max_captions_per_minute == 0.5
    assert run.max_caption_seconds == 2.5
    assert run.max_caption_words == 6
    assert run.min_caption_confidence == 0.7
    assert run.require_caption_confidence is True
    assert run.audio_polish == "assets"
    assert run.max_sfx_per_minute == 1.5
    assert run.music_bed is False
    assert run.ducking is False


def test_polish_defaults_to_off_in_the_run_config():
    from editing.cli import _auto_config, build_parser

    args = build_parser().parse_args(
        ["auto", "run", "--folder", "D:/clips"])
    run = _auto_config(args)

    assert run.captions == "off"
    assert run.audio_polish == "off"
    # The one late addition that is opt-out: it creates nothing new, costs a
    # fraction of a second, and is what makes a run inspectable.
    assert run.review_package is True


def test_the_review_package_can_be_turned_off_from_the_cli():
    from editing.cli import _auto_config, build_parser

    args = build_parser().parse_args(
        ["auto", "run", "--folder", "D:/clips", "--no-review-package"])
    assert _auto_config(args).review_package is False


def test_an_unknown_caption_mode_is_refused_by_the_parser():
    from editing.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["auto", "run", "--folder", "D:/c", "--captions", "everything"])


def test_an_unknown_audio_mode_is_refused_by_the_parser():
    from editing.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["auto", "run", "--folder", "D:/c", "--audio-polish", "loud"])
