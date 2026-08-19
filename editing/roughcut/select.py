"""Choosing what goes in the cut, and where it lands.

Two pure steps, both testable without Premiere or FFmpeg:

``select_ranges``   which stretches of source footage are worth keeping
``assemble``        where each kept range sits on the sequence

Keeping them separate matters. Selection is a judgement about content and can
be argued with; assembly is arithmetic and must simply be right. Conflating
them would make an off-by-one in the layout look like a taste disagreement.

**The selection rule, stated plainly.** A range is kept when the timeline says
it is usable, or when a recommendation explicitly asks for it (a hold, a
marker-worthy beat). It is dropped when it is dead air, when the vision model
failed on it, or when nothing scored it above the floor. Adjacent kept ranges
are merged so the cut does not contain a seam every eight seconds where two
analysis windows happened to meet.

**Anticipation is protected.** The beat before a payoff is what makes the
payoff land, so a low-scoring segment immediately preceding a high-value one is
kept anyway, and marked so nothing later speeds it up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from editing.recommend.schema import EditRecommendation, RecommendationSet
from editing.roughcut.schema import ClipPlacement, placement_id_for
from editing.schema import IMPORTANCE_WEIGHT, StructureTimeline, TimelineSegment

#: Importance levels that carry a video and are never sped up or trimmed.
HIGH_VALUE = frozenset({"payoff", "reveal", "danger", "funny"})

#: Segments below this usefulness are dropped unless something else keeps them.
DEFAULT_KEEP_THRESHOLD = 0.40

#: Speed applied to kept-but-dull filler. Deliberately mild: 2x reads as
#: "moving along", 4x reads as a joke and stops being watchable.
DEFAULT_FILLER_SPEED = 2.0

#: Handles added either side of a kept range, so cuts do not land exactly on
#: the first and last frame of an analysis window.
DEFAULT_HANDLE = 0.25

#: Ranges closer together than this are merged into one clip.
MERGE_GAP = 0.75

#: A clip shorter than this is not worth a cut point.
MIN_CLIP_SECONDS = 1.0


@dataclass
class SelectedRange:
    """A stretch of one source file that the cut will use."""

    asset_id: str
    source_file: str
    start: float
    end: float
    keep_reason: str = "unknown"
    speed: float = 1.0
    protected: bool = False
    recommendation_ids: list[str] = field(default_factory=list)
    segment_ids: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def overlaps(self, other: "SelectedRange") -> bool:
        return self.start < other.end and other.start < self.end


def _reason_for(segment: TimelineSegment) -> str:
    """Why this segment is worth keeping, in the vocabulary of the cut."""
    importance = segment.importance
    if importance in ("payoff", "reveal", "danger", "funny", "tension"):
        return importance
    if segment.audio_reaction is not None:
        return "audio_reaction"
    if segment.alignment == "contrast":
        return "contrast"
    if importance == "setup":
        return "setup"
    return "filler"


def select_ranges(
    timeline: StructureTimeline,
    recommendations: Optional[RecommendationSet] = None,
    *,
    keep_threshold: float = DEFAULT_KEEP_THRESHOLD,
    filler_speed: float = DEFAULT_FILLER_SPEED,
    handle: float = DEFAULT_HANDLE,
    keep_filler: bool = True,
    asset_durations: Optional[dict] = None,
) -> list[SelectedRange]:
    """Decide which source ranges the rough cut uses.

    ``keep_filler`` controls what happens to low-value-but-not-dead footage:
    kept and sped up (the default, which preserves continuity), or dropped
    entirely (a tighter, more disjointed cut).
    """
    recommendations = recommendations or RecommendationSet()
    durations = asset_durations or {}

    holds = _holds_by_segment(recommendations, timeline)
    trims = _trim_ranges(recommendations)
    selected: list[SelectedRange] = []

    segments = sorted(timeline.segments, key=lambda s: (s.asset_id, s.start))
    for index, segment in enumerate(segments):
        following = segments[index + 1] if index + 1 < len(segments) else None
        decision = _decide(
            segment, following, holds, trims,
            keep_threshold=keep_threshold,
            filler_speed=filler_speed,
            keep_filler=keep_filler,
        )
        if decision is not None:
            selected.append(decision)

    padded = [
        _apply_handles(entry, handle, durations.get(entry.asset_id))
        for entry in selected
    ]
    return _merge(padded)


def _holds_by_segment(
    recommendations: RecommendationSet, timeline: StructureTimeline
) -> dict:
    """Map ``(asset_id, rounded start)`` -> the hold recommendations there.

    Only *deliberate* holds count. A hold the safety pass forced on an
    over-eager punch-in says nothing about whether the footage is good; reading
    it as "protect this" would let the anti-trash pass accidentally decide the
    edit.
    """
    out: dict = {}
    for entry in recommendations.recommendations:
        if not entry.is_deliberate_hold:
            continue
        key = (entry.asset_id, round(entry.start, 3))
        out.setdefault(key, []).append(entry)
    return out


def _trim_ranges(recommendations: RecommendationSet) -> list:
    """Accepted trim_dead_air recommendations, as (asset_id, start, end)."""
    return [
        (entry.asset_id, entry.start, entry.end)
        for entry in recommendations.recommendations
        if entry.category == "trim_dead_air" and entry.status == "accepted"
    ]


def _is_trimmed(segment: TimelineSegment, trims: Sequence) -> bool:
    """Whether a trim covers most of this segment."""
    for asset_id, start, end in trims:
        if asset_id != segment.asset_id:
            continue
        overlap = max(0.0, min(segment.end, end) - max(segment.start, start))
        if segment.duration > 0 and overlap >= segment.duration * 0.6:
            return True
    return False


def _decide(
    segment: TimelineSegment,
    following: Optional[TimelineSegment],
    holds: dict,
    trims: Sequence,
    *,
    keep_threshold: float,
    filler_speed: float,
    keep_filler: bool,
) -> Optional[SelectedRange]:
    """Keep, speed up, or drop one segment."""
    held = holds.get((segment.asset_id, round(segment.start, 3)), [])

    def build(reason: str, *, speed: float = 1.0, protected: bool = False,
              notes: str = "") -> SelectedRange:
        return SelectedRange(
            asset_id=segment.asset_id,
            source_file=segment.source_file,
            start=segment.start,
            end=segment.end,
            keep_reason=reason,
            speed=speed,
            protected=protected,
            recommendation_ids=[entry.recommendation_id for entry in held],
            segment_ids=[segment.segment_id],
            notes=notes,
        )

    # Dead air goes, always. It is the one thing the audio layer is certain
    # about and the one thing a viewer never wants.
    if segment.is_dead_air:
        return None
    if _is_trimmed(segment, trims):
        return None

    # A failed analysis is not evidence of anything, so it cannot justify
    # keeping footage -- but it is not evidence against it either, which is
    # why this is a drop rather than a hard error.
    if segment.events and all(event.error for event in segment.events):
        return None

    # A deliberate hold is the strongest possible keep: a layer looked at this
    # and said leave it exactly as it is.
    if held:
        return build("hold", protected=True,
                     notes="Held by the pacing layer: no effects, no retime.")

    importance = segment.importance
    if importance in HIGH_VALUE:
        return build(_reason_for(segment), protected=True,
                     notes="High-value moment: kept at full speed.")

    # Anticipation. The beat before a payoff earns its place by what follows
    # it, so it is kept even when it scores badly on its own.
    if (
        following is not None
        and following.asset_id == segment.asset_id
        and following.importance in ("payoff", "reveal")
        and not segment.is_dead_air
    ):
        return build("tension", protected=True,
                     notes="Anticipation before a payoff: preserved at full speed.")

    if segment.usefulness >= keep_threshold or segment.usable:
        return build(_reason_for(segment))

    if not keep_filler:
        return None

    # Everything left is watchable but dull. Speeding it up keeps continuity
    # without spending the viewer's patience -- unless someone is talking,
    # because sped-up dialogue is unusable.
    if segment.has_speech:
        return build("setup", notes="Low-value but has narration: kept at "
                                    "full speed rather than sped up.")
    return build("filler", speed=filler_speed,
                 notes=f"Low-value silent footage: {filler_speed:g}x.")


def _apply_handles(
    entry: SelectedRange, handle: float, duration: Optional[float]
) -> SelectedRange:
    """Widen a range slightly so cuts do not land on a window boundary."""
    if handle <= 0:
        return entry
    entry.start = max(0.0, entry.start - handle)
    entry.end = entry.end + handle
    if duration and duration > 0:
        entry.end = min(entry.end, duration)
    return entry


def _merge(ranges: Sequence[SelectedRange]) -> list[SelectedRange]:
    """Join adjacent ranges from the same file into single clips.

    Without this the cut contains a cut point every analysis window, which
    looks like a stutter rather than an edit. Ranges only merge when they share
    a speed -- joining a 2x filler run to a 1x payoff would silently retime the
    payoff.
    """
    ordered = sorted(ranges, key=lambda entry: (entry.asset_id, entry.start))
    merged: list[SelectedRange] = []

    for entry in ordered:
        previous = merged[-1] if merged else None

        # Handles can push two ranges into each other. When they cannot merge
        # (different speeds), the overlap must be resolved or the same footage
        # appears twice -- and worse, a slice of a protected payoff would end up
        # inside the sped-up filler clip that follows it.
        if (
            previous is not None
            and previous.asset_id == entry.asset_id
            and previous.speed != entry.speed
            and entry.start < previous.end
        ):
            # The protected side keeps the contested frames; when neither is
            # protected the earlier clip keeps them, which preserves order.
            if entry.protected and not previous.protected:
                previous.end = entry.start
            else:
                entry.start = previous.end

        if (
            previous is not None
            and previous.asset_id == entry.asset_id
            and previous.speed == entry.speed
            and entry.start <= previous.end + MERGE_GAP
        ):
            previous.end = max(previous.end, entry.end)
            previous.protected = previous.protected or entry.protected
            previous.recommendation_ids = list(dict.fromkeys(
                previous.recommendation_ids + entry.recommendation_ids
            ))
            previous.segment_ids = list(dict.fromkeys(
                previous.segment_ids + entry.segment_ids
            ))
            # The stronger reason wins, so a merged clip is described by its
            # best moment rather than by whichever came first.
            if _reason_rank(entry.keep_reason) > _reason_rank(previous.keep_reason):
                previous.keep_reason = entry.keep_reason
                previous.notes = entry.notes
            continue
        merged.append(entry)

    return [entry for entry in merged if entry.duration >= MIN_CLIP_SECONDS]


def _reason_rank(reason: str) -> float:
    if reason == "hold":
        return 1.0
    if reason == "audio_reaction":
        return 0.85
    if reason == "contrast":
        return 0.8
    return IMPORTANCE_WEIGHT.get(reason, 0.2)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def assemble(
    ranges: Sequence[SelectedRange], *, track: str = "V1"
) -> list[ClipPlacement]:
    """Lay selected ranges end to end and compute every sequence position.

    This is arithmetic, and the whole plan depends on it being exact: markers,
    punch-ins and review frames are all placed using the positions computed
    here, before Premiere has been touched. A clip sped up 2x occupies half its
    source duration, and everything after it moves accordingly.

    Order is the order given -- selection already sorted by file and time, so
    the cut plays through each file chronologically.
    """
    placements: list[ClipPlacement] = []
    cursor = 0.0

    for index, entry in enumerate(ranges):
        if entry.duration <= 0:
            continue
        placement = ClipPlacement(
            placement_id=placement_id_for(entry.asset_id, entry.start, entry.end),
            asset_id=entry.asset_id,
            source_file=entry.source_file,
            source_in=entry.start,
            source_out=entry.end,
            sequence_start=round(cursor, 3),
            track=track,
            index=len(placements),
            speed=entry.speed,
            keep_reason=entry.keep_reason,
            recommendation_ids=list(entry.recommendation_ids),
            segment_ids=list(entry.segment_ids),
            protected=entry.protected,
            notes=entry.notes,
        )
        placements.append(placement)
        cursor = round(cursor + placement.sequence_duration, 3)

    return placements


def map_to_sequence(
    placements: Sequence[ClipPlacement], asset_id: str, source_time: float
) -> Optional[float]:
    """Where a source timestamp ends up on the sequence, if it survived the cut.

    Returns None when that moment was cut out -- which is the honest answer, and
    the reason markers for removed footage are dropped rather than nudged to the
    nearest surviving frame.
    """
    for placement in placements:
        if placement.asset_id != asset_id:
            continue
        mapped = placement.source_to_sequence(source_time)
        if mapped is not None:
            return mapped
    return None


def coverage(placements: Sequence[ClipPlacement]) -> dict:
    """How much footage the cut uses, for the CLI and the report."""
    source = sum(p.source_duration for p in placements)
    cut = max((p.sequence_end for p in placements), default=0.0)
    return {
        "clips": len(placements),
        "source_seconds": round(source, 2),
        "cut_seconds": round(cut, 2),
        "compression": round(cut / source, 3) if source else 0.0,
        "protected_clips": sum(1 for p in placements if p.protected),
        "sped_clips": sum(1 for p in placements if p.speed != 1.0),
    }
