"""Turning a rough cut into something you can actually watch.

Sessions 1-9 produce plans. Session 10A produces transcripts. Nothing so far
produces *video*: the only way to see a decision was to open Premiere and
execute against it, which is the slowest and riskiest loop in the system.

This package closes that. Given a ``RoughCutPlan`` -- which already carries
exact source ranges, their order and their speeds -- it renders a proxy MP4
with FFmpeg, writes review notes beside it, and reuses the render when nothing
has changed. No Premiere, no GPU, no model.

The loop it exists to make cheap::

    change a rule -> build the plan -> render -> watch -> change a rule

Nothing here is a delivery render. It is deliberately fast and deliberately
ugly, because the question it answers is "does this cut work", not "is this
ready to upload".
"""
from __future__ import annotations

from editing.render.schema import (
    RenderArtifact, RenderConfig, RenderFailure, RenderInput, RenderJob,
    RenderReport, RenderResult, RenderSegment,
)

__all__ = [
    "RenderArtifact", "RenderConfig", "RenderFailure", "RenderInput",
    "RenderJob", "RenderReport", "RenderResult", "RenderSegment",
]
