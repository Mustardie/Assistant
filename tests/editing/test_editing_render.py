"""Rendering a rough cut to something you can actually watch.

Five properties carry the weight here.

**A render is never faked.** ``rendered`` is True only when a real encoder
produced a file with bytes in it. The mock runner completes, writes a
placeholder and comes back ``mocked`` with ``rendered=False``, and says so on
the result, in the report and at the top of the review notes.

**What FFmpeg cannot show is said, and the cut still renders.** By Session 6 a
rough cut carries markers, captions, sound effects and graphics; refusing to
render it would make this package useless exactly when it became valuable.

**The cache is keyed on everything that changes a frame.** The cut, the source
bytes, the settings and the FFmpeg build. Changing any of them must miss;
changing a timeout or the notes interval must not. Both directions are tested,
because a cache that over-hits shows you a video of a cut you no longer have.

**Every segment gets an audio stream, even the silent ones.** A clip with no
microphone track gets ``anullsrc``, because the concat demuxer refuses to join
files whose stream layouts differ -- which is the single most common way a
naive version of this fails.

**A failure is a record with a folder behind it.** A missing FFmpeg, a
disconnected drive and a clip the encoder refused all come back as a job with
a failure, a hint, and the commands on disk.

Nothing here needs FFmpeg, a GPU, Premiere, Whisper or real footage: every
subprocess goes through an injected runner.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from editing.errors import EditingError, ToolMissingError
from editing.render import commands as commands_module
from editing.render import convert as convert_module
from editing.render import notes as notes_module
from editing.render import report as report_module
from editing.render import run as run_module
from editing.render import sources as sources_module
from editing.render import store as store_module
from editing.render.runner import CommandResult, FFmpegRunner, MockRunner
from editing.render.schema import (
    BACKENDS, INSTALL_HINT, QUALITIES, QUALITY_SETTINGS, RenderArtifact,
    RenderConfig, RenderFailure, RenderInput, RenderJob, RenderResult,
    RenderSegment, job_id_for, segment_id_for,
)
from editing.roughcut.schema import ClipPlacement, RoughCutPlan, SequenceMarker


# ---------------------------------------------------------------------------
# A runner that behaves like FFmpeg without being it
# ---------------------------------------------------------------------------

class FakeRunner:
    """Records commands, writes real output files, runs nothing.

    Deliberately not named ``mock``: the render path treats a runner called
    ``mock`` as producing placeholders, and most of these tests need to
    exercise the *successful* path where a real video is claimed to exist.
    """

    name = "fake"

    def __init__(
        self,
        *,
        available: bool = True,
        fail_on: tuple = (),
        encoders: tuple = ("libx264", "libx265", "aac"),
        version: str = "6.1-fake",
        probe: dict = None,
        write_output: bool = True,
    ):
        self.ffmpeg = "ffmpeg"
        self.ffprobe = "ffprobe"
        self.commands: list[list[str]] = []
        self._available = available
        self._fail_on = tuple(fail_on)
        self._encoders = set(encoders)
        self._version = version
        self._write_output = write_output
        self._probe = probe if probe is not None else {
            "duration": 0.0, "width": 1280, "height": 720, "fps": 30.0,
            "has_audio": True,
        }

    def available(self) -> bool:
        return self._available

    def version(self) -> str:
        return self._version

    def encoders(self) -> set:
        return set(self._encoders)

    def run(self, command, *, timeout=1800.0, log_path=None) -> CommandResult:
        from editing.render.runner import _log

        parts = [str(part) for part in command]
        self.commands.append(parts)
        line = " ".join(parts)
        for needle in self._fail_on:
            if needle in line:
                result = CommandResult(command=parts, returncode=1,
                                       stderr=f"fake failure on {needle}")
                _log(log_path, result)
                return result
        target = Path(parts[-1])
        if self._write_output and target.suffix:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake video payload" * 32)
        result = CommandResult(command=parts, returncode=0)
        _log(log_path, result)
        return result

    def probe(self, path, *, timeout=120.0) -> dict:
        info = dict(self._probe)
        if not info.get("duration"):
            # Answer with what the plan expects unless a test overrode it, so
            # drift assertions are about drift and not about the fake.
            info["duration"] = 0.0
        return info

    def health(self) -> dict:
        return {"backend": self.name, "ready": self._available,
                "version": self._version, "hint": ""}

    # -- helpers for assertions ------------------------------------------

    @property
    def encode_commands(self) -> list[list[str]]:
        return [c for c in self.commands if "-f" not in c or "concat" not in c]

    @property
    def concat_commands(self) -> list[list[str]]:
        return [c for c in self.commands if "concat" in " ".join(c)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def footage(tmp_path) -> dict:
    """Two files that exist on disk. Contents are irrelevant; sizes are not."""
    folder = tmp_path / "footage"
    folder.mkdir()
    first = folder / "ep12_part1.mp4"
    first.write_bytes(b"clip a" * 4096)
    second = folder / "ep12_part2.mp4"
    second.write_bytes(b"clip b" * 2048)
    return {"a": first, "b": second}


def placement(
    source, start, source_in, source_out, *, speed=1.0, index=0,
    track="V1", keep_reason="payoff", protected=False, recommendations=(),
    placement_id="",
) -> ClipPlacement:
    return ClipPlacement(
        placement_id=placement_id or f"p_{index}",
        asset_id="a_test",
        source_file=str(source),
        source_in=source_in,
        source_out=source_out,
        sequence_start=start,
        track=track,
        index=index,
        speed=speed,
        keep_reason=keep_reason,
        protected=protected,
        recommendation_ids=list(recommendations),
    )


def a_plan(footage, *, ops=(), markers=()) -> RoughCutPlan:
    """A three-clip cut: a hold, a sped-up grind, and the payoff."""
    return RoughCutPlan(
        sequence_name="Nova Rough Cut",
        placements=[
            placement(footage["a"], 0.0, 10.0, 20.0, index=0,
                      keep_reason="setup", recommendations=["rec_1"]),
            placement(footage["b"], 10.0, 5.0, 25.0, index=1, speed=2.0,
                      keep_reason="filler"),
            placement(footage["a"], 20.0, 60.0, 68.0, index=2,
                      keep_reason="payoff", protected=True),
        ],
        ops=[dict(op) for op in ops],
        markers=[SequenceMarker(time=t, name=n) for t, n in markers],
    )


@pytest.fixture
def plan(footage) -> RoughCutPlan:
    return a_plan(footage)


def render(config, plan, runner=None, **kwargs):
    """Render with a fake runner and settings that are easy to assert on."""
    settings = kwargs.pop("settings", None) or RenderConfig()
    return run_module.render_plan(
        config, plan,
        settings=settings.validated(),
        runner=runner if runner is not None else FakeRunner(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_a_quality_preset_sets_crf_and_encoder_preset_together():
    for name in QUALITIES:
        settings = RenderConfig(quality=name).validated()
        crf, preset = QUALITY_SETTINGS[name]
        assert settings.crf == crf
        assert settings.preset == preset


def test_an_explicit_crf_beats_the_preset():
    assert RenderConfig(quality="proxy", crf=15).validated().crf == 15


def test_nonsense_settings_clamp_rather_than_raise():
    settings = RenderConfig(
        backend="banana", quality="cinematic", scale_mode="squish",
        video_encoder="magic", audio_encoder="flac", fps=9000,
        sample_rate=1, audio_channels=0, max_segments=0, container="",
    ).validated()

    assert settings.backend == "ffmpeg"
    assert settings.quality == "proxy"
    assert settings.scale_mode == "pad"
    assert settings.video_encoder == "auto"
    assert settings.audio_encoder == "aac"
    assert settings.fps == 240.0, "clamped, not rejected"
    assert settings.sample_rate == 8000
    assert settings.audio_channels == 1
    assert settings.max_segments == 1
    assert settings.container == "mp4"


def test_the_width_is_derived_from_the_height_and_stays_even():
    assert RenderConfig(height=720).validated().width == 1280
    assert RenderConfig(height=1080).validated().width == 1920
    # H.264 with 4:2:0 chroma cannot encode odd dimensions.
    odd = RenderConfig(height=719).validated()
    assert odd.height % 2 == 0 and odd.width % 2 == 0


def test_height_zero_means_keep_the_source_size_and_says_it_is_risky():
    settings = RenderConfig(height=0).validated()
    assert settings.width == 0 and not settings.scales
    assert any("will fail to join" in w for w in settings.warnings)


def test_settings_warn_about_the_choices_that_bite():
    assert any("out of sync" in w for w in RenderConfig(fps=0).warnings)
    assert any("judged wrong" in w
               for w in RenderConfig(include_audio=False).warnings)
    assert any("hardware encoder" in w
               for w in RenderConfig(video_encoder="h264_nvenc").warnings)
    assert any("MOCK" in w for w in RenderConfig(backend="mock").warnings)
    assert RenderConfig().validated().warnings == []


def test_a_hardware_encoder_uses_its_own_quality_flag_and_preset():
    settings = RenderConfig(video_encoder="h264_nvenc").validated()
    assert settings.quality_flag == "-cq", "nvenc ignores -crf silently"
    assert settings.encoder_preset == "p4"
    assert settings.is_hardware

    software = RenderConfig().validated()
    assert software.quality_flag == "-crf"
    assert software.resolved_encoder == "libx264"
    assert not software.is_hardware


def test_the_cache_key_ignores_settings_that_change_no_pixel():
    base = RenderConfig().validated()
    for field, value in (
        ("keep_temp", True), ("use_cache", False), ("segment_timeout", 30.0),
        ("concat_timeout", 60.0), ("notes_interval", 45.0),
    ):
        changed = replace(base, **{field: value}).validated()
        assert changed.cache_key_part() == base.cache_key_part(), field


def test_the_cache_key_notices_settings_that_do():
    base = RenderConfig().validated()
    for field, value in (
        ("height", 480), ("quality", "high"), ("fps", 60.0),
        ("include_audio", False), ("scale_mode", "crop"),
        ("video_encoder", "libx265"), ("backend", "mock"),
        ("max_seconds", 60.0), ("audio_channels", 1),
    ):
        changed = replace(base, **{field: value}).validated()
        assert changed.cache_key_part() != base.cache_key_part(), field


def test_config_round_trips_through_a_dict():
    settings = RenderConfig(quality="high", height=1080, fps=60).validated()
    assert RenderConfig.from_dict(settings.to_dict()).validated() == settings


def test_config_from_a_dict_ignores_keys_it_does_not_know():
    settings = RenderConfig.from_dict({"height": 480, "wat": "no"})
    assert settings.height == 480


# ---------------------------------------------------------------------------
# Schema records
# ---------------------------------------------------------------------------

def test_a_segment_computes_its_own_durations():
    segment = RenderSegment(
        source_in=10.0, source_out=30.0,
        timeline_in=5.0, timeline_out=15.0, speed=2.0,
    )
    assert segment.source_duration == 20.0
    assert segment.duration == 10.0
    assert segment.has_speed_change
    assert not segment.is_empty


def test_a_segment_round_trips_and_repairs_reversed_times():
    broken = RenderSegment.from_dict({
        "source_in": 30.0, "source_out": 5.0,
        "timeline_in": 9.0, "timeline_out": 1.0,
        "speed": 0, "recommendation_ids": "one_id", "warnings": None,
    })
    assert broken.source_out == 30.0, "a reversed range collapses, never wraps"
    assert broken.timeline_out == 9.0
    assert broken.speed == 1.0, "a zero speed would divide by zero downstream"
    # A bare string is one ID, not a list of characters.
    assert broken.recommendation_ids == ["one_id"]
    assert broken.warnings == []


def test_a_segment_id_is_stable_and_leads_with_its_index():
    first = segment_id_for(3, "p_7", 12.5)
    assert first == segment_id_for(3, "p_7", 12.5)
    assert first.startswith("s0003_")
    assert first != segment_id_for(3, "p_7", 12.6)


def test_a_job_id_is_stable_for_one_plan_and_one_key():
    assert job_id_for("structure", "abc") == job_id_for("structure", "abc")
    assert job_id_for("structure", "abc") != job_id_for("structure", "abd")
    assert job_id_for("structure", "abc").startswith("structure-")


def test_an_input_is_unusable_when_it_is_missing_or_empty():
    assert RenderInput(exists=True, size_bytes=10).usable
    assert not RenderInput(exists=True, size_bytes=0).usable
    assert not RenderInput(exists=False, size_bytes=10).usable


def test_an_input_cache_key_leaves_mtime_out():
    item = RenderInput(path="a.mp4", size_bytes=10, mtime=1.0,
                       content_hash="h")
    moved = RenderInput(path="a.mp4", size_bytes=10, mtime=99999.0,
                        content_hash="h")
    assert item.cache_key_part() == moved.cache_key_part(), \
        "copying a file to another drive must not cost an hour of re-encoding"


def test_a_result_only_claims_rendered_when_it_means_it():
    assert not RenderResult().rendered
    assert not RenderResult(status="failed", rendered=False).ok
    assert RenderResult(status="rendered", rendered=True).ok
    assert not RenderResult(status="mocked", mock=True).ok


def test_realtime_factor_is_zero_for_anything_that_did_not_encode():
    real = RenderResult(planned_duration=60.0, elapsed=30.0)
    assert real.realtime_factor == 2.0
    assert replace(real, from_cache=True).realtime_factor == 0.0
    assert replace(real, mock=True).realtime_factor == 0.0


def test_duration_drift_needs_both_numbers():
    assert RenderResult(planned_duration=10.0).duration_drift == 0.0
    assert RenderResult(measured_duration=10.0).duration_drift == 0.0
    assert RenderResult(planned_duration=10.0,
                        measured_duration=10.5).duration_drift == 0.5


def test_a_failure_round_trips_and_coerces_an_unknown_stage():
    failure = RenderFailure.from_dict({"stage": "banana", "message": "no"})
    assert failure.stage == "unknown"
    assert RenderFailure.from_dict(None) is None
    assert "no" in failure.render()


def test_a_job_round_trips_through_a_dict(footage, plan):
    job = RenderJob(job_id="j", plan_name="structure",
                    segments=convert_module.to_segments(plan).segments)
    restored = RenderJob.from_dict(job.to_dict())
    # ``segments`` live in their own file, so the job dict does not carry them.
    assert restored.job_id == "j"
    assert restored.stats()["segments"] == 0


def test_an_artifact_describes_a_file_that_is_there(tmp_path):
    target = tmp_path / "render.mp4"
    target.write_bytes(b"x" * 2048)
    artifact = RenderArtifact.describe(target, kind="video")
    assert artifact.exists and artifact.size_bytes == 2048
    assert RenderArtifact.describe(tmp_path / "nope.mp4").exists is False


# ---------------------------------------------------------------------------
# Conversion: plan -> segments
# ---------------------------------------------------------------------------

def test_a_plan_becomes_segments_that_play_back_to_back(plan):
    result = convert_module.to_segments(plan)

    assert len(result) == 3
    assert [s.index for s in result.segments] == [0, 1, 2]
    # 10s + (20s at 2x) + 8s
    assert [s.duration for s in result.segments] == [10.0, 10.0, 8.0]
    assert [s.timeline_in for s in result.segments] == [0.0, 10.0, 20.0]
    assert result.duration == 28.0


def test_segments_are_ordered_by_sequence_position_not_list_order(footage):
    plan = RoughCutPlan(placements=[
        placement(footage["a"], 20.0, 60.0, 65.0, index=2),
        placement(footage["a"], 0.0, 10.0, 12.0, index=0),
        placement(footage["b"], 10.0, 1.0, 4.0, index=1),
    ])
    result = convert_module.to_segments(plan)

    # A pass that appends a placement without re-sorting must not be able to
    # make a rendered cut play in a different order than the same plan
    # executed in Premiere.
    assert [s.source_in for s in result.segments] == [10.0, 1.0, 60.0]
    assert [s.timeline_in for s in result.segments] == [0.0, 2.0, 5.0]


def test_source_ranges_survive_conversion_exactly(plan):
    first = convert_module.to_segments(plan).segments[0]
    assert (first.source_in, first.source_out) == (10.0, 20.0)
    assert first.placement_id == "p_0"
    assert first.recommendation_ids == ["rec_1"]
    assert first.keep_reason == "setup"


def test_a_protected_placement_stays_marked_as_one(plan):
    payoff = convert_module.to_segments(plan).segments[2]
    assert payoff.protected
    assert "protected" in payoff.label


def test_placements_on_other_tracks_are_skipped_with_a_reason(footage):
    plan = RoughCutPlan(placements=[
        placement(footage["a"], 0.0, 0.0, 5.0, index=0),
        placement(footage["b"], 0.0, 0.0, 5.0, index=1, track="V2"),
        placement(footage["b"], 0.0, 0.0, 5.0, index=2, track="A2"),
    ])
    result = convert_module.to_segments(plan)

    assert len(result) == 1
    assert result.dropped == 2
    assert any("flat proxy has one video stream" in w for w in result.warnings)


def test_a_range_too_short_to_be_a_shot_is_dropped(footage):
    plan = RoughCutPlan(placements=[
        placement(footage["a"], 0.0, 1.0, 1.01, index=0),
        placement(footage["a"], 0.0, 2.0, 6.0, index=1),
    ])
    result = convert_module.to_segments(plan)

    assert len(result) == 1
    assert result.dropped == 1
    assert any("shorter than" in w for w in result.warnings)


def test_a_placement_with_no_source_file_is_skipped_not_crashed():
    plan = RoughCutPlan(placements=[
        ClipPlacement(placement_id="p_0", asset_id="a", source_file="",
                      source_in=0.0, source_out=5.0, sequence_start=0.0),
    ])
    result = convert_module.to_segments(plan)
    assert len(result) == 0
    assert any("names no source file" in w for w in result.warnings)


def test_an_empty_plan_says_how_to_build_one():
    result = convert_module.to_segments(RoughCutPlan())
    assert len(result) == 0
    assert any("roughcut build" in w for w in result.warnings)


def test_muting_a_placement_turns_its_audio_off(plan):
    result = convert_module.to_segments(plan, muted_placements=["p_1"])
    assert [s.audio_enabled for s in result.segments] == [True, False, True]
    assert any("audio muted" in w for w in result.warnings)


def test_audio_off_in_the_config_mutes_everything(plan):
    result = convert_module.to_segments(plan, include_audio=False)
    assert not any(s.audio_enabled for s in result.segments)


def test_source_overrides_move_footage_without_touching_the_plan(
        footage, plan):
    moved = str(footage["b"])
    result = convert_module.to_segments(
        plan, source_overrides={str(footage["a"]): moved})

    assert all(s.source_path == moved for s in result.segments)
    assert plan.placements[0].source_file == str(footage["a"]), \
        "the plan on disk is left alone"


# -- speed -------------------------------------------------------------------

def test_a_speed_change_shortens_the_timeline_not_the_source(footage):
    plan = RoughCutPlan(placements=[
        placement(footage["a"], 0.0, 0.0, 30.0, speed=3.0, index=0),
    ])
    segment = convert_module.to_segments(plan).segments[0]

    assert segment.source_duration == 30.0
    assert segment.duration == 10.0


def test_a_slow_down_lengthens_it(footage):
    plan = RoughCutPlan(placements=[
        placement(footage["a"], 0.0, 0.0, 10.0, speed=0.5, index=0),
    ])
    assert convert_module.to_segments(plan).segments[0].duration == 20.0


def test_an_unsupported_speed_falls_back_to_1x_and_says_so():
    rate, notes = convert_module.resolve_speed(40.0, label="clip.mp4")
    assert rate == 1.0, "clamping to 8x would look like a bug in the cut"
    assert any("outside the" in note for note in notes)

    rate, notes = convert_module.resolve_speed(0.0)
    assert rate == 1.0 and notes

    rate, notes = convert_module.resolve_speed("fast")
    assert rate == 1.0 and notes

    assert convert_module.resolve_speed(2.0) == (2.0, [])


def test_an_impossible_speed_in_a_plan_still_renders_the_clip(footage):
    plan = RoughCutPlan(placements=[
        placement(footage["a"], 0.0, 0.0, 10.0, speed=40.0, index=0),
    ])
    result = convert_module.to_segments(plan)

    assert len(result) == 1
    assert result.segments[0].speed == 1.0
    assert result.segments[0].duration == 10.0
    assert any("rendered at 1x" in w for w in result.warnings)


# -- truncation --------------------------------------------------------------

def test_max_seconds_cuts_the_segment_that_straddles_the_boundary(plan):
    result = convert_module.to_segments(plan, max_seconds=15.0)

    assert len(result) == 2
    assert result.segments[1].timeline_out == 15.0
    # 5s of timeline at 2x is 10s of source.
    assert result.segments[1].source_out == 15.0
    assert any("cut short" in w for w in result.segments[1].warnings)
    assert any("--max-seconds" in w for w in result.warnings)


def test_max_seconds_longer_than_the_cut_changes_nothing(plan):
    assert len(convert_module.to_segments(plan, max_seconds=600.0)) == 3


# -- unsupported features ----------------------------------------------------

def test_the_features_a_flat_proxy_cannot_show_are_listed_once_each(footage):
    plan = a_plan(footage, ops=[
        {"op": "sequence.create"}, {"op": "clip.append"},
        {"op": "text.create"}, {"op": "text.create"},
        {"op": "audio.duck"}, {"op": "graphic.image"},
    ])
    unsupported = convert_module.describe_unsupported(plan)
    joined = " ".join(unsupported)

    assert "2 x text and captions" in joined
    assert "ducking under speech" in joined
    assert "graphic overlays" in joined
    assert "sequence.create" not in joined, "the assembly is represented"
    assert "clip.append" not in joined


def test_markers_are_named_as_missing_and_pointed_at_the_notes(footage):
    plan = a_plan(footage, markers=[(3.0, "danger"), (9.0, "payoff")])
    unsupported = convert_module.describe_unsupported(plan)
    assert any("2 sequence marker(s)" in item for item in unsupported)
    assert any("review notes" in item for item in unsupported)


def test_markers_are_counted_once_not_from_both_sides(footage):
    """``marker.add`` operations and ``plan.markers`` are the same markers.

    Reporting both said the same thing twice, with two different numbers.
    """
    plan = a_plan(
        footage,
        ops=[{"op": "marker.add"}] * 10,
        markers=[(float(i), f"m{i}") for i in range(10)],
    )
    unsupported = convert_module.describe_unsupported(plan)
    marker_lines = [item for item in unsupported if "marker" in item]

    assert len(marker_lines) == 1
    assert "10 sequence marker(s)" in marker_lines[0]


def test_markers_are_reported_even_when_only_the_operations_carry_them(
        footage):
    plan = a_plan(footage, ops=[{"op": "marker.add"}] * 3)
    assert any("3 sequence marker(s)" in item
               for item in convert_module.describe_unsupported(plan))


def test_an_operation_nobody_has_heard_of_is_still_reported(footage):
    plan = a_plan(footage, ops=[{"op": "warp.stabilise"}])
    assert any("warp.stabilise" in item
               for item in convert_module.describe_unsupported(plan))


def test_rough_cut_warnings_are_carried_into_the_render(footage):
    plan = a_plan(footage)
    plan.warnings.append("two ranges overlapped and were merged")
    result = convert_module.to_segments(plan)
    assert any("two ranges overlapped" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------

def _flag(command, name):
    """The value after ``name`` in a command, or None."""
    parts = [str(p) for p in command]
    return parts[parts.index(name) + 1] if name in parts else None


def test_a_segment_command_seeks_before_the_input(plan):
    segment = convert_module.to_segments(plan).segments[0]
    command = commands_module.segment_command(
        segment, "out.mp4", RenderConfig().validated())
    parts = [str(p) for p in command]

    # ``-ss`` and ``-t`` before ``-i`` is what makes a 10-second segment from
    # the middle of a 40-minute file cost 10 seconds of decoding.
    assert parts.index("-ss") < parts.index("-i")
    assert parts.index("-t") < parts.index("-i")
    assert _flag(command, "-ss") == "10"
    assert _flag(command, "-t") == "10"


def test_a_segment_command_scales_pads_and_fixes_the_pixel_aspect(plan):
    segment = convert_module.to_segments(plan).segments[0]
    filters = _flag(commands_module.segment_command(
        segment, "out.mp4", RenderConfig().validated()), "-vf")

    assert "scale=1280:720:force_original_aspect_ratio=decrease" in filters
    assert "pad=1280:720" in filters
    assert "setsar=1" in filters, "non-square pixels display stretched"
    assert "fps=30" in filters


def test_crop_and_stretch_modes_produce_their_own_filters():
    settings = RenderConfig(scale_mode="crop").validated()
    filters = ",".join(commands_module.scale_filters(settings))
    assert "force_original_aspect_ratio=increase" in filters
    assert "crop=1280:720" in filters

    stretched = ",".join(commands_module.scale_filters(
        RenderConfig(scale_mode="stretch").validated()))
    assert stretched.startswith("scale=1280:720")
    assert "pad=" not in stretched

    assert commands_module.scale_filters(
        RenderConfig(height=0).validated()) == []


def test_a_speed_change_becomes_setpts_and_a_chained_atempo(plan):
    segment = convert_module.to_segments(plan).segments[1]
    command = commands_module.segment_command(
        segment, "out.mp4", RenderConfig().validated())

    assert _flag(command, "-vf").startswith("setpts=0.5*PTS")
    assert "atempo=2" in _flag(command, "-af")


def test_atempo_chains_beyond_what_one_filter_can_do():
    assert commands_module.atempo_chain(1.0) == []
    assert commands_module.atempo_chain(2.0) == ["atempo=2"]
    assert commands_module.atempo_chain(4.0) == ["atempo=2", "atempo=2"]
    assert commands_module.atempo_chain(0.25) == ["atempo=0.5", "atempo=0.5"]

    # The factors multiply back to the speed asked for -- which is what makes
    # the audio land with its own video rather than near it.
    chain = commands_module.atempo_chain(3.0)
    product = 1.0
    for entry in chain:
        product *= float(entry.split("=")[1])
    assert product == pytest.approx(3.0)


def test_every_audio_chain_ends_with_a_resample(plan):
    segment = convert_module.to_segments(plan).segments[0]
    chain = commands_module.audio_filters(segment, RenderConfig().validated())
    # Capture software starts audio late; without this the offset accumulates
    # across a hundred segments into visible desync.
    assert chain[-1] == "aresample=async=1:first_pts=0"
    assert any("aformat=" in entry for entry in chain)


def test_a_source_with_no_audio_gets_a_generated_silent_track(plan):
    segment = convert_module.to_segments(plan).segments[0]
    command = commands_module.segment_command(
        segment, "out.mp4", RenderConfig().validated(),
        source_has_audio=False)
    line = " ".join(command)

    # The concat demuxer refuses to join files whose stream layouts differ,
    # so a clip recorded without a microphone would break the whole render.
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in line
    assert "-map 0:v:0 -map 1:a:0" in line
    assert "-shortest" in command


def test_a_muted_segment_also_gets_silence_rather_than_no_track(plan):
    segment = convert_module.to_segments(
        plan, muted_placements=["p_0"]).segments[0]
    line = " ".join(commands_module.segment_command(
        segment, "out.mp4", RenderConfig().validated()))
    assert "anullsrc" in line


def test_audio_off_still_produces_a_uniform_audio_stream(plan):
    settings = RenderConfig(include_audio=False).validated()
    segment = convert_module.to_segments(plan, include_audio=False).segments[0]
    assert "anullsrc" in " ".join(
        commands_module.segment_command(segment, "out.mp4", settings))


def test_a_source_with_audio_maps_its_own_stream(plan):
    segment = convert_module.to_segments(plan).segments[0]
    line = " ".join(commands_module.segment_command(
        segment, "out.mp4", RenderConfig().validated(),
        source_has_audio=True))
    assert "-map 0:v:0 -map 0:a:0" in line
    assert "anullsrc" not in line


def test_encoder_arguments_follow_the_encoder_dialect():
    software = commands_module.video_encoder_args(
        RenderConfig(quality="high").validated())
    assert software[:2] == ["-c:v", "libx264"]
    assert "-crf" in software and "18" in software
    assert "-preset" in software

    hardware = commands_module.video_encoder_args(
        RenderConfig(video_encoder="h264_nvenc").validated())
    assert "-cq" in hardware and "-crf" not in hardware
    assert "p4" in hardware


def test_audio_encoder_arguments_pin_the_stream_shape():
    args = commands_module.audio_encoder_args(RenderConfig().validated())
    assert args[:2] == ["-c:a", "aac"]
    assert "-ar" in args and "48000" in args
    assert "-ac" in args and "2" in args
    # Bitrate is meaningless for PCM and FFmpeg complains about it.
    assert "-b:a" not in commands_module.audio_encoder_args(
        RenderConfig(audio_encoder="pcm_s16le").validated())


def test_the_concat_list_escapes_quotes_the_way_the_demuxer_wants():
    text = commands_module.concat_list_text(
        ["C:/Users/o'brien/a.mp4", "b.mp4"])
    assert "file 'C:/Users/o'\\''brien/a.mp4'" in text
    assert text.endswith("\n")


def test_the_join_is_a_stream_copy_and_the_fallback_re_encodes():
    settings = RenderConfig().validated()
    copy = commands_module.concat_command("list.txt", "out.mp4", settings)
    assert "-c" in copy and "copy" in copy
    assert "+faststart" in copy

    reencoded = commands_module.concat_command(
        "list.txt", "out.mp4", settings, reencode=True)
    assert "libx264" in reencoded
    assert "copy" not in reencoded


def test_numbers_in_commands_never_come_out_in_scientific_notation():
    assert commands_module._num(0.0000001) == "0"
    assert commands_module._num(1.5) == "1.5"
    assert commands_module._num(30) == "30"


def test_a_command_can_be_rendered_as_a_pasteable_line():
    line = commands_module.render_command_line(
        ["ffmpeg", "-i", "C:/a b/c.mp4"])
    assert '"C:/a b/c.mp4"' in line


# ---------------------------------------------------------------------------
# Sources and the cache key
# ---------------------------------------------------------------------------

def test_each_source_is_measured_once_however_often_it_is_used(footage, plan):
    segments = convert_module.to_segments(plan).segments
    runner = FakeRunner()
    inputs, warnings = sources_module.describe_inputs(
        segments, runner=runner)

    assert len(inputs) == 2, "one entry per file, not per segment"
    assert [item.segments for item in inputs] == [2, 1]
    assert all(item.content_hash for item in inputs)
    assert warnings == []


def test_a_missing_source_is_described_rather_than_raised(tmp_path):
    segments = [RenderSegment(source_path=str(tmp_path / "gone.mp4"))]
    inputs, warnings = sources_module.describe_inputs(segments, probe=False)

    assert not inputs[0].usable
    assert any("not there any more" in w for w in warnings)


def test_a_probe_that_finds_no_audio_says_the_segments_get_silence(
        footage, plan):
    runner = FakeRunner(probe={"duration": 60.0, "has_audio": False,
                               "width": 1920, "height": 1080, "fps": 60.0})
    inputs, warnings = sources_module.describe_inputs(
        convert_module.to_segments(plan).segments, runner=runner)

    assert not inputs[0].has_audio
    assert any("no audio track" in w for w in warnings)


def test_a_probe_that_fails_assumes_audio_and_says_so(footage, plan):
    class Broken(FakeRunner):
        def probe(self, path, *, timeout=120.0):
            raise RuntimeError("ffprobe exploded")

    inputs, warnings = sources_module.describe_inputs(
        convert_module.to_segments(plan).segments, runner=Broken())

    # Assuming audio and being wrong produces one clear error naming the clip;
    # assuming silence and being wrong throws the commentary away.
    assert all(item.has_audio for item in inputs)
    assert any("Assuming it has an audio track" in w for w in warnings)


def test_a_range_past_the_end_of_a_file_is_flagged(footage, plan):
    segments = convert_module.to_segments(plan).segments
    inputs = [RenderInput(path=str(footage["a"]), duration=30.0, exists=True,
                          size_bytes=10)]
    warnings = sources_module.check_ranges(segments, inputs)
    assert any("only 30.0s long" in w for w in warnings)


def test_a_plan_fingerprint_survives_a_trip_through_json(footage):
    # A plan built in memory carries ints where one read back from disk
    # carries floats. Hashing them differently made the cache miss on the one
    # path it exists for: build a plan, save it, render it later.
    built = RoughCutPlan(placements=[
        ClipPlacement(placement_id="p", asset_id="a",
                      source_file=str(footage["a"]),
                      source_in=1, source_out=9, sequence_start=0),
    ])
    loaded = RoughCutPlan.from_dict(json.loads(json.dumps(built.to_dict())))
    assert sources_module.plan_fingerprint(built) == \
        sources_module.plan_fingerprint(loaded)


def test_a_plan_fingerprint_ignores_what_the_renderer_cannot_see(footage):
    plan = a_plan(footage)
    before = sources_module.plan_fingerprint(plan)

    # Re-running the style pass rewrites hundreds of operations and changes
    # not one frame of the assembly.
    plan.ops = [{"op": "text.create"}] * 200
    plan.markers = [SequenceMarker(time=1.0, name="x")]
    plan.explanation = ["because"]
    plan.dry_run_passed = True
    assert sources_module.plan_fingerprint(plan) == before


def test_a_plan_fingerprint_notices_a_changed_cut(footage):
    before = sources_module.plan_fingerprint(a_plan(footage))
    changed = a_plan(footage)
    changed.placements[1].source_out += 0.5
    assert sources_module.plan_fingerprint(changed) != before

    reordered = a_plan(footage)
    reordered.placements[0].sequence_start = 99.0
    assert sources_module.plan_fingerprint(reordered) != before


def test_the_cache_key_changes_with_the_cut_the_sources_and_the_settings(
        footage, plan):
    segments = convert_module.to_segments(plan).segments
    inputs, _ = sources_module.describe_inputs(segments, probe=False)
    settings = RenderConfig().validated()

    def key(**kwargs):
        return sources_module.render_cache_key(
            segments=kwargs.get("segments", segments),
            inputs=kwargs.get("inputs", inputs),
            config=kwargs.get("config", settings),
            plan_hash=kwargs.get("plan_hash", "plan"),
            ffmpeg_version=kwargs.get("ffmpeg_version", "6.1"),
        )

    base = key()
    assert key() == base, "the same everything must hit"
    assert key(plan_hash="other") != base
    assert key(config=replace(settings, height=480).validated()) != base
    assert key(ffmpeg_version="7.0") != base, \
        "a new build can produce a different file from identical inputs"

    changed_source = [replace(inputs[0], content_hash="different"), inputs[1]]
    assert key(inputs=changed_source) != base


def test_a_re_exported_source_invalidates_the_render(footage, plan):
    segments = convert_module.to_segments(plan).segments
    settings = RenderConfig().validated()
    before, _ = sources_module.describe_inputs(segments, probe=False)
    first = sources_module.render_cache_key(
        segments=segments, inputs=before, config=settings)

    footage["a"].write_bytes(b"re-exported, same name" * 512)
    after, _ = sources_module.describe_inputs(segments, probe=False)
    second = sources_module.render_cache_key(
        segments=segments, inputs=after, config=settings)

    assert first != second, \
        "serving a render of footage that no longer exists is the worst " \
        "possible failure for this package"


# ---------------------------------------------------------------------------
# Rendering, end to end, with a fake runner
# ---------------------------------------------------------------------------

def test_a_render_produces_a_video_and_the_whole_folder(config, plan):
    runner = FakeRunner()
    job = render(config, plan, runner)

    assert job.status == "rendered"
    assert job.rendered and job.result.rendered
    assert job.result.ok

    folder = Path(job.output_dir)
    for name in ("config.json", "segments.json", "ffmpeg_commands.json",
                 "result.json", "job.json", "review_notes.md", "report.md",
                 "render.mp4"):
        assert (folder / name).exists(), name
    assert (folder / "logs").is_dir()


def test_a_render_runs_one_command_per_clip_and_one_join(config, plan):
    runner = FakeRunner()
    job = render(config, plan, runner)

    assert len(runner.commands) == 4
    assert job.result.commands_run == 4
    assert "concat" in " ".join(runner.commands[-1])


def test_a_render_never_writes_beside_the_footage(config, plan, footage):
    render(config, plan, FakeRunner())
    beside = {p.name for p in footage["a"].parent.iterdir()}
    assert beside == {"ep12_part1.mp4", "ep12_part2.mp4"}


def test_the_intermediates_are_cleared_after_a_successful_render(config, plan):
    job = render(config, plan, FakeRunner())
    assert not store_module.temp_dir(job.output_dir).exists()
    assert store_module.temp_size(job.output_dir) == 0


def test_keep_temp_keeps_them(config, plan):
    job = render(config, plan, FakeRunner(),
                 settings=RenderConfig(keep_temp=True))
    temp = store_module.temp_dir(job.output_dir)
    assert temp.exists()
    assert len(list(temp.glob("*.mp4"))) == 3


def test_the_intermediates_are_kept_after_a_failure(config, plan):
    job = render(config, plan, FakeRunner(fail_on=("concat",)))

    assert job.status == "failed"
    # The question after a failed join is always "which clip is wrong", and
    # answering it must not cost another render.
    assert len(list(store_module.temp_dir(job.output_dir).glob("*.mp4"))) == 3


def test_the_commands_file_records_what_actually_ran(config, plan):
    runner = FakeRunner()
    job = render(config, plan, runner)
    stored = json.loads(
        (Path(job.output_dir) / "ffmpeg_commands.json").read_text("utf-8"))

    assert stored["count"] == len(job.commands)
    assert stored["commands"][0] == runner.commands[0]


def test_the_segments_file_is_readable_on_its_own(config, plan):
    job = render(config, plan, FakeRunner())
    stored = json.loads(
        (Path(job.output_dir) / "segments.json").read_text("utf-8"))

    assert stored["count"] == 3
    assert stored["duration"] == 28.0
    assert stored["segments"][1]["speed"] == 2.0
    assert stored["segments"][1]["source_file"] == "ep12_part2.mp4"


def test_a_dry_run_builds_every_command_and_runs_none(config, plan):
    runner = FakeRunner()
    job = render(config, plan, runner, dry_run=True)

    assert job.status == "planned"
    assert runner.commands == []
    assert len(job.commands) == 4
    assert not job.result.rendered
    assert not (Path(job.output_dir) / "render.mp4").exists()
    # The notes are still written: they are what a person reads to decide
    # whether the cut is worth rendering at all.
    assert (Path(job.output_dir) / "review_notes.md").exists()


def test_a_render_verifies_the_finished_file_against_the_plan(config, plan):
    runner = FakeRunner(probe={"duration": 28.05, "width": 1280,
                               "height": 720, "fps": 30.0, "has_audio": True})
    job = render(config, plan, runner)

    assert job.result.planned_duration == 28.0
    assert job.result.measured_duration == 28.05
    assert abs(job.result.duration_drift) < 0.1
    assert job.result.width == 1280 and job.result.has_audio


def test_a_render_that_comes_out_the_wrong_length_says_so(config, plan):
    runner = FakeRunner(probe={"duration": 20.0, "width": 1280, "height": 720,
                               "fps": 30.0, "has_audio": True})
    job = render(config, plan, runner)

    assert job.status == "rendered", "a wrong length is a warning, not a stop"
    assert any("away from the" in w for w in job.warnings)


def test_a_render_that_lost_its_audio_says_so(config, plan):
    runner = FakeRunner(probe={"duration": 28.0, "width": 1280, "height": 720,
                               "fps": 30.0, "has_audio": False})
    job = render(config, plan, runner)
    assert any("no audio stream" in w for w in job.warnings)


def test_a_stream_copy_join_that_fails_falls_back_to_re_encoding(config, plan):
    runner = FakeRunner(fail_on=("-c copy",))
    job = render(config, plan, runner)

    assert job.status == "rendered"
    assert any("re-encoded to join" in w for w in job.warnings)
    assert "libx264" in " ".join(runner.commands[-1])


def test_a_clip_the_encoder_refuses_names_the_clip(config, plan, footage):
    runner = FakeRunner(fail_on=("ep12_part2",))
    job = render(config, plan, runner)

    assert job.status == "failed"
    assert job.failure.stage == "encode_segment"
    assert "ep12_part2.mp4" in job.failure.message
    assert "clip 2 of 3" in job.failure.message
    assert job.failure.path == str(footage["b"])
    assert job.failure.command, "the invocation that failed is kept"
    assert "ffmpeg_commands.json" in job.failure.hint


def test_a_join_that_cannot_be_saved_at_all_is_a_concat_failure(config, plan):
    job = render(config, plan, FakeRunner(fail_on=("concat",)))

    assert job.failure.stage == "concat"
    assert "would not join" in job.failure.message
    assert "temp/" in job.failure.hint


def test_a_failure_still_leaves_a_job_folder_to_debug(config, plan):
    job = render(config, plan, FakeRunner(fail_on=("concat",)))
    folder = Path(job.output_dir)

    assert (folder / "job.json").exists()
    assert (folder / "ffmpeg_commands.json").exists()
    assert (folder / "report.md").exists()
    assert "would not join" in (folder / "report.md").read_text("utf-8")


def test_a_missing_ffmpeg_gives_the_install_command(config, plan):
    job = render(config, plan, FakeRunner(available=False))

    assert job.status == "failed"
    assert job.failure.stage == "missing_ffmpeg"
    assert job.failure.recoverable
    assert job.failure.hint == INSTALL_HINT
    assert "winget install" in job.failure.hint


def test_a_missing_ffmpeg_does_not_try_to_probe_anything(config, plan):
    runner = FakeRunner(available=False)
    render(config, plan, runner)
    # Probing needs ffprobe; a missing binary must produce one sentence, not
    # a probe failure per source file.
    assert runner.commands == []


def test_a_missing_source_file_is_named_before_anything_encodes(
        config, plan, footage):
    footage["b"].unlink()
    runner = FakeRunner()
    job = render(config, plan, runner)

    assert job.failure.stage == "missing_source"
    assert job.failure.path.endswith("ep12_part2.mp4")
    assert "Reconnect the drive" in job.failure.hint
    assert runner.commands == [], "nothing was encoded"


def test_an_empty_plan_fails_unrecoverably_with_the_build_command(config):
    job = render(config, RoughCutPlan())

    assert job.failure.stage == "empty_plan"
    assert not job.failure.recoverable
    assert "roughcut build" in job.failure.hint


def test_too_many_clips_is_refused_before_the_work_starts(config, plan):
    job = render(config, plan, settings=RenderConfig(max_segments=2))

    assert job.failure.stage == "config"
    assert "the limit is 2" in job.failure.message
    assert "--max-segments" in job.failure.hint


def test_an_encoder_this_build_lacks_falls_back_to_libx264(config, plan):
    runner = FakeRunner(encoders=("libx264", "aac"))
    job = render(config, plan, runner,
                 settings=RenderConfig(video_encoder="h264_nvenc"))

    assert job.status == "rendered"
    assert job.config.resolved_encoder == "libx264"
    assert any("not in this FFmpeg build" in w for w in job.warnings)


def test_an_encoder_this_build_has_is_used(config, plan):
    runner = FakeRunner(encoders=("libx264", "h264_nvenc", "aac"))
    job = render(config, plan, runner,
                 settings=RenderConfig(video_encoder="h264_nvenc"))
    assert job.config.resolved_encoder == "h264_nvenc"
    assert "-cq" in " ".join(runner.commands[0])


def test_max_seconds_renders_only_the_opening(config, plan):
    runner = FakeRunner()
    job = render(config, plan, runner, settings=RenderConfig(max_seconds=15.0))

    assert len(job.segments) == 2
    assert job.duration == 15.0
    assert len(runner.commands) == 3


# ---------------------------------------------------------------------------
# The mock runner
# ---------------------------------------------------------------------------

def test_the_mock_runner_completes_and_claims_no_video(config, plan):
    job = render(config, plan, MockRunner(),
                 settings=RenderConfig(backend="mock"))

    assert job.status == "mocked"
    assert job.ok, "the pipeline ran to completion"
    assert not job.rendered, "and there is nothing to watch"
    assert not job.result.rendered
    assert job.result.mock


def test_a_mock_render_says_so_everywhere_it_can(config, plan):
    job = render(config, plan, MockRunner(),
                 settings=RenderConfig(backend="mock"))
    folder = Path(job.output_dir)

    assert any("MOCK RENDER" in w for w in job.result.warnings)
    assert "MOCK" in (folder / "review_notes.md").read_text("utf-8")
    assert "MOCK RENDER" in (folder / "report.md").read_text("utf-8")
    assert "MOCK RENDER" in report_module.render_text(job)
    assert json.loads(
        (folder / "result.json").read_text("utf-8"))["mock"] is True


def test_a_mock_render_is_never_reused_as_a_real_one(config, plan):
    settings = RenderConfig(backend="mock")
    first = render(config, plan, MockRunner(), settings=settings)
    second = render(config, plan, MockRunner(), settings=settings)

    assert second.status == "mocked", "not 'cached'"
    assert store_module.cached_job(
        config, first.job_id, first.cache_key) is None


def test_a_mock_and_a_real_render_of_the_same_cut_are_different_jobs(
        config, plan):
    mocked = render(config, plan, MockRunner(),
                    settings=RenderConfig(backend="mock"))
    real = render(config, plan, FakeRunner())
    assert mocked.job_id != real.job_id


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

def test_an_unchanged_cut_is_not_rendered_twice(config, plan):
    first = render(config, plan, FakeRunner())
    runner = FakeRunner()
    second = render(config, plan, runner)

    assert second.status == "cached"
    assert second.job_id == first.job_id
    assert second.result.from_cache
    assert runner.commands == [], "nothing was encoded"


def test_a_reused_render_still_points_at_a_video(config, plan):
    render(config, plan, FakeRunner())
    second = render(config, plan, FakeRunner())
    assert Path(second.output_path).exists()
    assert second.result.rendered


def test_a_reused_render_rewrites_notes_somebody_deleted(config, plan):
    first = render(config, plan, FakeRunner())
    Path(first.notes_path).unlink()

    second = render(config, plan, FakeRunner())
    assert second.status == "cached"
    assert Path(second.notes_path).exists()


def test_a_deleted_video_is_a_miss_not_a_path_to_nothing(config, plan):
    first = render(config, plan, FakeRunner())
    Path(first.output_path).unlink()

    runner = FakeRunner()
    second = render(config, plan, runner)
    assert second.status == "rendered"
    assert runner.commands, "it rendered again"


def test_a_changed_cut_renders_again_under_a_new_id(config, plan, footage):
    first = render(config, plan, FakeRunner())

    changed = a_plan(footage)
    changed.placements[2].source_out += 2.0
    second = render(config, changed, FakeRunner())

    assert second.job_id != first.job_id
    assert second.status == "rendered"
    assert Path(first.output_path).exists(), "the old render is not destroyed"


def test_changed_settings_render_again(config, plan):
    first = render(config, plan, FakeRunner())
    second = render(config, plan, FakeRunner(),
                    settings=RenderConfig(height=480))
    assert second.status == "rendered" and second.job_id != first.job_id


def test_a_changed_source_file_renders_again(config, plan, footage):
    first = render(config, plan, FakeRunner())
    footage["a"].write_bytes(b"a different export entirely" * 400)

    second = render(config, plan, FakeRunner())
    assert second.job_id != first.job_id
    assert second.status == "rendered"


def test_a_new_ffmpeg_build_renders_again(config, plan):
    first = render(config, plan, FakeRunner(version="6.1-fake"))
    second = render(config, plan, FakeRunner(version="7.0-fake"))
    assert second.job_id != first.job_id


def test_force_re_renders_into_the_same_folder(config, plan):
    first = render(config, plan, FakeRunner())
    runner = FakeRunner()
    second = render(config, plan, runner, force=True)

    assert second.job_id == first.job_id
    assert second.status == "rendered"
    assert runner.commands


def test_turning_the_cache_off_re_renders(config, plan):
    render(config, plan, FakeRunner())
    runner = FakeRunner()
    second = render(config, plan, runner, settings=RenderConfig(use_cache=False))
    assert second.status == "rendered"
    assert runner.commands


def test_a_dry_run_never_serves_a_cached_render(config, plan):
    render(config, plan, FakeRunner())
    job = render(config, plan, FakeRunner(), dry_run=True)
    assert job.status == "planned"


def test_a_settings_change_that_changes_nothing_still_hits(config, plan):
    render(config, plan, FakeRunner())
    second = render(config, plan, FakeRunner(),
                    settings=RenderConfig(keep_temp=True,
                                          notes_interval=30.0,
                                          segment_timeout=99.0))
    assert second.status == "cached"


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def test_renders_are_listed_newest_first(config, plan, footage):
    first = render(config, plan, FakeRunner())
    other = a_plan(footage)
    other.placements[0].source_out += 1.0
    second = render(config, other, FakeRunner())

    listed = [job.job_id for job in store_module.list_jobs(config)]
    assert set(listed) == {first.job_id, second.job_id}
    assert store_module.latest_job(config) is not None


def test_resolving_a_render_with_no_id_takes_the_most_recent(config, plan):
    job = render(config, plan, FakeRunner())
    assert store_module.resolve_job(config).job_id == job.job_id


def test_resolving_a_render_when_there_are_none_says_how_to_make_one(config):
    with pytest.raises(EditingError) as caught:
        store_module.resolve_job(config)
    assert "render roughcut" in caught.value.hint


def test_an_unknown_job_id_is_an_error_with_the_list_command(config):
    with pytest.raises(EditingError) as caught:
        store_module.load_job(config, "nope")
    assert "render list" in caught.value.hint


def test_an_unreadable_job_record_is_skipped_rather_than_fatal(config, plan):
    job = render(config, plan, FakeRunner())
    (Path(job.output_dir) / "job.json").write_text("{ broken", encoding="utf-8")

    assert store_module.list_jobs(config) == []
    assert store_module.cached_job(config, job.job_id, job.cache_key) is None


def test_cleaning_the_intermediates_keeps_the_video(config, plan):
    job = render(config, plan, FakeRunner(),
                 settings=RenderConfig(keep_temp=True))
    before = store_module.temp_size(job.output_dir)
    assert before > 0

    result = store_module.clean(config, temp_only=True)
    assert result["freed_bytes"] >= before
    assert Path(job.output_path).exists()


def test_cleaning_a_render_removes_the_whole_folder(config, plan):
    job = render(config, plan, FakeRunner())
    store_module.clean(config, job_id=job.job_id)
    assert not Path(job.output_dir).exists()


def test_cleaning_can_keep_the_most_recent(config, plan, footage):
    older = render(config, plan, FakeRunner())
    other = a_plan(footage)
    other.placements[0].source_out += 1.0
    newer = render(config, other, FakeRunner())

    store_module.clean(config, keep_latest=1)
    remaining = {job.job_id for job in store_module.list_jobs(config)}
    assert newer.job_id in remaining
    assert older.job_id not in remaining


def test_cleaning_an_unknown_job_is_an_error(config):
    with pytest.raises(EditingError):
        store_module.clean(config, job_id="nope")


def test_usage_reports_what_the_renders_are_costing(config, plan):
    render(config, plan, FakeRunner(), settings=RenderConfig(keep_temp=True))
    usage = store_module.usage(config)
    assert usage["jobs"] == 1
    assert usage["total_bytes"] > 0
    assert usage["temp_bytes"] > 0


def test_artifacts_are_read_off_the_disk_not_remembered(config, plan):
    job = render(config, plan, FakeRunner())
    kinds = {item.kind for item in job.result.artifacts}
    assert {"video", "notes", "report", "commands", "log"} <= kinds

    Path(job.output_path).unlink()
    assert not any(item.kind == "video"
                   for item in store_module.collect_artifacts(job))


def test_a_job_reads_back_with_its_segments_and_commands(config, plan):
    job = render(config, plan, FakeRunner())
    loaded = store_module.load_job(config, job.job_id)

    assert len(loaded.segments) == 3
    assert loaded.duration == 28.0
    assert len(loaded.commands) == 4
    assert loaded.result.rendered


def test_the_result_file_can_be_loaded_on_its_own(config, plan):
    job = render(config, plan, FakeRunner())
    assert store_module.load_result(config, job.job_id).rendered

    (Path(job.output_dir) / "result.json").unlink()
    with pytest.raises(EditingError):
        store_module.load_result(config, job.job_id)


# ---------------------------------------------------------------------------
# Review notes
# ---------------------------------------------------------------------------

def test_review_notes_have_a_section_per_clip_with_matching_timecodes(
        config, plan):
    job = render(config, plan, FakeRunner())
    text = Path(job.notes_path).read_text("utf-8")

    assert text.startswith("# Review Notes")
    assert "## 00:00-00:10" in text
    assert "## 00:10-00:20" in text
    assert "## 00:20-00:28" in text
    assert text.count("- Notes:") == 3


def test_review_notes_say_where_each_clip_came_from_and_why(config, plan):
    text = Path(render(config, plan, FakeRunner()).notes_path).read_text(
        "utf-8")

    assert "`ep12_part1.mp4` 10.0-20.0s" in text
    assert "Kept because: setup" in text
    assert "@ 2x" in text
    assert "protected -- a hold said leave this alone" in text
    assert "From: rec_1" in text


def test_review_notes_carry_the_shorthand_and_the_overall_questions(
        config, plan):
    text = Path(render(config, plan, FakeRunner()).notes_path).read_text(
        "utf-8")

    for label, _meaning in notes_module.SHORTCUTS:
        assert f"`{label}`" in text
    assert "## Overall" in text
    assert "Where did you first get bored?" in text
    assert "feedback rate" in text, "an opinion nothing reads is decoration"


def test_the_notes_tell_you_how_to_render_this_same_cut_again(
        config, plan, tmp_path):
    """A command that renders a different cut is worse than no command."""
    named = render(config, plan, FakeRunner())
    assert "render roughcut --name structure" in Path(
        named.notes_path).read_text("utf-8")

    target = tmp_path / "exported.json"
    target.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    from_file = run_module.render_from_file(
        config, target, settings=RenderConfig().validated(),
        runner=FakeRunner())
    text = Path(from_file.notes_path).read_text("utf-8")
    assert "render from-plan" in text
    assert "roughcut --name exported" not in text


def test_review_notes_can_be_written_at_a_fixed_interval(config, plan):
    job = render(config, plan, FakeRunner(),
                 settings=RenderConfig(notes_interval=10.0))
    text = Path(job.notes_path).read_text("utf-8")

    assert "## 00:00-00:10" in text
    assert "## 00:20-00:28" in text
    assert "ep12_part1.mp4" in text


def test_a_muted_clip_is_marked_in_the_notes(config, footage):
    plan = a_plan(footage)
    job = run_module.render_plan(
        config, plan, settings=RenderConfig().validated(),
        runner=FakeRunner(), muted_placements=["p_1"])
    assert "- Audio: muted" in Path(job.notes_path).read_text("utf-8")


def test_timecodes_grow_an_hour_field_only_when_they_need_one():
    assert notes_module.timecode(0) == "00:00"
    assert notes_module.timecode(75.4) == "01:15"
    assert notes_module.timecode(3661) == "1:01:01"
    assert notes_module.timecode(-5) == "00:00"


def test_notes_for_a_very_long_cut_stop_and_say_they_stopped(config, footage):
    plan = RoughCutPlan(placements=[
        placement(footage["a"], float(i * 2), float(i), float(i) + 2.0,
                  index=i, placement_id=f"p_{i}")
        for i in range(12)
    ])
    job = RenderJob(plan_name="structure",
                    segments=convert_module.to_segments(plan).segments)
    text = notes_module.render_notes(job, max_sections=5)

    assert text.count("- Notes:") == 5
    assert "7 more section(s) not written" in text


def test_notes_can_be_regenerated_blank_from_a_finished_render(config, plan):
    job = render(config, plan, FakeRunner())
    Path(job.notes_path).write_text("my scribbles", encoding="utf-8")

    store_module.write_text(
        store_module.notes_path(job.output_dir),
        notes_module.render_notes(store_module.load_job(config, job.job_id)))
    assert "my scribbles" not in Path(job.notes_path).read_text("utf-8")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_the_report_says_what_could_not_be_shown(config, footage):
    plan = a_plan(footage, ops=[{"op": "text.create"}, {"op": "audio.duck"}],
                  markers=[(2.0, "danger")])
    job = render(config, plan, FakeRunner())
    text = report_module.render_text(job)

    assert "NOT IN THIS VIDEO" in text
    assert "text and captions" in text
    assert "ducking under speech" in text
    assert "sequence marker(s)" in text


def test_the_report_mentions_drift_only_when_it_means_something(config, plan):
    """Frame-rate conversion costs hundredths on every render.

    Printing that every time would train the reader to skip the line that
    matters.
    """
    tiny = FakeRunner(probe={"duration": 28.04, "width": 1280, "height": 720,
                             "fps": 30.0, "has_audio": True})
    assert "away from what the cut" not in report_module.render_markdown(
        render(config, plan, tiny))

    plan.placements[0].source_out += 0.001   # a different cut, a new job
    big = FakeRunner(probe={"duration": 22.0, "width": 1280, "height": 720,
                            "fps": 30.0, "has_audio": True})
    assert "away from what the cut" in report_module.render_markdown(
        render(config, plan, big))


def test_the_report_states_the_limitations_every_time(config, plan):
    report = report_module.build_report(render(config, plan, FakeRunner()))
    joined = " ".join(report.limitations)

    assert "proxy, not a delivery render" in joined
    assert "Only the V1 assembly" in joined
    assert "hard cut" in joined
    assert "has touched Premiere" in joined


def test_the_report_offers_the_commands_to_watch_and_redo(config, plan):
    job = render(config, plan, FakeRunner())
    commands = report_module.build_report(job).next_commands

    assert any(f"render open {job.job_id}" == c.split("cli ")[-1]
               for c in commands)
    assert any("--force" in c for c in commands)
    assert any("feedback start" in c for c in commands)


def test_a_render_from_a_file_is_told_to_redo_itself_from_that_file(
        config, plan, tmp_path):
    target = tmp_path / "exported_plan.json"
    target.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    job = run_module.render_from_file(
        config, target, settings=RenderConfig().validated(),
        runner=FakeRunner())

    commands = report_module.build_report(job).next_commands
    # ``render roughcut --name exported_plan`` would silently render a
    # different cut, or nothing at all.
    assert any("from-plan" in c for c in commands)
    assert not any("roughcut --name" in c for c in commands)


def test_a_failed_report_leads_with_what_went_wrong(config, plan):
    job = render(config, plan, FakeRunner(available=False))
    markdown = report_module.render_markdown(job)

    assert "## What went wrong" in markdown
    assert "FFmpeg is not installed" in markdown
    assert "winget install" in markdown


def test_the_report_lists_the_sources_and_flags_the_missing_ones(
        config, plan, footage):
    job = render(config, plan, FakeRunner())
    markdown = report_module.render_markdown(job)
    assert "ep12_part1.mp4` -- 2 clip(s)" in markdown

    footage["b"].unlink()
    failed = render(config, plan, FakeRunner(), force=True)
    assert "MISSING SOURCES" in report_module.render_text(failed)


def test_the_job_list_is_one_line_each_and_says_what_to_do_when_empty(
        config, plan):
    assert "render roughcut" in report_module.render_job_list([])

    job = render(config, plan, FakeRunner())
    listing = report_module.render_job_list(
        store_module.list_jobs(config))
    assert job.job_id in listing
    assert "render open <job_id>" in listing


def test_a_report_can_be_rebuilt_from_disk_without_re_rendering(config, plan):
    job = render(config, plan, FakeRunner())
    loaded = store_module.load_job(config, job.job_id)
    report = report_module.build_report(loaded)

    assert report.rendered
    assert report.stats["segments"] == 3
    assert report.to_dict()["job_id"] == job.job_id


# ---------------------------------------------------------------------------
# Rendering from a file
# ---------------------------------------------------------------------------

def test_a_plan_file_renders_and_keeps_its_own_name(config, plan, tmp_path):
    target = tmp_path / "ep12_cut.json"
    target.write_text(json.dumps(plan.to_dict()), encoding="utf-8")

    job = run_module.render_from_file(
        config, target, settings=RenderConfig().validated(),
        runner=FakeRunner())

    assert job.status == "rendered"
    assert job.plan_name == "ep12_cut"
    assert job.job_id.startswith("ep12_cut-")
    assert job.plan_path == str(target)


def test_a_plan_file_that_is_not_there_says_how_to_make_one(config, tmp_path):
    with pytest.raises(EditingError) as caught:
        run_module.render_from_file(config, tmp_path / "nope.json")
    assert "roughcut build" in caught.value.hint


def test_a_plan_file_that_is_not_json_is_explained(config, tmp_path):
    target = tmp_path / "bad.json"
    target.write_text("this is not json", encoding="utf-8")
    with pytest.raises(EditingError) as caught:
        run_module.render_from_file(config, target)
    assert "roughcut build" in caught.value.hint


def test_a_json_file_that_is_not_a_plan_is_explained(config, tmp_path):
    target = tmp_path / "list.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(EditingError) as caught:
        run_module.render_from_file(config, target)
    assert "placements" in caught.value.hint


# ---------------------------------------------------------------------------
# The real runner, without running it
# ---------------------------------------------------------------------------

def test_the_real_runner_turns_a_missing_binary_into_a_typed_error():
    runner = FFmpegRunner(ffmpeg="definitely-not-a-real-binary-xyz")
    assert not runner.available()
    with pytest.raises(ToolMissingError) as caught:
        runner.run([runner.ffmpeg, "-version"])
    assert "winget install" in caught.value.hint


def test_the_real_runners_health_carries_the_install_hint():
    health = FFmpegRunner(ffmpeg="not-a-real-binary-xyz",
                          ffprobe="not-real-either-xyz").health()
    assert health["ready"] is False
    assert health["hint"] == INSTALL_HINT


def test_a_version_that_cannot_be_read_is_empty_not_an_exception():
    assert FFmpegRunner(ffmpeg="not-a-real-binary-xyz").version() == ""
    assert FFmpegRunner(ffmpeg="not-a-real-binary-xyz").encoders() == set()


def test_the_encoder_list_is_parsed_from_ffmpegs_own_table(monkeypatch):
    runner = FFmpegRunner()
    sample = (
        "Encoders:\n"
        " V..... = Video\n"
        " ------\n"
        " V....D libx264              H.264 / AVC\n"
        " V....D h264_nvenc           NVIDIA NVENC H.264\n"
        " A....D aac                  AAC (Advanced Audio Coding)\n"
    )
    monkeypatch.setattr(
        runner, "run",
        lambda *a, **k: CommandResult(command=list(a[0]), stdout=sample))
    assert {"libx264", "h264_nvenc", "aac"} <= runner.encoders()


def test_the_version_is_parsed_from_the_banner(monkeypatch):
    runner = FFmpegRunner()
    monkeypatch.setattr(
        runner, "run",
        lambda *a, **k: CommandResult(
            command=list(a[0]), stdout="ffmpeg version 6.1.1 Copyright (c)"))
    assert runner.version() == "6.1.1"


def test_the_mock_runner_writes_a_labelled_placeholder(tmp_path):
    runner = MockRunner()
    target = tmp_path / "out.mp4"
    result = runner.run(["ffmpeg", "-i", "in.mp4", str(target)])

    assert result.ok
    assert b"MOCK RENDER" in target.read_bytes()
    assert runner.commands


def test_the_mock_runner_measures_nothing(tmp_path):
    probe = MockRunner().probe(tmp_path / "out.mp4")
    # Reporting the duration the plan expected would make ``duration_drift``
    # look verified when nothing has been verified at all.
    assert probe["duration"] == 0.0
    assert probe["has_audio"] is False


def test_the_runner_log_records_every_invocation(tmp_path):
    log = tmp_path / "logs" / "ffmpeg.log"
    runner = MockRunner()
    runner.run(["ffmpeg", "-i", "a.mp4", str(tmp_path / "b.mp4")],
               log_path=log)
    text = log.read_text("utf-8")
    assert "$ ffmpeg" in text and "exit 0" in text


def test_building_a_runner_reads_the_backend_from_the_config(config):
    from editing.render.runner import build_runner
    assert build_runner(config, backend="mock").name == "mock"
    assert build_runner(config, backend="ffmpeg").name == "ffmpeg"
    # Anything unrecognised gets the real one: failing loudly on a missing
    # FFmpeg beats silently producing placeholders.
    assert build_runner(config, backend="banana").name == "ffmpeg"


def test_nothing_but_the_runner_shells_out():
    """Subprocess lives in one module, so the rest is testable without it."""
    import ast

    package = Path(__file__).resolve().parents[2] / "editing" / "render"
    offenders = []
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "subprocess" for name in names):
                offenders.append(path.name)
    assert set(offenders) <= {"runner.py"}, offenders


# ---------------------------------------------------------------------------
# Through the pipeline
# ---------------------------------------------------------------------------

def _pipeline(config, sampling):
    from editing.pipeline import build_pipeline
    return build_pipeline(config, sampling)


def test_the_pipeline_renders_the_rough_cut_it_has_on_disk(
        config, sampling, plan):
    pipeline = _pipeline(config, sampling)
    pipeline.write_rough_cut(plan)

    job = pipeline.render_roughcut(runner=FakeRunner())
    assert job.status == "rendered"
    assert job.plan_name == "structure"
    assert job.sequence_name == "Nova Rough Cut"


def test_the_pipeline_reuses_a_render_across_calls(config, sampling, plan):
    pipeline = _pipeline(config, sampling)
    pipeline.write_rough_cut(plan)
    pipeline.render_roughcut(runner=FakeRunner())

    runner = FakeRunner()
    second = pipeline.render_roughcut(runner=runner)
    assert second.status == "cached"
    assert runner.commands == []


def test_the_pipeline_lists_shows_and_reports_on_renders(
        config, sampling, plan):
    pipeline = _pipeline(config, sampling)
    pipeline.write_rough_cut(plan)
    job = pipeline.render_roughcut(runner=FakeRunner())

    assert [j.job_id for j in pipeline.render_jobs()] == [job.job_id]
    assert pipeline.render_job().job_id == job.job_id
    assert pipeline.render_result(job.job_id).rendered

    _job, report = pipeline.render_report(job.job_id)
    assert report.rendered
    assert Path(pipeline.render_notes(job.job_id)).exists()


def test_the_pipeline_reports_whether_a_render_could_run(config, sampling):
    status = _pipeline(config, sampling).render_status()
    assert "ready" in status and "root" in status
    assert status["jobs"] == 0


def test_the_pipeline_cleans_renders(config, sampling, plan):
    pipeline = _pipeline(config, sampling)
    pipeline.write_rough_cut(plan)
    job = pipeline.render_roughcut(runner=FakeRunner())

    result = pipeline.clean_renders(job_id=job.job_id)
    assert result["removed"] == [job.job_id]
    assert not Path(job.output_dir).exists()


def test_renders_live_under_their_own_directory(config):
    assert config.render_dir.name == "render"
    assert config.render_dir.parent == config.output_dir
    assert store_module.jobs_root(config) == config.render_dir / "jobs"


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def test_the_render_commands_parse():
    from editing.cli import build_parser

    parser = build_parser()
    for argv, expected in (
        (["render", "roughcut"], "roughcut"),
        (["render", "roughcut", "--quality", "proxy", "--height", "720"],
         "roughcut"),
        (["render", "from-plan", "plan.json"], "from-plan"),
        (["render", "show", "job-1"], "show"),
        (["render", "list"], "list"),
        (["render", "report"], "report"),
        (["render", "notes"], "notes"),
        (["render", "open", "job-1", "--notes"], "open"),
        (["render", "clean", "--temp-only", "--yes"], "clean"),
        (["render", "status"], "status"),
    ):
        args = parser.parse_args(argv)
        assert args.render_command == expected
        assert args.func.__name__ == "cmd_render"


def test_render_options_reach_the_parsed_arguments():
    from editing.cli import build_parser

    args = build_parser().parse_args([
        "render", "roughcut", "--quality", "high", "--height", "1080",
        "--fps", "60", "--encoder", "libx265", "--crf", "20",
        "--scale-mode", "crop", "--no-audio", "--max-seconds", "90",
        "--keep-temp", "--force", "--dry-run", "--mock",
    ])
    assert (args.quality, args.height, args.fps) == ("high", 1080, 60.0)
    assert args.encoder == "libx265" and args.crf == 20
    assert args.scale_mode == "crop" and args.no_audio
    assert args.max_seconds == 90.0
    assert args.keep_temp and args.force and args.dry_run and args.mock


def test_every_render_command_can_be_scoped_to_an_auto_run():
    """A run's proxy lives inside that run, so finding it needs --run too."""
    from editing.cli import build_parser

    parser = build_parser()
    for command in ("roughcut", "show", "list", "open", "clean", "report",
                    "notes", "status"):
        argv = ["render", command, "--run", "20260101T000000-abc-style"]
        if command == "clean":
            argv.append("--yes")
        assert parser.parse_args(argv).run == "20260101T000000-abc-style"


