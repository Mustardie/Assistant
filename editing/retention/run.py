"""One retention pass, end to end.

Reads what the other layers produced, decides, validates, and hands the result
to Session 3's builder. Nothing here executes anything, and the cut it read is
never modified -- the output is a new ``RoughCutPlan`` under its own name.

## Choosing the base

``retention``           the heuristic cut
``director_retention``  the director's cut; fails clearly without one
``hybrid``              the director's cut if there is one, else the heuristic
``report_only``         whichever exists, and change nothing

The distinction between ``director_retention`` and ``hybrid`` is what happens
when the director pass is missing. One says "I asked for the director's cut and
did not get it", the other says "use whatever is best available". Both are
reasonable to want; guessing which would leave somebody with a threshold cut
they believed was directed.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Sequence

from editing.config import EditingConfig
from editing.retention import compile as compile_module
from editing.retention.schema import (
    RetentionCutConfig, RetentionCutFailure, RetentionCutPlan,
)
from editing.roughcut.build import RoughCutOptions, build_rough_cut
from editing.roughcut.select import SelectedRange, select_ranges

logger = logging.getLogger("nova.editing.retention.run")

Reporter = Callable[[str], None]


def _quiet(_message: str) -> None:
    return None


def plan(
    config: EditingConfig,
    *,
    timeline,
    memory,
    retention,
    recommendations=None,
    roughcut=None,
    director_plan=None,
    assets: Optional[Sequence] = None,
    settings: Optional[RetentionCutConfig] = None,
    roughcut_options: Optional[RoughCutOptions] = None,
    name: str = "structure",
    say: Reporter = _quiet,
) -> tuple:
    """Build a retention cut plan and the ranges that follow from it.

    Returns ``(RetentionCutPlan, ranges)``. Never raises for anything a person
    can act on: a missing retention plan, a missing director cut and a cut
    with nothing in it are all results with a failure record and a hint.
    """
    started = time.time()
    settings = (settings or RetentionCutConfig()).validated()
    options = roughcut_options or RoughCutOptions()

    base_ranges, base, failure = _base(
        settings, timeline, recommendations, roughcut, director_plan,
        options, assets,
    )
    if failure is not None:
        empty = RetentionCutPlan(
            name=name, mode=settings.mode, config=settings, base=base,
            failure=failure,
        )
        return empty, list(base_ranges)

    say(f"[retention] {len(base_ranges)} range(s) from the {base} cut")

    built, ranges = compile_module.build(
        memory, retention, base_ranges, timeline,
        config=settings,
        roughcut=roughcut,
        director_plan=director_plan,
        base=base,
        name=name,
    )

    if assets:
        _apply_paths(ranges, assets)

    stats = built.stats()
    say(
        f"[retention] {stats['accepted']} accepted, {stats['rejected']} "
        f"rejected -> {stats['cut_duration']:.0f}s "
        f"(was {stats['base_duration']:.0f}s)"
    )
    if built.cold_open.chosen:
        say(f"[retention] cold open: {built.cold_open.hook_type} at "
            f"{built.cold_open.original_start:.0f}s, "
            f"{built.cold_open.duration:.0f}s long")
    logger.debug("Retention pass compiled in %.2fs", time.time() - started)
    return built, ranges


def _base(
    settings: RetentionCutConfig,
    timeline,
    recommendations,
    roughcut,
    director_plan,
    options: RoughCutOptions,
    assets,
) -> tuple:
    """The cut this pass applies on top of, and where it came from."""
    durations = {
        asset.asset_id: asset.duration
        for asset in (assets or getattr(timeline, "assets", []) or [])
    }

    wants_director = settings.mode in ("director_retention", "hybrid")
    has_director = director_plan is not None and bool(
        getattr(director_plan, "ranges", None))

    if wants_director and has_director:
        from editing.director import convert as director_convert
        return director_convert.to_selected(director_plan), "director", None

    if settings.mode == "director_retention" and not has_director:
        return [], "director", RetentionCutFailure(
            stage="no_base_cut",
            code="no_director_plan",
            message="Mode is 'director_retention' and there is no usable "
                    "director plan to build on.",
            hint="Run `python -m editing.cli director plan` first, or use "
                 "--mode retention to wire retention into the rule-based cut.",
            recoverable=True,
        )

    if roughcut is not None and getattr(roughcut, "placements", None):
        return _from_roughcut(roughcut), "heuristic", None

    ranges = select_ranges(
        timeline, recommendations,
        keep_threshold=options.keep_threshold,
        filler_speed=options.filler_speed,
        handle=options.handle,
        keep_filler=not options.drop_filler,
        asset_durations=durations,
    )
    return ranges, "heuristic", None


def _from_roughcut(roughcut) -> list[SelectedRange]:
    """A built rough cut, back in the shape the selector produces.

    Reading the cut that exists rather than re-selecting: the cut on disk may
    have come from the director, from a tuned threshold, or from somebody
    editing the JSON, and re-deriving it would quietly discard all three.
    """
    return [
        SelectedRange(
            asset_id=placement.asset_id,
            source_file=placement.source_file,
            start=placement.source_in,
            end=placement.source_out,
            keep_reason=placement.keep_reason,
            speed=placement.speed,
            protected=placement.protected,
            recommendation_ids=list(placement.recommendation_ids),
            segment_ids=list(placement.segment_ids),
            notes=placement.notes,
        )
        for placement in roughcut.placements
    ]


def _apply_paths(ranges: Sequence[SelectedRange], assets) -> None:
    paths = {asset.asset_id: asset.path for asset in (assets or ())}
    for entry in ranges:
        if entry.asset_id in paths:
            entry.source_file = paths[entry.asset_id]


def to_rough_cut(
    ranges: Sequence[SelectedRange],
    timeline,
    recommendations=None,
    *,
    assets: Optional[Sequence] = None,
    options: Optional[RoughCutOptions] = None,
    validate: bool = False,
    sequence_name: str = "",
):
    """Retention ranges into a rough cut, through the existing builder.

    Deliberately goes through ``build_rough_cut`` with a pre-made selection
    rather than reimplementing assembly: the layout arithmetic, the operation
    conversion, the dry run and every execution guard are the same code on the
    same objects. A retention cut is a rough cut whose ranges came from
    somewhere else.
    """
    from dataclasses import replace as _replace

    options = options or RoughCutOptions()
    if sequence_name:
        options = _replace(options, sequence_name=sequence_name)

    return build_rough_cut(
        timeline, recommendations,
        assets=assets,
        options=_replace(options, mode="preselected"),
        validate=validate,
        preselected=list(ranges),
    )
