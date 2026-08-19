"""Visual analysis: sampled frames in, structured events out.

    duration + motion signal -> sampling plan -> frames -> Qwen3-VL -> VisualEvent

``sampling`` is the pure planner (no files, no model), ``prompt`` is what the
model is asked, and the frame extraction and model client sit behind them.
Splitting it this way keeps the part most likely to be wrong -- the sampling
policy -- directly testable without FFmpeg or a GPU.
"""
from editing.visual.sampling import (
    MotionPoint, SampleWindow, coverage_gaps, estimate_calls, frame_times_for,
    plan_summary, plan_windows,
)

__all__ = [
    "MotionPoint", "SampleWindow", "plan_windows", "plan_summary",
    "coverage_gaps", "frame_times_for", "estimate_calls",
]
