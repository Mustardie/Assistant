"""The reliability gates: pass, warn, fail, and what may stop a run.

Two properties are asserted harder than the individual checks are.

**A warning never stops anything.** The failure mode this package is written
against is a check that refuses to let an overnight run finish over a caption
density -- because a check like that gets disabled, and a disabled check
protects nothing. Only a very short list of conditions may block, and the test
below names that list explicitly so widening it has to be deliberate.

**A gate about a pass that did not run says ``skipped``.** Fifteen green ticks
that mean nothing is worse than five that mean something, so "captions are not
too dense" is never reported as a pass on a run with no captions.

Every check is a pure function of :class:`GateInputs`, so a situation here is
six assignments rather than a pipeline.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from editing.reliability import checks as checks_module
from editing.reliability import report as gate_report
from editing.reliability import run as reliability_run
from editing.reliability.schema import (
    CLEAN, GATE_NAMES, GATE_TITLES, STATUSES, GateInputs, GateReport,
    GateResult, gate,
)


@pytest.fixture
def healthy() -> GateInputs:
    """A run that did everything and did it plausibly."""
    return GateInputs(
        run_id="r1",
        style="cinematic_minecraft",
        footage_folder="/footage/ep12",
        footage_files=3,
        footage_seconds=1800.0,
        transcribed=True,
        transcript_words=4200,
        transcript_files=3,
        transcript_confidence=0.86,
        director_enabled=True,
        director_ran=True,
        director_decisions=20,
        director_accepted=14,
        retention_enabled=True,
        retention_ran=True,
        retention_applied=True,
        cold_open=True,
        cold_open_seconds=12.0,
        hooks_found=4,
        base_duration=900.0,
        cut_duration=780.0,
        source_duration=1800.0,
        clips=42,
        captions_enabled=True,
        captions_placed=9,
        captions_per_minute=0.7,
        caption_ceiling=0.8,
        audio_enabled=True,
        audio_mode="assets",
        cues_placed=8,
        sfx_per_minute=0.6,
        sfx_ceiling=0.8,
        render_enabled=True,
        render_ran=True,
        render_claimed=True,
        render_path="/runs/r1/render.mp4",
        render_exists=True,
        render_size_mb=180.0,
        render_duration=780.0,
        render_planned_duration=780.0,
    )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_every_named_gate_has_a_check_and_a_title():
    assert set(GATE_NAMES) == set(checks_module.CHECKS)
    for name in GATE_NAMES:
        assert GATE_TITLES.get(name), name


def test_a_report_has_one_result_per_gate(healthy):
    report = reliability_run.evaluate(healthy)
    assert len(report) == len(GATE_NAMES)
    assert [r.name for r in report.gates] == list(GATE_NAMES)


def test_every_result_carries_a_valid_status(healthy):
    for result in reliability_run.evaluate(healthy).gates:
        assert result.status in STATUSES


def test_a_healthy_run_passes_everything(healthy):
    report = reliability_run.evaluate(healthy)
    assert report.status == "pass"
    assert report.usable is True
    assert report.failures == []
    assert report.warnings == []


def test_every_warning_and_failure_carries_evidence_and_a_fix():
    """A gate with neither is a complaint rather than a finding."""
    broken = GateInputs(
        run_id="r1", footage_files=1, probe_errors=1,
        cut_duration=10.0, source_duration=1800.0, clips=1,
        captions_enabled=True, captions_placed=8, captions_per_minute=9.0,
        audio_enabled=True, audio_mode="assets", cues_placed=9,
        sfx_per_minute=9.0, missing_assets=3,
        render_enabled=True, render_ran=True, render_claimed=True,
        render_exists=True, render_size_mb=0.01, render_duration=200.0,
        render_planned_duration=10.0,
        retention_enabled=True, retention_ran=True, unresolved_warnings=2,
        director_enabled=True, director_ran=True, director_decisions=8,
    )
    report = reliability_run.evaluate(broken)
    interesting = report.failures + report.warnings
    assert interesting
    for result in interesting:
        assert result.evidence, result.name
        assert result.suggested_fix, result.name


# ---------------------------------------------------------------------------
# What may stop a run
# ---------------------------------------------------------------------------

#: The only conditions that may say "this output is not worth reviewing".
#: Written out so that widening the list has to be a deliberate edit here as
#: well as in the checks.
MAY_BLOCK = {"footage", "retention_length", "compression", "render_output",
             "render_size"}


def test_only_a_short_list_of_checks_may_ever_block(healthy):
    """Run every check against everything that could go wrong with it.

    Asserted by exercising the checks rather than by reading them: what
    matters is which gates can actually come back saying "do not review
    this", and the answer has to stay the five in ``MAY_BLOCK``.
    """
    degradations = [
        {"footage_files": 0},
        {"probe_errors": 3},
        {"transcript_words": 0, "transcribed": False},
        {"transcript_words": 5},
        {"transcript_mock": True},
        {"transcript_confidence": 0.1},
        {"transcript_confidence": -1.0},
        {"hooks_found": 0, "cold_open": False},
        {"cold_open": False},
        {"director_accepted": 0},
        {"director_mock": True},
        {"director_ran": False},
        {"cut_duration": 0.0},
        {"cut_duration": 20.0},
        {"cut_duration": 60.0, "base_duration": 900.0},
        {"duplicate_seconds": 9.0},
        {"unresolved_warnings": 5},
        {"captions_per_minute": 20.0, "captions_placed": 90},
        {"captions_placed": 0, "captions_per_minute": 0.0},
        {"sfx_per_minute": 20.0, "cues_placed": 90},
        {"missing_assets": 12},
        {"audio_mode": "placeholders"},
        {"render_exists": False},
        {"render_size_mb": 0.001},
        {"render_mock": True},
        {"render_ran": False},
        {"render_duration": 12.0},
        {"render_duration": 0.0},
    ]
    for change in degradations:
        report = reliability_run.evaluate(replace(healthy, **change))
        blocking = {result.name for result in report.blocking}
        assert blocking <= MAY_BLOCK, (change, blocking)


def test_a_warning_never_blocks(healthy):
    thin = replace(healthy, transcript_words=3)
    report = reliability_run.evaluate(thin)
    assert report.warnings
    assert report.blocking == []
    assert report.usable is True


def test_a_passing_gate_can_never_be_marked_blocking():
    result = gate("footage", "pass", "fine", can_continue=False)
    assert result.can_continue is True


def test_no_footage_fails_and_blocks():
    report = reliability_run.evaluate(GateInputs(run_id="r1"))
    footage = report.get("footage")
    assert footage.status == "fail"
    assert footage.can_continue is False
    assert report.usable is False
    assert "discover" in footage.suggested_fix


def test_a_render_that_claims_a_video_it_does_not_have_blocks(healthy):
    lying = replace(healthy, render_exists=False)
    report = reliability_run.evaluate(lying)
    assert report.get("render_output").status == "fail"
    assert report.usable is False


def test_a_tiny_rendered_file_blocks(healthy):
    tiny = replace(healthy, render_size_mb=0.01)
    report = reliability_run.evaluate(tiny)
    assert report.get("render_size").status == "fail"
    assert report.get("render_size").can_continue is False


def test_a_cut_with_no_runtime_blocks(healthy):
    empty = replace(healthy, cut_duration=0.0, retention_applied=False)
    report = reliability_run.evaluate(empty)
    assert report.get("compression").status == "fail"
    assert report.usable is False


# ---------------------------------------------------------------------------
# Skipping
# ---------------------------------------------------------------------------

def test_checks_about_passes_that_did_not_run_are_skipped():
    minimal = GateInputs(
        run_id="r1", footage_files=2, footage_seconds=600.0,
        cut_duration=300.0, source_duration=600.0, clips=8,
    )
    report = reliability_run.evaluate(minimal)
    for name in ("director", "retention_length", "cold_open_duplicate",
                 "caption_density", "sfx_density", "render_output",
                 "render_size", "output_duration"):
        assert report.get(name).status == "skipped", name


def test_a_skipped_gate_is_clean_but_not_a_pass():
    minimal = GateInputs(run_id="r1", footage_files=1, cut_duration=60.0,
                         clips=2)
    report = reliability_run.evaluate(minimal)
    skipped = report.get("director")
    assert skipped.status in CLEAN
    assert skipped.status != "pass"


def test_placeholder_audio_skips_the_missing_asset_check(healthy):
    placeholders = replace(healthy, audio_mode="placeholders",
                           missing_assets=0)
    report = reliability_run.evaluate(placeholders)
    assert report.get("missing_assets").status == "skipped"
    assert "placeholder" in report.get("missing_assets").reason


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def test_a_missing_transcript_warns_with_the_flag_that_fixes_it():
    result = checks_module.check_transcript(GateInputs(footage_files=1))
    assert result.status == "warn"
    assert "--transcribe" in result.suggested_fix


def test_a_mock_transcript_is_never_quietly_accepted():
    result = checks_module.check_transcript(
        GateInputs(transcribed=True, transcript_words=900,
                   transcript_mock=True))
    assert result.status == "warn"
    assert "fabricat" in result.reason


def test_low_speech_confidence_warns(healthy):
    result = checks_module.check_transcript_confidence(
        replace(healthy, transcript_confidence=0.3))
    assert result.status == "warn"
    assert result.evidence["confidence"] == 0.3


def test_a_transcript_with_no_confidence_figures_is_skipped(healthy):
    result = checks_module.check_transcript_confidence(
        replace(healthy, transcript_confidence=-1.0))
    assert result.status == "skipped"


def test_no_hook_warns(healthy):
    result = checks_module.check_hook(
        replace(healthy, hooks_found=0, cold_open=False))
    assert result.status == "warn"
    assert "show-hooks" in result.suggested_fix


def test_hooks_found_and_none_used_warns(healthy):
    result = checks_module.check_hook(replace(healthy, cold_open=False))
    assert result.status == "warn"
    assert "show-rejected" in result.suggested_fix


def test_a_director_whose_decisions_were_all_refused_warns(healthy):
    result = checks_module.check_director(
        replace(healthy, director_accepted=0))
    assert result.status == "warn"
    assert "show-rejected" in result.suggested_fix


def test_a_mock_director_warns_even_when_it_accepted_everything(healthy):
    result = checks_module.check_director(replace(healthy, director_mock=True))
    assert result.status == "warn"
    assert "mock" in result.reason.lower()


def test_a_retention_cut_that_deleted_the_episode_warns(healthy):
    result = checks_module.check_retention_length(
        replace(healthy, cut_duration=100.0, base_duration=900.0))
    assert result.status == "warn"
    assert result.evidence["share"] < 0.35
    # Warned rather than failed: an aggressive cut is a real thing to want.
    assert result.can_continue is True


def test_duplicate_cold_open_footage_warns(healthy):
    result = checks_module.check_cold_open_duplicate(
        replace(healthy, duplicate_seconds=6.0))
    assert result.status == "warn"
    assert "plays again" in result.reason
    assert result.evidence["duplicate_seconds"] == 6.0


def test_unresolved_story_warnings_are_reported(healthy):
    result = checks_module.check_story_warnings(
        replace(healthy, unresolved_warnings=3))
    assert result.status == "warn"
    assert result.evidence["unresolved"] == 3


def test_captions_above_the_hard_rate_warn(healthy):
    result = checks_module.check_caption_density(
        replace(healthy, captions_per_minute=9.0, captions_placed=40))
    assert result.status == "warn"
    assert "key_moments" in result.suggested_fix


def test_no_captions_is_a_pass_not_a_warning(healthy):
    result = checks_module.check_caption_density(
        replace(healthy, captions_placed=0, captions_per_minute=0.0))
    assert result.status == "pass"
    assert "normal result" in result.reason


def test_effects_above_the_hard_rate_warn(healthy):
    result = checks_module.check_sfx_density(
        replace(healthy, sfx_per_minute=9.0, cues_placed=60))
    assert result.status == "warn"
    assert "--max-sfx-per-minute" in result.suggested_fix


def test_missing_assets_warn_with_the_shopping_list_command(healthy):
    result = checks_module.check_missing_assets(
        replace(healthy, missing_assets=4))
    assert result.status == "warn"
    assert "show-missing" in result.suggested_fix


def test_a_suspicious_output_duration_warns(healthy):
    result = checks_module.check_output_duration(
        replace(healthy, render_duration=600.0))
    assert result.status == "warn"
    assert result.evidence["drift"] == 180.0


def test_a_second_of_drift_on_a_long_cut_is_fine(healthy):
    result = checks_module.check_output_duration(
        replace(healthy, render_duration=781.0))
    assert result.status == "pass"


def test_a_mocked_render_warns_and_says_no_video_exists(healthy):
    result = checks_module.check_render_output(
        replace(healthy, render_mock=True))
    assert result.status == "warn"
    assert "placeholder" in result.reason


def test_a_check_that_raises_becomes_a_skipped_gate(monkeypatch, healthy):
    """One broken check must not cost the other fourteen."""
    def explode(_inputs):
        raise RuntimeError("boom")

    monkeypatch.setitem(checks_module.CHECKS, "footage", explode)
    report = reliability_run.evaluate(healthy)
    assert len(report) == len(GATE_NAMES)
    assert report.get("footage").status == "skipped"
    assert "RuntimeError" in report.get("footage").reason


# ---------------------------------------------------------------------------
# Reporting and serialisation
# ---------------------------------------------------------------------------

def test_the_report_leads_with_what_blocks(healthy):
    report = reliability_run.evaluate(replace(healthy, render_exists=False))
    text = gate_report.render(report)
    assert "THE OUTPUT IS NOT USABLE" in text
    assert text.index("THE OUTPUT IS NOT USABLE") < text.index("EVERY CHECK")


def test_the_report_never_claims_the_edit_is_good(healthy):
    text = gate_report.render(reliability_run.evaluate(healthy))
    lowered = text.lower()
    assert "shape, not at taste" in lowered
    assert "guaranteed" not in lowered
    assert "retention improved" not in lowered


def test_the_short_form_is_only_the_things_that_are_wrong(healthy):
    clean = gate_report.render_short(reliability_run.evaluate(healthy))
    assert "All reliability checks passed" in clean

    noisy = gate_report.render_short(
        reliability_run.evaluate(replace(healthy, transcript_words=2)))
    assert "transcript" in noisy


def test_a_report_survives_a_round_trip(healthy):
    report = reliability_run.evaluate(healthy)
    restored = GateReport.from_dict(report.to_dict())
    assert restored.stats() == report.stats()
    assert [r.name for r in restored.gates] == [r.name for r in report.gates]


def test_a_result_survives_a_round_trip():
    result = gate("footage", "warn", "why", evidence={"files": 2},
                  fix="do this")
    restored = GateResult.from_dict(result.to_dict())
    assert restored.status == "warn"
    assert restored.evidence == {"files": 2}
    assert restored.suggested_fix == "do this"


def test_gate_inputs_survive_a_round_trip(healthy):
    restored = GateInputs.from_dict(healthy.to_dict())
    assert restored.cut_duration == healthy.cut_duration
    assert restored.transcript_confidence == healthy.transcript_confidence
