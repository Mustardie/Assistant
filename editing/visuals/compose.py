"""The final edit composer.

    cut + captions + sound + visuals -> FinalEditPlan

Assembly, not decision. Every part of the result was chosen by the pass that
owns that decision; this exists so there is one file a person can read to find
out what the edit *is*, instead of five files and a mental model of how they
relate.

## What the modes mean

``off``            compose nothing.
``plan_only``      the ``FinalEditPlan`` and nothing else. The default.
``proxy_preview``  also the FFmpeg capability statement and a sidecar marker
                   file. Still burns nothing in.
``premiere_plan``  also a Premiere operation plan, validated offline.
``hybrid``         both.

None of them executes anything. ``premiere_plan`` produces a *plan*; running it
is a separate, explicit act behind its own ``--yes``, which is how every other
executable thing in this system works.
"""
from __future__ import annotations

import logging
from typing import Sequence

from editing.roughcut.schema import RoughCutPlan
from editing.visuals.execution import (
    FinalEditPlan, FinalEditSegment, VisualExecutionPlan,
)
from editing.visuals.schema import (
    PREVIEW_NOTE, VisualConfig, VisualLayerPlan,
)

logger = logging.getLogger("nova.editing.visuals.compose")


def compose_final_edit(
    cut: RoughCutPlan,
    visuals: VisualLayerPlan,
    config: VisualConfig,
    *,
    caption_plan=None,
    audio_plan=None,
    name: str = "structure",
    run_id: str = "",
    style: str = "",
    base: str = "roughcut",
    fps: float = 30.0,
) -> FinalEditPlan:
    """Everything this run decided, as one readable object."""
    from editing.visuals.schema import now

    config = config.validated()
    final = FinalEditPlan(
        name=name,
        mode=config.mode,
        style=style or visuals.style,
        sequence_name=cut.sequence_name,
        run_id=run_id,
        base=base,
        duration=round(cut.total_duration, 3),
        visuals=visuals,
        generated_at=now(),
    )

    if not config.composes:
        final.warnings.append(
            "the composer is off for this run, so nothing was assembled.")
        return final

    _segments(final, cut, visuals, caption_plan, audio_plan)
    _summaries(final, caption_plan, audio_plan)
    final.execution = _execution(visuals, config, name=name, fps=fps)
    _warnings(final, visuals, config)
    _manual_checks(final, visuals)
    return final


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

def _segments(final: FinalEditPlan, cut: RoughCutPlan,
              visuals: VisualLayerPlan, caption_plan, audio_plan) -> None:
    """One record per clip, with everything landing on it.

    Assigned by time rather than by ``placement_id``: a caption or a cue knows
    when it is, and the clip it sits on is whichever one is playing then. That
    is also the only assignment that stays right if a plan is read against a
    cut that has since been rebuilt.
    """
    captions = _windows(getattr(caption_plan, "accepted", []) or [],
                        lambda item: (item.start, item.end, item.caption_id))
    cues = _windows(getattr(audio_plan, "accepted", []) or [],
                    lambda item: (item.start, item.end, item.cue_id))
    treatments = _windows(visuals.accepted,
                          lambda item: (item.start, item.end,
                                        item.treatment_id))

    for index, placement in enumerate(cut.placements):
        segment = FinalEditSegment(
            index=index,
            placement_id=placement.placement_id,
            asset_id=placement.asset_id,
            source_file=placement.source_file,
            start=placement.sequence_start,
            end=placement.sequence_end,
            source_in=placement.source_in,
            source_out=placement.source_out,
            speed=placement.speed,
            keep_reason=placement.keep_reason,
            protected=placement.protected,
        )
        low, high = placement.sequence_start, placement.sequence_end
        segment.treatments = _overlapping(treatments, low, high)
        segment.captions = _overlapping(captions, low, high)
        segment.audio_cues = _overlapping(cues, low, high)

        if segment.protected and segment.treatments:
            segment.notes.append(
                "the pacing layer asked for this clip to be left alone, and "
                "it now carries a visual treatment. Worth a look.")
        if segment.is_busy:
            segment.notes.append(
                f"{len(segment.treatments)} treatment(s) and "
                f"{len(segment.captions)} caption(s) on one clip: this is the "
                "first place to look if the edit feels over-worked.")
        if segment.speed != 1.0 and segment.treatments:
            segment.notes.append(
                f"this clip is retimed ({segment.speed:g}x) and carries a "
                "treatment. Two edits compounding on one piece of footage.")
        final.segments.append(segment)


def _windows(items: Sequence, unpack) -> list[tuple]:
    out: list[tuple] = []
    for item in items:
        start, end, key = unpack(item)
        if start is None or start < 0:
            continue
        out.append((float(start), float(end), str(key)))
    return out


