"""Deciding what the vision model gets to look at.

A 40-minute Minecraft recording at 60fps is about 144,000 frames. The model can
reasonably see a few hundred. This module turns a duration (plus an optional
motion signal) into the exact windows and frame timestamps to extract -- and it
is deliberately **pure**: no files, no ffmpeg, no model. That is what makes the
sampling policy testable, which matters because sampling is where a structure
layer silently goes wrong. Over-sample and a run takes all night; under-sample
and a death or a diamond vein falls between two windows and never appears in
the timeline at all.

The policy, in order:

1. **Uniform windows** across the whole file, with a small overlap so an event
   straddling a boundary is seen whole by at least one window.
2. **Densification.** Windows whose motion score clears the threshold are
   re-cut into shorter windows and given more frames. Fights, falls, mob
   attacks and scene changes are where the edit-worthy material is; corridors
   and tunnels are not.
3. **A trailing-sliver merge**, so a file that is not a whole number of windows
   long does not end with a 0.3-second event.
4. **A hard ceiling.** If the plan exceeds ``max_windows`` the window length is
   scaled up and the plan rebuilt, so coverage stays complete (every second of
   the file is still in some window) rather than sampling only part of a long
   recording.

Frames within a window sit at the centres of equal sub-slices rather than on
the edges: the boundary of a window is exactly where a hard cut or a loading
screen tends to be, and a black frame teaches the model nothing.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, replace
from typing import Optional, Sequence

from editing.config import SamplingConfig


@dataclass(frozen=True)
class MotionPoint:
    """A motion/scene-change reading at a point in time. ``score`` is 0..1."""

    time: float
    score: float


@dataclass(frozen=True)
class SampleWindow:
    """One unit of analysis: a time range and the frames to show for it."""

    index: int
    start: float
    end: float
    frame_times: tuple[float, ...]
    dense: bool = False
    motion_score: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "frame_times": [round(t, 3) for t in self.frame_times],
            "dense": self.dense,
            "motion_score": round(self.motion_score, 4),
        }

    def cache_key_part(self) -> dict:
        """Only the fields that change what the model is shown.

        ``index`` is excluded on purpose: inserting a dense split earlier in a
        file renumbers later windows, and a window covering the same seconds
        with the same frames deserves the same cache entry regardless.
        """
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "frame_times": [round(t, 3) for t in self.frame_times],
            "dense": self.dense,
        }


def frame_times_for(start: float, end: float, count: int) -> tuple[float, ...]:
    """``count`` timestamps spread across ``[start, end)``, avoiding the edges.

    Each frame sits at the centre of an equal sub-slice, so three frames of an
    8-second window land at +1.33s, +4.0s and +6.67s. Requesting a frame at
    exactly ``end`` would often return the first frame of the *next* shot.
    """
    span = max(0.0, end - start)
    count = max(1, int(count))
    if span <= 0.0:
        return (round(start, 3),)
    slice_width = span / count
    return tuple(
        round(start + slice_width * (index + 0.5), 3) for index in range(count)
    )


def motion_score_for(
    motion: Sequence[MotionPoint],
    start: float,
    end: float,
    *,
    times: Optional[Sequence[float]] = None,
) -> float:
    """Peak motion inside ``[start, end)``.

    Peak rather than mean: a single hard scene change in an otherwise still
    window is exactly the moment worth looking at closely, and averaging it
    against seven quiet seconds would hide it.

    ``times`` lets a caller pass a pre-extracted sorted time list to avoid
    rebuilding it for every window of a long file.
    """
    if not motion:
        return 0.0
    if times is None:
        times = [point.time for point in motion]
    left = bisect.bisect_left(times, start)
    right = bisect.bisect_left(times, end)
    if left >= right:
        # No reading inside the window: fall back to the nearest one so a
        # coarse motion scan still informs a short window.
        nearest = min(left, len(motion) - 1)
        if nearest < 0:
            return 0.0
        return motion[nearest].score if abs(motion[nearest].time - start) <= (
            end - start
        ) else 0.0
    return max(motion[index].score for index in range(left, right))


def plan_windows(
    duration: float,
    config: SamplingConfig,
    *,
    motion: Optional[Sequence[MotionPoint]] = None,
) -> list[SampleWindow]:
    """The full sampling plan for one file.

    ``motion`` is optional; without it every window is planned at the normal
    density, which is correct but shows the model fewer frames during the
    moments that matter most.
    """
    config = config.validated()
    duration = float(duration or 0.0)
    if duration <= 0.0:
        return []

    windows = _build(duration, config, motion or ())

    # Enforce the ceiling by coarsening rather than truncating: a partial
    # timeline that silently stops at minute 12 of a 40-minute recording is a
    # far worse failure than a coarser one that covers all of it.
    guard = 0
    while len(windows) > config.max_windows and guard < 12:
        guard += 1
        scale = max(1.25, len(windows) / config.max_windows)
        config = replace(
            config,
            window_seconds=config.window_seconds * scale,
            dense_window_seconds=config.dense_window_seconds * scale,
        ).validated()
        windows = _build(duration, config, motion or ())

    return [
        SampleWindow(
            index=index,
            start=window.start,
            end=window.end,
            frame_times=window.frame_times,
            dense=window.dense,
            motion_score=window.motion_score,
        )
        for index, window in enumerate(windows)
    ]


def _build(
    duration: float,
    config: SamplingConfig,
    motion: Sequence[MotionPoint],
) -> list[SampleWindow]:
    times = [point.time for point in motion]
    stride = max(0.25, config.window_seconds - config.window_overlap)

    spans: list[tuple[float, float, float]] = []   # (start, end, motion score)
    start = 0.0
    # A float loop over a long duration accumulates error; stepping an integer
    # index keeps window boundaries exact and reproducible, which matters
    # because those boundaries end up in cache keys.
    step = 0
    while start < duration - 1e-6:
        end = min(duration, start + config.window_seconds)
        spans.append((start, end, motion_score_for(motion, start, end, times=times)))
        step += 1
        start = round(step * stride, 6)

    if not spans:
        spans = [(0.0, duration, motion_score_for(motion, 0.0, duration, times=times))]

    windows: list[SampleWindow] = []
    for span_start, span_end, score in spans:
        high_motion = score >= config.motion_threshold
        # Splitting only pays off when the window is meaningfully longer than
        # the dense width; otherwise a high-motion window just gets more
        # frames at its existing length.
        splittable = (span_end - span_start) > config.dense_window_seconds * 1.25

        if not (high_motion and splittable):
            windows.append(SampleWindow(
                index=0,
                start=span_start,
                end=span_end,
                frame_times=frame_times_for(
                    span_start, span_end,
                    config.dense_frames_per_window if high_motion
                    else config.frames_per_window,
                ),
                dense=high_motion,
                motion_score=score,
            ))
            continue

        for sub_start, sub_end in _split(span_start, span_end,
                                         config.dense_window_seconds):
            windows.append(SampleWindow(
                index=0,
                start=sub_start,
                end=sub_end,
                frame_times=frame_times_for(
                    sub_start, sub_end, config.dense_frames_per_window
                ),
                dense=True,
                motion_score=motion_score_for(
                    motion, sub_start, sub_end, times=times
                ) or score,
            ))

    return _merge_sliver(windows, config)


def _split(start: float, end: float, width: float) -> list[tuple[float, float]]:
    """Cut ``[start, end)`` into pieces of about ``width``, with no remainder.

    The pieces are equalised rather than leaving a short tail, so a dense
    12.5-second stretch at width 4 becomes 3 pieces of ~4.17s instead of
    3 pieces of 4s plus a 0.5s offcut.
    """
    span = max(0.0, end - start)
    if span <= 0.0:
        return [(start, end)]
    count = max(1, int(round(span / max(0.25, width))))
    piece = span / count
    return [
        (round(start + index * piece, 6), round(start + (index + 1) * piece, 6))
        for index in range(count)
    ]


def _merge_sliver(
    windows: list[SampleWindow], config: SamplingConfig
) -> list[SampleWindow]:
    """Fold a too-short final window into the one before it."""
    if len(windows) < 2:
        return windows
    last = windows[-1]
    if last.duration >= config.min_window_seconds:
        return windows

    previous = windows[-2]
    merged_end = max(previous.end, last.end)
    count = (
        config.dense_frames_per_window if previous.dense else config.frames_per_window
    )
    windows[-2] = SampleWindow(
        index=previous.index,
        start=previous.start,
        end=merged_end,
        frame_times=frame_times_for(previous.start, merged_end, count),
        dense=previous.dense or last.dense,
        motion_score=max(previous.motion_score, last.motion_score),
    )
    return windows[:-1]


def plan_summary(windows: Sequence[SampleWindow], duration: float) -> dict:
    """Numbers for the CLI to print before a long run starts.

    Seeing "412 windows, 1,236 frames" beforehand is what lets a user decide to
    raise ``--window-seconds`` instead of discovering the cost an hour in.
    """
    frames = sum(len(window.frame_times) for window in windows)
    dense = sum(1 for window in windows if window.dense)
    covered = sum(window.duration for window in windows)
    return {
        "duration": round(float(duration or 0.0), 2),
        "windows": len(windows),
        "dense_windows": dense,
        "frames": frames,
        "covered_seconds": round(covered, 2),
        "coverage_ratio": round(covered / duration, 3) if duration else 0.0,
        "seconds_per_window": round(duration / len(windows), 2) if windows else 0.0,
    }


def coverage_gaps(
    windows: Sequence[SampleWindow], duration: float, *, tolerance: float = 0.05
) -> list[tuple[float, float]]:
    """Stretches of the file no window covers.

    Should always be empty; it exists so a test and the CLI can *prove* that
    rather than assume it, because a silent gap is invisible in the output.
    """
    if not windows:
        return [(0.0, float(duration))] if duration > 0 else []
    gaps: list[tuple[float, float]] = []
    ordered = sorted(windows, key=lambda window: window.start)
    cursor = 0.0
    for window in ordered:
        if window.start > cursor + tolerance:
            gaps.append((round(cursor, 3), round(window.start, 3)))
        cursor = max(cursor, window.end)
    if duration - cursor > tolerance:
        gaps.append((round(cursor, 3), round(float(duration), 3)))
    return gaps


def estimate_calls(duration: float, config: SamplingConfig) -> int:
    """Rough window count before any motion data exists, for cost warnings."""
    config = config.validated()
    stride = max(0.25, config.window_seconds - config.window_overlap)
    return min(config.max_windows, max(1, math.ceil(float(duration or 0.0) / stride)))
