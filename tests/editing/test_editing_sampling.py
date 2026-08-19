"""Sampling and chunking.

Sampling is where a structure layer silently goes wrong: under-sample and a
death falls between two windows and never reaches the timeline; over-sample and
a run costs a night. These tests pin the properties that matter -- complete
coverage, bounded cost, densification where the motion is, and stable window
boundaries (which end up in cache keys).
"""
from __future__ import annotations

import pytest

from editing.config import SamplingConfig
from editing.ffmpeg import parse_motion_output
from editing.visual.sampling import (
    MotionPoint, coverage_gaps, estimate_calls, frame_times_for,
    motion_score_for, plan_summary, plan_windows,
)


# ---------------------------------------------------------------------------
# Frame placement
# ---------------------------------------------------------------------------

def test_frame_times_sit_at_sub_slice_centres():
    """Never on the boundary, where a cut or a black frame tends to be."""
    assert frame_times_for(0.0, 8.0, 3) == (1.333, 4.0, 6.667)


def test_frame_times_single_frame_is_centred():
    assert frame_times_for(10.0, 20.0, 1) == (15.0,)


def test_frame_times_never_reach_the_window_edges():
    times = frame_times_for(4.0, 12.0, 5)
    assert all(4.0 < time < 12.0 for time in times)


def test_frame_times_of_a_zero_length_window():
    assert frame_times_for(5.0, 5.0, 3) == (5.0,)


# ---------------------------------------------------------------------------
# Motion scoring
# ---------------------------------------------------------------------------

def test_motion_score_takes_the_peak_not_the_mean():
    """One hard change in a quiet window is the moment worth looking at."""
    motion = [MotionPoint(1.0, 0.01), MotionPoint(2.0, 0.9), MotionPoint(3.0, 0.02)]
    assert motion_score_for(motion, 0.0, 4.0) == 0.9


def test_motion_score_is_zero_without_data():
    assert motion_score_for([], 0.0, 8.0) == 0.0


def test_motion_score_only_counts_readings_inside_the_window():
    motion = [MotionPoint(1.0, 0.9), MotionPoint(50.0, 0.95)]
    assert motion_score_for(motion, 40.0, 45.0) == 0.0


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_plan_is_empty_for_a_zero_duration_file():
    assert plan_windows(0.0, SamplingConfig()) == []
    assert plan_windows(-5.0, SamplingConfig()) == []


def test_plan_covers_the_whole_file(sampling):
    windows = plan_windows(30.0, sampling)
    assert coverage_gaps(windows, 30.0) == []
    assert windows[0].start == 0.0
    assert windows[-1].end == pytest.approx(30.0)


def test_plan_windows_are_contiguous_and_ordered(sampling):
    windows = plan_windows(30.0, sampling)
    assert [window.index for window in windows] == list(range(len(windows)))
    for earlier, later in zip(windows, windows[1:]):
        assert later.start >= earlier.start
        assert later.start <= earlier.end   # no gap


def test_overlap_makes_consecutive_windows_share_time():
    config = SamplingConfig(window_seconds=8.0, window_overlap=2.0).validated()
    windows = plan_windows(30.0, config)
    assert windows[1].start < windows[0].end


def test_overlap_is_clamped_below_the_window_length():
    """An overlap at or above the window would stop the sampler advancing."""
    config = SamplingConfig(window_seconds=4.0, window_overlap=99.0).validated()
    assert config.window_overlap <= config.window_seconds * 0.5
    windows = plan_windows(20.0, config)
    assert len(windows) > 1
    assert coverage_gaps(windows, 20.0) == []


def test_frames_per_window_is_honoured(sampling):
    windows = plan_windows(30.0, sampling)
    normal = [window for window in windows if not window.dense]
    assert all(
        len(window.frame_times) == sampling.frames_per_window for window in normal
    )


# ---------------------------------------------------------------------------
# Densification
# ---------------------------------------------------------------------------

def test_high_motion_stretches_are_sampled_densely(sampling):
    quiet = plan_windows(24.0, sampling)
    motion = [
        MotionPoint(time, 0.9 if 8.0 <= time <= 16.0 else 0.01)
        for time in [index * 1.0 for index in range(25)]
    ]
    busy = plan_windows(24.0, sampling, motion=motion)

    assert len(busy) > len(quiet)
    dense = [window for window in busy if window.dense]
    assert dense

    # Densification works on the base window that saw the motion, and every
    # sub-window of a split inherits it -- so the dense region is the union of
    # the base windows containing a high reading (8-12, 12-16 and 16-20 here),
    # not the raw [8, 16] the readings fall in.
    assert all(8.0 <= window.start and window.end <= 20.0 for window in dense)
    # The genuinely quiet stretches are untouched.
    assert not any(window.dense for window in busy if window.end <= 8.0)
    assert not any(window.dense for window in busy if window.start >= 20.0)


def test_dense_windows_get_more_frames(sampling):
    motion = [MotionPoint(time, 0.95) for time in [0.0, 2.0, 4.0, 6.0, 8.0]]
    windows = plan_windows(12.0, sampling, motion=motion)
    dense = [window for window in windows if window.dense]
    assert dense
    assert all(
        len(window.frame_times) == sampling.dense_frames_per_window
        for window in dense
    )


def test_dense_windows_are_shorter(sampling):
    motion = [MotionPoint(time, 0.95) for time in [0.0, 2.0, 4.0, 6.0, 8.0]]
    windows = plan_windows(12.0, sampling, motion=motion)
    dense = [window for window in windows if window.dense]
    assert all(
        window.duration <= sampling.dense_window_seconds + 0.01 for window in dense
    )


