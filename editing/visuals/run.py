"""One visual pass, end to end.

Reads what the earlier passes left on disk, plans, composes, and writes six
files. Executes nothing, needs no Premiere, no FFmpeg, no model and no GPU.

## Which cut it reads

The retention cut when there is one, otherwise the rough cut. Same rule the
polish passes follow and for the same reason: a treatment is a position on a
*timeline*, and planning against the pre-retention cut while the run rendered
the retention one would put every effect at the wrong moment. Whichever it used
is recorded on the plan.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from editing.config import EditingConfig
from editing.roughcut.schema import RoughCutPlan
from editing.schema import StructureTimeline
from editing.visuals import compose as compose_module
from editing.visuals import plan as plan_module
from editing.visuals import premiere as premiere_module
from editing.visuals import preview as preview_module
from editing.visuals import report as report_module
from editing.visuals import store
from editing.visuals.execution import FinalEditPlan, build_comparison
from editing.visuals.schema import VisualConfig, VisualLayerPlan

logger = logging.getLogger("nova.editing.visuals.run")

Reporter = Callable[[str], None]


def _quiet(_message: str) -> None:
    return None


def plan_visuals(
    config: EditingConfig,
    *,
    timeline: StructureTimeline,
    cut: RoughCutPlan,
    style,
    settings: Optional[VisualConfig] = None,
    director_plan=None,
    retention_plan=None,
    caption_plan=None,
    audio_plan=None,
    memory=None,
    retention_findings=None,
    base: str = "roughcut",
    name: str = "structure",
    run_id: str = "",
    fps: float = 30.0,
    save: bool = True,
    say: Reporter = _quiet,
) -> tuple:
    """Plan the visual layer and compose the final edit.

    Returns ``(VisualLayerPlan, FinalEditPlan)``. The second is composed even
    when the layer found nothing: "this edit has no visual treatment in it" is
    a fact about the edit, and a plan that only existed when something was
    added would make its absence invisible.
    """
    started = time.time()
    settings = (settings or VisualConfig()).validated()

    visuals = plan_module.build_visual_plan(
        timeline, cut, style, settings,
        director_plan=director_plan,
        retention_plan=retention_plan,
        caption_plan=caption_plan,
        audio_plan=audio_plan,
        memory=memory,
        retention_findings=retention_findings,
        base=base,
        name=name,
    )

    stats = visuals.stats()
    say(f"[visuals] {stats['moments']} moment(s), {stats['accepted']} of "
        f"{stats['considered']} treatment(s), "
        f"{stats['effects_per_minute']:.2f} a minute")
    for warning in visuals.warnings[:5]:
        say(f"  ! {warning}")

    final = compose_module.compose_final_edit(
        cut, visuals, settings,
        caption_plan=caption_plan,
        audio_plan=audio_plan,
        name=name,
        run_id=run_id,
        style=getattr(style, "name", "") or "",
        base=base,
        fps=fps,
    )

    if save:
        _write(config, visuals, final, name=name)
    logger.debug("Visual pass in %.2fs", time.time() - started)
    return visuals, final


def _write(config: EditingConfig, visuals: VisualLayerPlan,
           final: FinalEditPlan, *, name: str) -> list:
    """Everything this pass produces, on disk."""
    written = [
        store.save_plan(config, visuals, name=name),
        store.save_text(store.report_path(config, name),
                        report_module.render(visuals)),
        store.save_final(config, final, name=name),
        store.save_text(store.final_report_path(config, name),
                        report_module.render_final(final)),
    ]

    comparison = build_comparison(visuals, final)
    written.append(store.save_comparison(config, comparison, name=name))

    premiere = final.execution.premiere
    if premiere is not None:
        written.append(store.save_premiere(config, premiere, name=name))

    preview = final.execution.preview
    if preview is not None and visuals.accepted:
        target = preview_module.write_markers(
            visuals, preview, store.markers_path(config, name))
        if target is not None:
            written.append(target)
            # The sidecar path is only known after it is written, so the plan
            # is saved twice rather than leaving the JSON claiming there is no
            # marker file when there is one.
            store.save_final(config, final, name=name)
    return written


def export_premiere_plan(
    config: EditingConfig,
    *,
    name: str = "structure",
    visuals: Optional[VisualLayerPlan] = None,
    fps: float = 30.0,
    save: bool = True,
):
    """Build (or rebuild) the Premiere operation plan on its own.

    Separate from the pass so somebody who planned in ``plan_only`` mode can
    get an operation plan without re-planning the visuals -- and so the
    validation runs against whatever is on disk now rather than against what
    was on disk when the pass ran.
    """
    visuals = visuals or store.load_plan(config, name=name)
    plan = premiere_module.build_premiere_plan(visuals, name=name)
    premiere_module.validate_offline(plan, fps=fps)
    if save:
        store.save_premiere(config, plan, name=name)
    return plan


def markers_beside(visuals: VisualLayerPlan, final: FinalEditPlan,
                   video_path: str) -> Optional[Path]:
    """Write the visual marker file next to a rendered video.

    Named after the video so the two travel together. Returns None when there
    is nothing to write -- an empty marker file beside a proxy would suggest
    effects were tried and failed rather than that none were earned.
    """
    preview = final.execution.preview
    if preview is None:
        preview = preview_module.build_preview_plan(
            visuals, name=visuals.name)
    return preview_module.markers_beside(visuals, preview, video_path)
