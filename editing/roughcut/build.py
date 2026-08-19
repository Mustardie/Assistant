"""One call from timeline + recommendations to a validated rough cut plan.

Everything the individual modules do, sequenced. Kept separate from the CLI so
the same path can be driven from a script or a test, and separate from
``convert`` so the selection knobs stay visible at the top level where a user
actually tunes them.
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

    def to_dict(self) -> dict:
        return {
            "sequence_name": self.sequence_name,
            "keep_threshold": self.keep_threshold,
            "filler_speed": self.filler_speed,
            "handle": self.handle,
            "drop_filler": self.drop_filler,
            "allow_zooms": self.allow_zooms,
            "preset": self.preset,
        }


def build_rough_cut(
    timeline: StructureTimeline,
    recommendations: Optional[RecommendationSet] = None,
    *,
    assets: Optional[Sequence[MediaAsset]] = None,
    options: Optional[RoughCutOptions] = None,
    validate: bool = True,
) -> RoughCutPlan:
    """Select, lay out, convert and (by default) dry-run a rough cut.

    ``validate=False`` is the ``plan-only`` path: it builds the operations but
    leaves ``dry_run_passed`` False, which the executor then refuses to run on.
    """
    options = options or RoughCutOptions()
    recommendations = recommendations or RecommendationSet()

    durations = {
        asset.asset_id: asset.duration for asset in (assets or timeline.assets)
    }
    paths = {
        asset.asset_id: asset.path for asset in (assets or timeline.assets)
    }

    ranges = select_ranges(
        timeline, recommendations,
        keep_threshold=options.keep_threshold,
        filler_speed=options.filler_speed,
        handle=options.handle,
        keep_filler=not options.drop_filler,
        asset_durations=durations,
    )
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

    if validate:
        execute.dry_run(plan)
    return plan


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
