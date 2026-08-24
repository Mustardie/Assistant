"""One call from timeline + recommendations to a validated rough cut plan.

Everything the individual modules do, sequenced. Kept separate from the CLI so
the same path can be driven from a script or a test, and separate from
``convert`` so the selection knobs stay visible at the top level where a user
actually tunes them.

**Three ways ranges get chosen**, and only the first existed before Session
10C:

``heuristic``    ``select_ranges`` -- the thresholds. Always available, and
                 the fallback whenever anything else is not.
``director``     the ranges a director pass proposed and its safety layer
                 accepted, and nothing else.
``hybrid``       those ranges, with the heuristic filling every stretch the
                 director said nothing about.
``preselected``  ranges a caller worked out for itself, handed in whole. What
                 the retention pass uses, so a cut it reshaped goes through
                 exactly the same assembly as every other cut.

Everything after selection is identical in all three. The layout arithmetic,
the operation conversion, the dry run and every execution guard are the same
code on the same objects -- a director cut is a rough cut whose ranges came
from somewhere else, and is not a second path into Premiere.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from editing.recommend.schema import RecommendationSet
from editing.roughcut import convert, execute
from editing.roughcut.schema import RoughCutPlan
from editing.roughcut.select import (
    DEFAULT_FILLER_SPEED, DEFAULT_HANDLE, DEFAULT_KEEP_THRESHOLD, assemble,
    select_ranges,
)
from editing.schema import MediaAsset, StructureTimeline


@dataclass
class RoughCutOptions:
    """How aggressive the cut is. Every default is the conservative choice."""

    sequence_name: str = "Nova Rough Cut"
    #: Segments below this usefulness are dropped unless something keeps them.
    keep_threshold: float = DEFAULT_KEEP_THRESHOLD
    #: Playback rate for kept-but-dull silent footage.
    filler_speed: float = DEFAULT_FILLER_SPEED
    #: Seconds of handle either side of a kept range.
    handle: float = DEFAULT_HANDLE
    #: Drop low-value footage entirely instead of speeding it up.
    drop_filler: bool = False
    #: Convert punch-ins and push-ins into real Motion > Scale animation.
    allow_zooms: bool = True
    #: A .sqpreset path. Without one the sequence is created from the first
    #: clip so it inherits real settings.
    preset: str = ""
    #: Where the ranges come from: heuristic / director / hybrid. Defaults to
    #: the Session 3 behaviour, so nothing changes unless it is asked for.
    mode: str = "heuristic"

    def to_dict(self) -> dict:
        return {
            "sequence_name": self.sequence_name,
            "keep_threshold": self.keep_threshold,
            "filler_speed": self.filler_speed,
            "handle": self.handle,
            "drop_filler": self.drop_filler,
            "allow_zooms": self.allow_zooms,
            "preset": self.preset,
            "mode": self.mode,
        }


def build_rough_cut(
    timeline: StructureTimeline,
    recommendations: Optional[RecommendationSet] = None,
    *,
    assets: Optional[Sequence[MediaAsset]] = None,
    options: Optional[RoughCutOptions] = None,
    validate: bool = True,
    director_plan=None,
    preselected: Optional[Sequence] = None,
) -> RoughCutPlan:
    """Select, lay out, convert and (by default) dry-run a rough cut.

    ``validate=False`` is the ``plan-only`` path: it builds the operations but
    leaves ``dry_run_passed`` False, which the executor then refuses to run on.

    ``director_plan`` is read only when ``options.mode`` asks for it, and a
    mode that asks for a plan it was not given falls back to the heuristic
    with the fallback recorded on the cut. Silently producing a threshold cut
    while the report says "director" would be the worst outcome available.
    """
    options = options or RoughCutOptions()
    recommendations = recommendations or RecommendationSet()

    durations = {
        asset.asset_id: asset.duration for asset in (assets or timeline.assets)
    }
    paths = {
        asset.asset_id: asset.path for asset in (assets or timeline.assets)
    }

    ranges, selection_notes = _select(
        timeline, recommendations, options, durations, director_plan,
        preselected)
    # Segments carry the source path, but an explicitly discovered asset list
    # is more authoritative -- it is what was actually probed on disk.
    for entry in ranges:
        if entry.asset_id in paths:
            entry.source_file = paths[entry.asset_id]

    plan = RoughCutPlan(
        sequence_name=options.sequence_name,
        placements=assemble(ranges),
        on_scratch=True,
    )

    segments_by_id = {
        segment.segment_id: segment for segment in timeline.segments
    }
    convert.build_ops(
        plan, recommendations,
        sequence_name=options.sequence_name,
        allow_zooms=options.allow_zooms,
        segments_by_id=segments_by_id,
        preset=options.preset,
    )

    _add_warnings(plan, timeline, options)
    plan.explanation.extend(selection_notes)

    if validate:
        execute.dry_run(plan)
    return plan


def _select(
    timeline: StructureTimeline,
    recommendations: RecommendationSet,
    options: RoughCutOptions,
    durations: dict,
    director_plan,
    preselected: Optional[Sequence] = None,
) -> tuple:
    """The ranges this cut is built from, and how they were chosen.

    Returns ``(ranges, notes)``. The notes land in the plan's explanation, so
    a cut always says which selector produced it -- including when a requested
    director cut had to fall back to the thresholds.
    """
    def heuristic() -> list:
        return select_ranges(
            timeline, recommendations,
            keep_threshold=options.keep_threshold,
            filler_speed=options.filler_speed,
            handle=options.handle,
            keep_filler=not options.drop_filler,
            asset_durations=durations,
        )

    mode = (options.mode or "heuristic").strip().lower()
    if mode == "preselected":
        # A caller that already knows what it wants -- the retention pass.
        # It still goes through this builder so the assembly, the operations,
        # the dry run and every execution guard are the same code.
        if not preselected:
            return heuristic(), [
                "Mode 'preselected' was asked for with no ranges, so the "
                "rule-based selector chose them."
            ]
        return list(preselected), [
            f"Ranges supplied by the caller: {len(preselected)} range(s), "
            "assembled and validated exactly like any other cut."
        ]

    if mode == "heuristic":
        return heuristic(), ["Ranges chosen by the rule-based selector."]

    if director_plan is None or not getattr(director_plan, "ranges", None):
        return heuristic(), [
            f"Mode '{mode}' was asked for, but there is no usable director "
            "plan, so the rule-based selector chose the ranges. Run "
            "`director plan` first."
        ]

    from editing.director import convert as director_convert

    if mode == "director":
        ranges = director_convert.to_selected(director_plan)
        return ranges, [
            f"Ranges chosen by the director pass: {len(ranges)} range(s) from "
            f"{len(director_plan.accepted)} accepted decision(s), "
            f"{len(director_plan.rejected)} rejected by the safety layer."
        ]

    ranges, notes = director_convert.merged_with_heuristic(
        director_plan, timeline, recommendations,
        keep_threshold=options.keep_threshold,
        filler_speed=options.filler_speed,
        handle=options.handle,
        keep_filler=not options.drop_filler,
        asset_durations=durations,
    )
    return ranges, [
        f"Ranges chosen in hybrid mode: {notes['from_director']} from the "
        f"director and {notes['from_heuristic']} from the rule-based selector "
        f"for footage the director did not mention "
        f"({notes['heuristic_dropped']} rule-based range(s) superseded)."
    ]


def _add_warnings(
    plan: RoughCutPlan, timeline: StructureTimeline, options: RoughCutOptions
) -> None:
    """Say the things a user needs to know before trusting this cut."""
    if not plan.placements:
        return

    if not any(segment.audio_events for segment in timeline.segments):
        plan.warnings.append(
            "No audio events on this timeline, so dead air could not be "
            "detected and nothing was trimmed for silence. Run `audio` first."
        )

    compression = (
        plan.total_duration / plan.source_duration if plan.source_duration else 0.0
    )
    if compression > 0.95:
        plan.warnings.append(
            f"The cut keeps {compression:.0%} of the footage it draws on -- "
            "barely a cut. Raise --keep-threshold to tighten it."
        )

    sped = [p for p in plan.placements if p.speed != 1.0]
    if sped and options.filler_speed > 3.0:
        plan.warnings.append(
            f"{len(sped)} clip(s) are sped to {options.filler_speed:g}x. Past "
            "about 3x, filler stops reading as footage and starts reading as "
            "a joke."
        )

    if plan.unconverted:
        plan.warnings.append(
            f"{len(plan.unconverted)} recommendation(s) could not be converted "
            "into this cut; see the unconverted list for the reasons."
        )