def _overlapping(windows: Sequence[tuple], low: float,
                 high: float) -> list[str]:
    return [key for start, end, key in windows
            if max(end, start) > low and start < high]


# ---------------------------------------------------------------------------
# Summaries, execution and prose
# ---------------------------------------------------------------------------

def _summaries(final: FinalEditPlan, caption_plan, audio_plan) -> None:
    """Counts lifted from the two polish plans.

    Copied rather than referenced so this object is readable on its own. A
    person opening the final edit plan should not have to load two more files
    to find out whether it has captions in it.
    """
    if caption_plan is not None:
        stats = caption_plan.stats()
        final.caption_summary = {
            "mode": caption_plan.mode,
            "accepted": stats["accepted"],
            "rejected": stats["rejected"],
            "per_minute": stats["captions_per_minute"],
            "burned_in": bool(getattr(caption_plan, "burned_in", False)),
            "sidecar": str(getattr(caption_plan, "sidecar_path", "")),
        }
    if audio_plan is not None:
        stats = audio_plan.stats()
        final.audio_summary = {
            "mode": audio_plan.mode,
            "accepted": stats["accepted"],
            "placed": stats["placed"],
            "missing_assets": stats["missing_assets"],
            "per_minute": stats["sfx_per_minute"],
            "plays_anything": bool(audio_plan.placed),
        }


def _execution(visuals: VisualLayerPlan, config: VisualConfig, *,
               name: str, fps: float) -> VisualExecutionPlan:
    """Whichever output paths this run asked for. Executes neither."""
    from editing.visuals import premiere as premiere_module
    from editing.visuals import preview as preview_module

    execution = VisualExecutionPlan(mode=config.mode)

    if config.wants_premiere:
        plan = premiere_module.build_premiere_plan(visuals, name=name)
        premiere_module.validate_offline(plan, fps=fps)
        execution.premiere = plan
        if not plan.dry_run_passed and plan.dry_run_error:
            execution.warnings.append(
                "the Premiere visual plan did not validate offline: "
                + str(plan.dry_run_error.get("error", ""))[:200])

    if config.wants_preview:
        execution.preview = preview_module.build_preview_plan(
            visuals, name=name)
        execution.warnings.append(PREVIEW_NOTE)

    execution.placeholder_only = [
        treatment.treatment_id for treatment in visuals.accepted
        if treatment.target_output == "placeholder_only"
    ]
    return execution


def _warnings(final: FinalEditPlan, visuals: VisualLayerPlan,
              config: VisualConfig) -> None:
    final.warnings.extend(visuals.warnings)
    final.warnings.extend(final.execution.warnings)

    busy = final.busy_segments
    if busy:
        final.warnings.append(
            f"{len(busy)} clip(s) carry more than two things at once. Nothing "
            "refused them -- every safety rule passed -- but they are where an "
            "edit starts feeling over-worked."
        )
    if config.layer == "high":
        final.warnings.append(
            "this plan was made at the 'high' visual layer. Watch a minute of "
            "it before committing to the rest."
        )
    if not visuals.accepted and visuals.moments:
        final.warnings.append(
            f"{len(visuals.moments)} moment(s) were found and none earned a "
            "treatment. The plan's rejection list says why for each."
        )


def _manual_checks(final: FinalEditPlan, visuals: VisualLayerPlan) -> None:
    """What a person has to settle before any of this is worth trusting."""
    checks: list[str] = []

    callouts = [t for t in visuals.accepted if t.family == "callout"]
    if callouts:
        checks.append(
            f"Place every one of the {len(callouts)} callout(s) by hand. This "
            "system knows what is on screen and never where, so each one "
            "lands at the centre of the frame with a note saying so."
        )
    picture = [t for t in visuals.accepted if t.changes_the_picture]
    if picture:
        checks.append(
            f"Check the HUD on the {len(picture)} clip(s) whose picture is "
            "scaled. The ceiling is computed for 16:9 and has not looked at "
            "your footage."
        )
    if visuals.lowered:
        checks.append(
            f"{len(visuals.lowered)} treatment(s) were softened rather than "
            "refused. Read what softened them and decide whether the softer "
            "version is still worth having."
        )
    cards = [t for t in visuals.accepted if t.family == "card"]
    if cards:
        checks.append(
            f"Read the {len(cards)} card(s). Their words come from what was "
            "said or seen; nothing was written for them, which also means "
            "nothing was polished."
        )
    if final.busy_segments:
        checks.append(
            f"Watch the {len(final.busy_segments)} busy clip(s) first. If any "
            "of this reads as over-edited, it will be there."
        )
    checks.append(
        "Nothing in this plan has been drawn or rendered. Watching the proxy "
        "shows the cut, not the edit."
    )
    final.manual_checks = checks