def test_densification_still_covers_everything(sampling):
    motion = [
        MotionPoint(time, 0.9 if 8.0 <= time <= 16.0 else 0.01)
        for time in [index * 1.0 for index in range(31)]
    ]
    windows = plan_windows(30.0, sampling, motion=motion)
    assert coverage_gaps(windows, 30.0) == []


def test_motion_below_the_threshold_changes_nothing(sampling):
    calm = [MotionPoint(time, 0.05) for time in [0.0, 2.0, 4.0, 6.0, 8.0]]
    assert len(plan_windows(24.0, sampling, motion=calm)) == len(
        plan_windows(24.0, sampling)
    )


def test_a_short_high_motion_window_gets_frames_not_a_split():
    """Too short to split usefully, so it is densified in place instead."""
    config = SamplingConfig(
        window_seconds=3.0, window_overlap=0.0, frames_per_window=2,
        dense_frames_per_window=5, dense_window_seconds=3.0,
        motion_threshold=0.3, min_window_seconds=0.5,
    ).validated()
    windows = plan_windows(6.0, config, motion=[MotionPoint(1.0, 0.9)])
    first = windows[0]
    assert first.dense is True
    assert len(first.frame_times) == 5
    assert first.duration == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

def test_max_windows_is_never_exceeded():
    config = SamplingConfig(
        window_seconds=8.0, window_overlap=0.0, max_windows=50
    ).validated()
    windows = plan_windows(7200.0, config)   # two hours
    assert len(windows) <= 50


def test_a_long_file_is_covered_completely_not_truncated():
    """Coarsening is correct; stopping at minute 12 of 120 is not."""
    config = SamplingConfig(
        window_seconds=8.0, window_overlap=0.0, max_windows=50
    ).validated()
    windows = plan_windows(7200.0, config)
    assert coverage_gaps(windows, 7200.0) == []
    assert windows[-1].end == pytest.approx(7200.0)


def test_a_trailing_sliver_is_merged_backwards():
    config = SamplingConfig(
        window_seconds=4.0, window_overlap=0.0, min_window_seconds=1.5
    ).validated()
    windows = plan_windows(8.2, config)
    assert all(window.duration >= 1.5 for window in windows)
    assert windows[-1].end == pytest.approx(8.2)


def test_a_file_shorter_than_one_window_gets_one_window(sampling):
    windows = plan_windows(1.0, sampling)
    assert len(windows) == 1
    assert windows[0].start == 0.0 and windows[0].end == pytest.approx(1.0)


def test_estimate_calls_is_bounded_by_max_windows():
    config = SamplingConfig(max_windows=100).validated()
    assert estimate_calls(100000.0, config) == 100


# ---------------------------------------------------------------------------
# Determinism and cache keys
# ---------------------------------------------------------------------------

def test_planning_is_deterministic(sampling):
    motion = [MotionPoint(time, 0.5) for time in [0.0, 3.0, 6.0]]
    first = plan_windows(37.0, sampling, motion=motion)
    second = plan_windows(37.0, sampling, motion=motion)
    assert [w.to_dict() for w in first] == [w.to_dict() for w in second]


def test_window_cache_key_ignores_index_but_not_frames(sampling):
    """Renumbering after a dense split must not invalidate an unchanged window."""
    windows = plan_windows(30.0, sampling)
    first = windows[0]
    same_times = type(first)(
        index=99, start=first.start, end=first.end,
        frame_times=first.frame_times, dense=first.dense,
    )
    assert same_times.cache_key_part() == first.cache_key_part()

    more_frames = type(first)(
        index=first.index, start=first.start, end=first.end,
        frame_times=first.frame_times + (first.end - 0.1,), dense=first.dense,
    )
    assert more_frames.cache_key_part() != first.cache_key_part()


def test_plan_summary_reports_real_numbers(sampling):
    windows = plan_windows(30.0, sampling)
    summary = plan_summary(windows, 30.0)
    assert summary["windows"] == len(windows)
    assert summary["frames"] == sum(len(w.frame_times) for w in windows)
    assert summary["coverage_ratio"] >= 1.0


def test_coverage_gaps_detects_a_hole():
    from editing.visual.sampling import SampleWindow

    windows = [
        SampleWindow(0, 0.0, 4.0, (2.0,)),
        SampleWindow(1, 10.0, 14.0, (12.0,)),
    ]
    assert coverage_gaps(windows, 14.0) == [(4.0, 10.0)]


# ---------------------------------------------------------------------------
# Motion parsing (the ffmpeg text format, without ffmpeg)
# ---------------------------------------------------------------------------

def test_parse_motion_output_pairs_scores_with_timestamps():
    text = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.scene_score=0.000000\n"
        "frame:1    pts:2048    pts_time:2.048\n"
        "lavfi.scene_score=0.412500\n"
        "frame:2    pts:4096    pts_time:4.096\n"
        "lavfi.scene_score=0.031250\n"
    )
    samples = parse_motion_output(text)
    assert [(round(s.time, 3), s.score) for s in samples] == [
        (0.0, 0.0), (2.048, 0.4125), (4.096, 0.03125),
    ]


def test_parse_motion_output_ignores_unrelated_lines():
    assert parse_motion_output("ffmpeg version 6.0\nInput #0, mov,mp4\n") == []


def test_parse_motion_output_clamps_scores_into_range():
    text = "frame:0 pts_time:1.0\nlavfi.scene_score=5.0\n"
    assert parse_motion_output(text)[0].score == 1.0