def test_an_unknown_render_subcommand_is_a_usage_error():
    from editing.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["render", "polish"])


def test_the_job_id_is_optional_wherever_it_can_be():
    from editing.cli import build_parser

    parser = build_parser()
    for command in ("show", "report", "notes", "open"):
        assert parser.parse_args(["render", command]).job_id == ""


def test_render_from_the_command_line_writes_a_video(config, plan,
                                                     monkeypatch):
    from editing import cli

    pipeline_config = config
    monkeypatch.setattr(
        cli, "_run_scoped_pipeline",
        lambda args: _pipeline(pipeline_config, __import__(
            "editing.config", fromlist=["SamplingConfig"]).SamplingConfig()))
    monkeypatch.setattr(
        "editing.pipeline.Pipeline.render_roughcut",
        lambda self, **kwargs: run_module.render_plan(
            self.config, plan, settings=kwargs.get("settings"),
            runner=FakeRunner()))

    assert cli.main(["render", "roughcut", "--json"]) == 0


def test_opening_a_mock_render_is_refused(config, plan, monkeypatch):
    """The one thing this package promises never to do."""
    from editing import cli
    from editing.config import SamplingConfig

    pipeline = _pipeline(config, SamplingConfig())
    pipeline.write_rough_cut(plan)
    job = pipeline.render_roughcut(runner=MockRunner(),
                                   settings=RenderConfig(backend="mock"))
    assert Path(job.output_path).exists(), "the placeholder is there"

    monkeypatch.setattr(cli, "_run_scoped_pipeline",
                        lambda args: _pipeline(config, SamplingConfig()))
    assert cli.main(["render", "open", job.job_id]) == 1
    # The notes exist either way, and opening those is fine.
    assert cli.main(["render", "open", job.job_id, "--notes",
                     "--json"]) == 0


def test_cleaning_from_the_command_line_needs_yes(config, monkeypatch):
    from editing import cli
    from editing.config import SamplingConfig

    monkeypatch.setattr(cli, "_run_scoped_pipeline",
                        lambda args: _pipeline(config, SamplingConfig()))
    assert cli.main(["render", "clean"]) == 1, "refused without --yes"


def test_a_failed_render_exits_non_zero(config, plan, monkeypatch):
    from editing import cli
    from editing.config import SamplingConfig

    monkeypatch.setattr(cli, "_run_scoped_pipeline",
                        lambda args: _pipeline(config, SamplingConfig()))
    monkeypatch.setattr(
        "editing.pipeline.Pipeline.render_roughcut",
        lambda self, **kwargs: run_module.render_plan(
            self.config, plan, settings=kwargs.get("settings"),
            runner=FakeRunner(available=False)))

    # A script has to be able to detect "there is no video" without parsing
    # the report.
    assert cli.main(["render", "roughcut"]) == 1
