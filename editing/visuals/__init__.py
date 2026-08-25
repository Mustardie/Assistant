"""The creative visual layer: where the edit points at something.

    moments -> candidates -> safety -> density -> VisualLayerPlan
                                                      |
                          cut + captions + sound + visuals -> FinalEditPlan
                                                      |
                              Premiere operations  /  FFmpeg capability + markers

    schema.py     VisualMoment, VisualEffectCandidate, VisualTreatment,
                  VisualSafetyCheck, VisualLayerPlan, VisualConfig
    execution.py  PremiereVisualOperationPlan, FFmpegVisualPreviewPlan,
                  VisualExecutionPlan, FinalEditPlan, VisualReport,
                  VisualComparisonReport
    moments.py    what the earlier passes recorded, resolved onto the cut
    treatments.py which effects suit which moment, and what each style is for
    safety.py     the fourteen rules that stop this being embarrassing
    plan.py       one visual pass: detect, propose, check, thin
    compose.py    the final edit composer
    premiere.py   the operation plan, validated offline
    preview.py    what FFmpeg could show, and the sidecar marker file
    report.py     the readable reports
    store.py      where it all lives
    run.py        one pass, end to end

Everything before this decided *what footage is in the edit*. This decides
where the edit should point at something — and its whole design is written
against one failure mode: effects everywhere.

**Every treatment names the moment it is for.** No effect comes from a clock, a
beat grid or "every N seconds". A candidate exists because the director
accepted a decision there, the retention pass moved that footage, the caption
pass found a payoff line, or the vision pass saw a creeper. No evidence, no
effect.

**Every refusal is kept**, with the named rule that made it and what that rule
measured — including the moments the style never offered anything for. "Four
effects" and "forty candidates, thirty-six refused, here is why" are different
reports, and only the second one distinguishes taste from a bug.

**The HUD is protected before anything else.** Minecraft's health, hunger and
hotbar are information the viewer is reading, and no style may override the
check that keeps them on screen.

**Nothing is drawn, rendered or executed.** The Premiere operations are
proposals validated offline; the FFmpeg preview is a capability statement and a
sidecar marker file. ``burned_in`` is False everywhere and there is no code
path that sets it True.
"""
from editing.visuals.compose import compose_final_edit
from editing.visuals.execution import (
    FFmpegVisualPreviewPlan, FinalEditPlan, FinalEditSegment, PreviewItem,
    PremiereVisualOperation, PremiereVisualOperationPlan,
    UnsupportedTreatment, VisualComparisonReport, VisualExecutionPlan,
    VisualReport, build_comparison,
)
from editing.visuals.moments import detect_moments
from editing.visuals.plan import build_visual_plan
from editing.visuals.premiere import (
    build_premiere_plan, can_express, validate_offline,
)
from editing.visuals.preview import (
    build_preview_plan, markers_beside, render_markers, support_for,
    target_for,
)
from editing.visuals.report import build_report, render, render_final
from editing.visuals.safety import check_all
from editing.visuals.schema import (
    COMPOSER_MODES, EFFECT_FAMILIES, EFFECT_FAMILY, EFFECT_TYPES, INTENSITIES,
    NOT_MEASURED, NOT_RENDERED, PREVIEW_NOTE, REJECT_REASONS, SOURCE_TYPES,
    TARGET_OUTPUTS, VISUAL_LAYERS, VISUAL_MOMENT_TYPES, VisualConfig,
    VisualEffectCandidate, VisualLayerPlan, VisualMoment, VisualSafetyCheck,
    VisualTreatment, family_of,
)
from editing.visuals.treatments import (
    MOMENT_EFFECTS, STYLE_RULES, allowed_effects, propose, visual_defaults,
)

__all__ = [
    # schema
    "VisualConfig", "VisualMoment", "VisualEffectCandidate",
    "VisualTreatment", "VisualSafetyCheck", "VisualLayerPlan",
    "VISUAL_LAYERS", "COMPOSER_MODES", "VISUAL_MOMENT_TYPES", "EFFECT_TYPES",
    "EFFECT_FAMILIES", "EFFECT_FAMILY", "INTENSITIES", "SOURCE_TYPES",
    "TARGET_OUTPUTS", "REJECT_REASONS", "NOT_RENDERED", "NOT_MEASURED",
    "PREVIEW_NOTE", "family_of",
    # execution
    "FinalEditPlan", "FinalEditSegment", "VisualExecutionPlan",
    "PremiereVisualOperationPlan", "PremiereVisualOperation",
    "UnsupportedTreatment", "FFmpegVisualPreviewPlan", "PreviewItem",
    "VisualReport", "VisualComparisonReport", "build_comparison",
    # passes
    "detect_moments", "propose", "check_all", "build_visual_plan",
    "compose_final_edit",
    "allowed_effects", "visual_defaults", "MOMENT_EFFECTS", "STYLE_RULES",
    # outputs
    "build_premiere_plan", "validate_offline", "can_express",
    "build_preview_plan", "support_for", "target_for", "render_markers",
    "markers_beside",
    # reports
    "render", "render_final", "build_report",
]
