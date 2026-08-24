"""One polish pass, end to end.

Reads whatever the earlier passes left on disk, decides, writes two plans, two
reports and one sidecar. Executes nothing, needs no Premiere, no FFmpeg, no
model and no asset library.

## Which cut it reads

The retention cut when there is one, otherwise the rough cut. That order is
load-bearing: captions and cues are positions on a *timeline*, and planning
them against the pre-retention cut while the run rendered the retention one
would put every one of them at the wrong moment. Whichever it used is recorded
on the plan.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from editing.config import EditingConfig
from editing.polish import audio as audio_module
from editing.polish import captions as captions_module
from editing.polish import report as report_module
from editing.polish import sidecar as sidecar_module
from editing.polish import store
from editing.polish.schema import (
    AudioPolishConfig, AudioPolishPlan, CaptionConfig, CaptionPlan,
)
from editing.roughcut.schema import RoughCutPlan
from editing.schema import StructureTimeline
from editing.style.presets import StylePreset

logger = logging.getLogger("nova.editing.polish.run")

Reporter = Callable[[str], None]


def _quiet(_message: str) -> None:
    return None


def plan_captions(
    config: EditingConfig,
    *,
    timeline: StructureTimeline,
    cut: RoughCutPlan,
    style: StylePreset,
    settings: Optional[CaptionConfig] = None,
    memory=None,
    name: str = "structure",
    save: bool = True,
    say: Reporter = _quiet,
) -> CaptionPlan:
    """Decide which lines earn a caption, and write the plan and sidecar."""
    started = time.time()
    settings = (settings or CaptionConfig()).validated()

    plan = captions_module.build_caption_plan(
        timeline, cut, style, settings, memory=memory, name=name)

    stats = plan.stats()
    say(f"[polish] captions: {stats['accepted']} of {stats['considered']} "
        f"line(s), {stats['captions_per_minute']:.2f} a minute")
    for warning in plan.warnings[:5]:
        say(f"  ! {warning}")

    if save:
        store.save_captions(config, plan, name=name)
        if plan.accepted:
            sidecar_module.write_srt(
                plan, store.sidecar_path(config, name))
            # The sidecar path is only known after it is written, so the plan
            # is saved twice rather than leaving the JSON claiming there is
            # no sidecar when there is one.
            store.save_captions(config, plan, name=name)
        store.save_text(
            store.caption_report_path(config, name),
            report_module.render_captions(plan),
        )
    logger.debug("Caption polish in %.2fs", time.time() - started)
    return plan


def plan_audio(
    config: EditingConfig,
    *,
    timeline: StructureTimeline,
    cut: RoughCutPlan,
    style: StylePreset,
    settings: Optional[AudioPolishConfig] = None,
    library=None,
    name: str = "structure",
    save: bool = True,
    say: Reporter = _quiet,
) -> AudioPolishPlan:
    """Decide which sounds belong where, and write the plan and report."""
    started = time.time()
    settings = (settings or AudioPolishConfig()).validated()

    plan = audio_module.build_audio_plan(
        timeline, cut, style, settings, library=library, name=name)

    stats = plan.stats()
    say(f"[polish] audio: {stats['accepted']} of {stats['considered']} cue(s), "
        f"{stats['placed']} from the library, "
        f"{stats['missing_assets']} missing")
    for warning in plan.warnings[:5]:
        say(f"  ! {warning}")

    if save:
        store.save_audio(config, plan, name=name)
        store.save_text(
            store.audio_report_path(config, name),
            report_module.render_audio(plan),
        )
    logger.debug("Audio polish in %.2fs", time.time() - started)
    return plan


def sidecar_beside(plan: CaptionPlan, video_path: str) -> Optional[Path]:
    """Write the caption sidecar next to a rendered video.

    Named after the video with a ``.srt`` extension, which is the one naming
    convention every player looks for. Returns None when there is nothing to
    write -- an empty subtitle file beside a proxy would suggest captions were
    tried and failed, rather than that none were earned.
    """
    if not video_path or not plan.accepted:
        return None
    target = Path(video_path).with_suffix(".srt")
    try:
        target.write_text(
            sidecar_module.to_srt(plan.decisions), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - a sidecar is never worth failing
        logger.debug("Could not write sidecar at %s: %s", target, exc)
        return None
    return target
