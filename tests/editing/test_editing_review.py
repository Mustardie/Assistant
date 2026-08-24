"""The review package: one folder, one index, and no verdict.

Two properties are the point.

**It never claims a file exists when it does not.** A review package is the
thing somebody opens *after* a run went wrong as often as after it went right,
so a missing artifact is an item marked absent. A package that listed a video
that had been deleted would be worse than no package.

**It is a view, not a record.** Rebuilding it says what is true now. So the
index is asserted on content -- the five questions, in order -- rather than on
having been written once.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from editing.auto import store as auto_store
from editing.auto.schema import AutoRunConfig, AutoRunState, AutoStageResult
from editing.reliability.schema import GateReport, gate
from editing.review import build as review_build
from editing.review import index as review_index
from editing.review import store as review_store
from editing.review.schema import ReviewPackage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_run(config, tmp_path, *, video: bool = True, run_id: str = "run-1",
             status: str = "complete") -> AutoRunState:
    """A finished run with plausible artifacts on disk."""
    run = AutoRunConfig(
        footage_folder=str(tmp_path / "clips"),
        style="cinematic_minecraft",
        retention_cut=True, render_proxy=video,
        captions="key_moments", audio_polish="placeholders",
    )
    state = auto_store.create(config, run, run_id)
    state.status = status

    artifacts = Path(state.run_dir) / "artifacts"
    for relative in ("polish/structure.captions.txt",
                     "polish/structure.captions.json",
                     "polish/structure.captions.srt",
                     "polish/structure.audio.txt",
                     "retention/structure.plan.txt",
                     "roughcut/structure.json"):
        target = artifacts / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("artifact", encoding="utf-8")

    reports = Path(state.run_dir) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "report.txt").write_text("the run report", encoding="utf-8")

    video_path = ""
    if video:
        video_path = str(Path(state.run_dir) / "artifacts" / "render.mp4")
        Path(video_path).write_bytes(b"video" * 4096)

    state.stages = [
        AutoStageResult(stage="roughcut_build", status="passed", summary={
            "sequence": "Nova Rough Cut", "clips": 12,
            "cut_duration": 620.0, "source_duration": 1800.0,
            "selection": "hybrid",
        }),
        AutoStageResult(stage="retention_cut", status="passed", summary={
            "applied": True, "base": "heuristic", "mode": "retention",
            "base_duration": 700.0, "cut_duration": 620.0,
            "cold_open": True, "cold_open_type": "danger",
            "cold_open_seconds": 11.0, "zones_compressed": 2,
            "seconds_removed": 64.0, "dead_air_cut": 5,
            "setups_protected": 3, "payoffs_protected": 2, "refused": 4,
            "unresolved": 1,
        }),
        AutoStageResult(stage="render_proxy", status="passed", summary={
            "video": video_path, "notes": "", "duration": 620.0,
            "size_mb": 0.02, "mock": False, "rendered": bool(video),
        }),
        AutoStageResult(stage="feedback_queue", status="passed", summary={
            "worth_reviewing": 7,
        }),
    ]
    auto_store.save(config, state)
    return state


@pytest.fixture
def state(config, tmp_path):
    return make_run(config, tmp_path)


@pytest.fixture
def checks() -> GateReport:
    report = GateReport(run_id="run-1")
    report.gates = [
        gate("footage", "pass", "3 files."),
        gate("transcript", "warn", "This run has no transcript.",
             evidence={"words": 0}, fix="--transcribe"),
        gate("caption_density", "pass", "4 captions."),
    ]
    return report


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def test_a_package_names_the_run_it_is_about(config, state):
    package = review_build.build_package(config, state, copy_files=False)
    assert package.run_id == state.run_id
    assert package.style == "cinematic_minecraft"
    assert package.run_status == "complete"


def test_a_package_points_at_the_video_rather_than_copying_it(config, state):
    package = review_build.build_package(config, state)
    assert package.video_exists is True
    item = package.item("video")
    assert item is not None and item.kind == "video"
    # Pointed at, never copied: a proxy is hundreds of megabytes.
    assert item.copied_to == ""


def test_small_reports_are_copied_into_the_folder(config, state):
    package = review_build.build_package(config, state)
    captions = package.item("captions")
    assert captions.exists
    assert captions.copied_to
    assert Path(captions.copied_to).exists()
    assert Path(captions.copied_to).parent == \
        review_store.package_dir(config, state.run_id)


def test_an_artifact_that_was_never_produced_is_marked_absent(config, state):
    package = review_build.build_package(config, state)
    director = package.item("director")
    assert director is not None
    assert director.exists is False
    assert director.copied_to == ""


def test_a_run_with_no_video_says_so_rather_than_pointing_at_nothing(
    config, tmp_path
):
    state = make_run(config, tmp_path, video=False, run_id="run-novideo")
    package = review_build.build_package(config, state)
    assert package.video_exists is False
    assert any("No video was rendered" in line for line in package.headline)
    assert "Nothing was rendered" in review_index.render_index(package)


def test_a_mocked_render_is_never_treated_as_a_video(config, tmp_path):
    state = make_run(config, tmp_path, run_id="run-mock")
    render = state.stage("render_proxy")
    render.summary["mock"] = True
    package = review_build.build_package(config, state)
    assert package.video_exists is False


# ---------------------------------------------------------------------------
# The five lists
# ---------------------------------------------------------------------------

def test_the_package_answers_all_five_questions(config, state, checks):
    package = review_build.build_package(config, state, checks=checks)
    assert package.headline
    assert package.changed
    assert package.watch_for
    assert package.weak_points
    assert package.decisions_needed


def test_the_headline_carries_a_transcript_summary(config, state):
    """Every layer that reads words is only as good as this line."""
    package = review_build.build_package(config, state)
    line = next(l for l in package.headline if l.startswith("Transcript:"))
    assert "none" in line
    assert "worked blind" in line


def test_a_transcript_that_exists_is_summarised_with_its_source(
    config, tmp_path
):
    state = make_run(config, tmp_path, run_id="run-words")
    state.stages.append(AutoStageResult(
        stage="analyze", status="passed", summary={
            "transcript_words": 1840, "segments_with_speech": 22}))
    package = review_build.build_package(config, state)

    line = next(l for l in package.headline if l.startswith("Transcript:"))
    assert "1840 word(s)" in line
    assert "22 segment(s)" in line
    assert "beside the footage" in line


def test_a_fabricated_transcript_is_never_quietly_summarised(
    config, tmp_path
):
    state = make_run(config, tmp_path, run_id="run-mockwords")
    state.stages.append(AutoStageResult(
        stage="analyze", status="passed", summary={
            "transcript_words": 90, "segments_with_speech": 3}))
    state.stages.append(AutoStageResult(
        stage="transcribe", status="passed", summary={
            "transcribed": 3, "mock": True, "model": "mock"}))
    package = review_build.build_package(config, state)

    line = next(l for l in package.headline if l.startswith("Transcript:"))
    assert "MOCK" in line
    assert "nothing heard the footage" in line


def test_what_changed_is_counts_rather_than_claims(config, state):
    package = review_build.build_package(config, state)
    text = " ".join(package.changed).lower()
    assert "reshaped" in text
    assert "cold open" in text or "opens on" in text
    assert "better" not in text
    assert "improved" not in text


def test_a_cold_open_is_the_first_thing_to_watch_for(config, state):
    package = review_build.build_package(config, state)
    assert "first" in package.watch_for[0]
    assert "11s" in package.watch_for[0]


def test_the_checks_feed_the_weak_points(config, state, checks):
    package = review_build.build_package(config, state, checks=checks)
    assert any("transcript" in line for line in package.weak_points)
    assert package.checks["warned"] == 1


def test_a_blocked_stage_is_a_weak_point(config, state):
    state.stages.append(AutoStageResult(
        stage="review_critique", status="blocked",
        note="the model was not reachable"))
    package = review_build.build_package(config, state)
    assert any("review_critique did not complete" in line
               for line in package.weak_points)


def test_an_unresolved_story_warning_needs_a_human(config, state):
    package = review_build.build_package(config, state)
    assert any("unresolved" in line for line in package.decisions_needed)


def test_a_clean_run_still_says_what_that_does_and_does_not_mean(
    config, tmp_path
):
    state = make_run(config, tmp_path, run_id="run-clean")
    state.stages = [s for s in state.stages if s.stage != "retention_cut"]
    package = review_build.build_package(config, state)
    assert any("not that the edit is good" in line
               for line in package.weak_points)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

def test_the_index_asks_the_five_questions_in_order(config, state, checks):
    package = review_build.build_package(config, state, checks=checks)
    text = review_index.render_index(package)
    headings = [
        "## 1. Watch this",
        "## 2. What changed",
        "## 3. What to watch for",
        "## 4. Weak points",
        "## 5. Needs you",
    ]
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)


def test_the_index_names_the_video_and_the_subtitles(config, state):
    package = review_build.build_package(config, state)
    text = review_index.render_index(package)
    assert package.video in text
    assert "not burned into the video" in text


def test_the_index_says_how_to_get_a_video_when_there_is_none(
    config, tmp_path
):
    state = make_run(config, tmp_path, video=False, run_id="run-noindex")
    package = review_build.build_package(config, state)
    text = review_index.render_index(package)
    assert "Nothing was rendered" in text
    assert "render roughcut" in text


def test_the_index_never_says_the_edit_is_good(config, state, checks):
    package = review_build.build_package(config, state, checks=checks)
    text = review_index.render_index(package).lower()
    assert "nothing in this package says the edit is good" in text
    assert "guaranteed" not in text
    assert "retention improved" not in text


def test_the_index_carries_the_fixes_the_checks_suggested(
    config, state, checks
):
    package = review_build.build_package(config, state, checks=checks)
    text = review_index.render_index(package)
    assert "Fixes the checks suggested" in text
    assert "--transcribe" in text


def test_the_index_lists_what_this_run_did_not_produce(config, state):
    package = review_build.build_package(config, state)
    text = review_index.render_index(package)
    assert "Not produced by this run" in text


def test_the_short_summary_fits_a_terminal(config, state, checks):
    package = review_build.build_package(config, state, checks=checks)
    text = review_index.render_summary(package)
    assert package.run_id in text
    assert "What changed:" in text
    assert "Needs you:" in text


# ---------------------------------------------------------------------------
# Writing and reading
# ---------------------------------------------------------------------------

def test_writing_a_package_leaves_an_index_and_a_json(config, state, checks):
    package, written = review_build.write_package(
        config, state, checks=checks)
    assert review_store.index_path(config, state.run_id).exists()
    assert review_store.package_path(config, state.run_id).exists()
    assert review_store.checks_path(config, state.run_id).exists()
    assert len(written) >= 3


def test_a_package_lives_inside_the_run_it_is_about(config, state):
    review_build.write_package(config, state)
    folder = review_store.package_dir(config, state.run_id)
    assert folder.parent == auto_store.run_dir(config, state.run_id)


def test_a_package_survives_a_round_trip(config, state, checks):
    package, _written = review_build.write_package(
        config, state, checks=checks)
    restored = review_store.load_package(config, state.run_id)
    assert restored.run_id == package.run_id
    assert restored.stats()["items"] == package.stats()["items"]
    assert restored.watch_for == package.watch_for


def test_rebuilding_a_package_says_what_is_true_now(config, state):
    review_build.write_package(config, state)
    Path(state.stage("render_proxy").summary["video"]).unlink()

    rebuilt = review_build.build_package(config, state)
    assert rebuilt.video_exists is False


def test_a_missing_package_says_how_to_build_one(config):
    from editing.errors import EditingError

    with pytest.raises(EditingError) as caught:
        review_store.load_package(config, "nope")
    assert "review package" in (caught.value.hint or "")


def test_the_latest_run_with_a_package_is_findable(config, tmp_path):
    older = make_run(config, tmp_path, run_id="20260101T000000-aaa-style")
    newer = make_run(config, tmp_path, run_id="20260202T000000-bbb-style")
    review_build.write_package(config, older)
    review_build.write_package(config, newer)
    assert review_store.latest_with_package(config) == newer.run_id


def test_no_package_anywhere_is_none_rather_than_an_error(config):
    assert review_store.latest_with_package(config) is None


def test_an_empty_package_still_renders():
    text = review_index.render_index(ReviewPackage(run_id="r1"))
    assert "Review — r1" in text
    assert "## 5. Needs you" in text
