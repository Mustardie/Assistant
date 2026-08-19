"""Visual analysis: sampled frames in, structured events out.

    duration + motion -> sampling plan -> frames -> Qwen3-VL -> VisualEvent

``sampling`` is the pure planner (no files, no model), ``prompt`` is what the
model is asked, ``frames`` does the FFmpeg work, ``qwen`` speaks to whatever is
serving the model, and ``analyzer`` sequences the four with the cache in front.
Splitting it this way keeps the part most likely to be wrong -- the sampling
policy -- directly testable without FFmpeg or a GPU.
"""
from editing.visual.analyzer import AnalysisResult, VisualAnalyzer, build_analyzer
from editing.visual.frames import (
    ExtractedFrames, FFmpegFrameSource, FrameSource, motion_stats, probe_motion,
)
from editing.visual.prompt import SYSTEM_PROMPT, build_user_prompt
from editing.visual.qwen import (
    MockVisionModel, OllamaVision, OpenAICompatibleVision, VisionModel,
    build_model, extract_json, health,
)
from editing.visual.sampling import (
    MotionPoint, SampleWindow, coverage_gaps, estimate_calls, frame_times_for,
    plan_summary, plan_windows,
)

__all__ = [
    # sampling
    "MotionPoint", "SampleWindow", "plan_windows", "plan_summary",
    "coverage_gaps", "frame_times_for", "estimate_calls",
    # frames
    "ExtractedFrames", "FrameSource", "FFmpegFrameSource", "probe_motion",
    "motion_stats",
    # model
    "VisionModel", "OpenAICompatibleVision", "OllamaVision", "MockVisionModel",
    "build_model", "extract_json", "health",
    # prompt
    "SYSTEM_PROMPT", "build_user_prompt",
    # orchestration
    "VisualAnalyzer", "AnalysisResult", "build_analyzer",
]
