"""Layered edit recommendations.

    StructureTimeline (visual + transcript + audio)
        -> six layers of proposals
        -> safety pass
        -> draft Premiere plan
        -> offline dry-run validation

    schema.py         EditRecommendation and its vocabularies
    layers.py         the six layers, in order
    planner.py        runs them and keeps per-layer provenance
    premiere_plan.py  conversion to catalog operations + dry-run
    report.py         the human-readable output

Two things this package is careful about. **Nothing executes** — the draft plan
is validated offline and written to disk; running it is a separate decision for
a person. And **nothing is silently dropped** — the safety pass marks
recommendations ``rejected`` or ``downgraded`` with a reason instead of deleting
them, so the output always shows what was considered.
"""
from editing.recommend.layers import (
    PROPOSING_LAYERS, layer_audio, layer_pacing, layer_polish, layer_safety,
    layer_story, layer_visual,
)
from editing.recommend.planner import (
    PlannerOptions, plan_recommendations, summarise_segments,
)
from editing.recommend.premiere_plan import (
    DraftPlan, build_and_dry_run, build_plan, dry_run,
)
from editing.recommend.report import render, render_top_moments
from editing.recommend.schema import (
    ACTIVE_CATEGORIES, EDIT_CATEGORIES, INTENSITIES, RISKS, STATUSES,
    VIEWER_EFFECTS, EditRecommendation, Evidence, RecommendationSet,
)

__all__ = [
    # schema
    "EditRecommendation", "RecommendationSet", "Evidence",
    "EDIT_CATEGORIES", "ACTIVE_CATEGORIES", "INTENSITIES", "VIEWER_EFFECTS",
    "RISKS", "STATUSES",
    # layers
    "layer_story", "layer_pacing", "layer_visual", "layer_audio",
    "layer_polish", "layer_safety", "PROPOSING_LAYERS",
    # planner
    "plan_recommendations", "PlannerOptions", "summarise_segments",
    # premiere
    "build_plan", "dry_run", "build_and_dry_run", "DraftPlan",
    # report
    "render", "render_top_moments",
]
