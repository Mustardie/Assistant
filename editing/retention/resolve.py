"""Turning "at 04:12 in the episode" into "this footage, in this file".

Every finding Session 8 produced is in *episode time*, and episode time is one
of two incompatible things:

``roughcut``   sequence time on the scratch sequence the cut would build
``timeline``   a synthetic ordering of every segment of every asset, laid end
               to end, that no sequence has ever looked like

The memory records which it used. This module is the only place that record is
read, and it is read rather than guessed -- acting on the wrong timebase would
put the cold open somewhere else entirely and nothing downstream could tell.

## How

``EpisodeTrack`` already holds the mapping: each slot carries both episode
time and the source range it came from. So resolution is a lookup, not
arithmetic:

    episode range -> overlapping slots -> the source sub-range of each

The sub-range matters. A finding covering the middle third of a slot resolves
to the middle third of *that slot's footage*, scaled by the slot's speed if the
rough cut retimed it -- otherwise a risk zone over a 4x grind clip would
resolve to a quarter of the footage it actually covers.

## What cannot be resolved

A finding whose episode range touches no slot resolves to nothing, and comes
back as an empty span list. That is not an error: it is what happens when the
memory was built from a different cut than the one being edited, and the honest
outcome is a rejected decision saying so rather than a guess.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.retention.schema import SourceSpan

logger = logging.getLogger("nova.editing.retention.resolve")

#: Spans shorter than this are dropped. A hundredth of a second of footage is
#: not a range, it is a rounding artefact from the slot arithmetic.
MIN_SPAN = 0.05

#: Spans on the same asset closer together than this are joined, so a finding
#: covering four consecutive slots resolves to one range rather than four
#: touching ones.
JOIN_GAP = 0.25


class Resolver:
    """Episode time to source footage, for one episode.

    Built once per retention pass from the same track the memory was built
    from. Holding it rather than passing the track around means every module
    resolves the same way, and the timebase is checked in one place.
    """

    def __init__(self, track):
        self.track = track
        self.timebase = getattr(track, "timebase", "empty")
        self.duration = float(getattr(track, "duration", 0.0) or 0.0)
        #: True when the clock this resolves against is not the clock the
        #: findings were measured on. Every answer is then suspect.
        self.mismatched = False

    # -- the main job ----------------------------------------------------

    def spans(self, start: float, end: float) -> list[SourceSpan]:
        """The source footage an episode range covers.

        Returns ``[]`` when the range touches nothing, which is the honest
        answer for a finding built against a different cut.
        """
        if end <= start:
            return []

        found: list[SourceSpan] = []
        for slot in getattr(self.track, "slots", ()):
            overlap = _overlap(slot.start, slot.end, start, end)
            if overlap <= 0:
                continue
            span = self._slot_span(slot, start, end)
            if span is not None and span.duration >= MIN_SPAN:
                found.append(span)
        return _join(found)

    def _slot_span(self, slot, start: float, end: float
                   ) -> Optional[SourceSpan]:
        """The part of one slot's source footage inside an episode range.

        The speed factor is the subtle part. When the rough cut placed a clip
        at 2x, one second of episode time is two seconds of footage -- so a
        risk zone over a sped-up grind has to resolve to the footage it really
        covers, not half of it.
        """
        segment = getattr(slot, "segment", None)
        if segment is None:
            return None

        slot_duration = max(0.0, slot.end - slot.start)
        if slot_duration <= 0:
            return None

        head = max(0.0, start - slot.start)
        tail = max(0.0, slot.end - end)
        rate = float(getattr(slot, "speed", 1.0) or 1.0)

        source_start = slot.source_start + head * rate
        source_end = slot.source_end - tail * rate
        if source_end <= source_start:
            return None

        # Never reach outside the footage the slot actually names.
        source_start = max(slot.source_start, source_start)
        source_end = min(slot.source_end, source_end)
        if source_end - source_start < MIN_SPAN:
            return None

        placement = getattr(slot, "placement_id", "") or ""
        return SourceSpan(
            asset_id=segment.asset_id,
            source_file=segment.source_file,
            start=round(source_start, 3),
            end=round(source_end, 3),
            segment_ids=[segment.segment_id],
            placement_ids=[placement] if placement else [],
        )

    # -- lookups the other modules need ----------------------------------

    def spans_for_segments(self, segment_ids: Sequence[str]
                           ) -> list[SourceSpan]:
        """Source footage for named segments.

        The fallback path when an episode range does not resolve: the finding
        still names the segments it was built from, and those are stable across
        timebases. Session 8's own advice for a ``timeline`` memory.
        """
        wanted = {str(item) for item in segment_ids if item}
        if not wanted:
            return []
        found: list[SourceSpan] = []
        for slot in getattr(self.track, "slots", ()):
            segment = getattr(slot, "segment", None)
            if segment is None or segment.segment_id not in wanted:
                continue
            placement = getattr(slot, "placement_id", "") or ""
            found.append(SourceSpan(
                asset_id=segment.asset_id,
                source_file=segment.source_file,
                start=round(slot.source_start, 3),
                end=round(slot.source_end, 3),
                segment_ids=[segment.segment_id],
                placement_ids=[placement] if placement else [],
            ))
        return _join(found)

    def resolve_item(self, item) -> list[SourceSpan]:
        """An episode-layer record, resolved by range and then by segment.

        Two attempts, in that order: the range is more precise, and the
        segment ids are more robust. A finding that resolves neither way is
        genuinely about footage this cut does not contain.
        """
        spans = self.spans(getattr(item, "start", 0.0),
                           getattr(item, "end", 0.0))
        if spans:
            return spans
        return self.spans_for_segments(getattr(item, "segment_ids", ()) or ())

    def slots_between(self, start: float, end: float) -> list:
        return [
            slot for slot in getattr(self.track, "slots", ())
            if _overlap(slot.start, slot.end, start, end) > 0
        ]

    def position(self, when: float) -> float:
        """Where a moment sits in the episode, 0..1."""
        return max(0.0, min(1.0, when / self.duration)) if self.duration \
            else 0.0

    def has_speech(self, start: float, end: float) -> bool:
        """Whether anybody is talking across an episode range.

        The single most consulted fact in this layer: speech is what makes a
        stretch un-speed-uppable, un-cuttable and usually not dead air.
        """
        for slot in self.slots_between(start, end):
            segment = getattr(slot, "segment", None)
            if segment is not None and segment.has_speech:
                return True
        return False

    def audio_types(self, start: float, end: float) -> set:
        found: set = set()
        for slot in self.slots_between(start, end):
            found |= set(getattr(slot, "audio_types", set()) or set())
        return found

    def actions(self, start: float, end: float) -> list[str]:
        out: list[str] = []
        for slot in self.slots_between(start, end):
            for action in getattr(slot, "actions", []) or []:
                if action not in out:
                    out.append(action)
        return out

    def importances(self, start: float, end: float) -> set:
        return {
            getattr(slot, "importance", "unknown")
            for slot in self.slots_between(start, end)
        }

    def is_protected(self, start: float, end: float) -> bool:
        """Whether the base cut already marked this footage untouchable."""
        return any(getattr(slot, "protected", False)
                   for slot in self.slots_between(start, end))


def build_resolver(timeline, roughcut=None, *, timebase: str = ""):
    """A resolver over the same clock the episode memory used.

    ``timebase`` comes from the memory or the retention plan. When it says
    ``timeline``, the rough cut is deliberately *not* used to build the track
    even if one exists -- a memory built without a cut has episode times that
    only make sense in that ordering, and mixing the two silently shifts every
    finding.

    The reverse case is worse and is why ``mismatched`` exists: a memory built
    *from* a rough cut, resolved without one, would have its sequence times
    read against a synthetic ordering. Every number would still be a number,
    every finding would still land somewhere, and all of them would be wrong.
    So the fallback happens -- there is nothing else to do -- and it is
    flagged, so the caller can refuse to act on it.
    """
    from editing.episode import track as track_module

    if timebase == "timeline":
        return Resolver(track_module.from_timeline(timeline))
    if timebase == "roughcut":
        if roughcut is not None:
            return Resolver(track_module.from_roughcut(timeline, roughcut))
        resolver = Resolver(track_module.build(timeline, None))
        resolver.mismatched = True
        return resolver
    return Resolver(track_module.build(timeline, roughcut))


# ---------------------------------------------------------------------------
# Span arithmetic
# ---------------------------------------------------------------------------

def _overlap(a_start: float, a_end: float, b_start: float, b_end: float
             ) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _join(spans: Sequence[SourceSpan]) -> list[SourceSpan]:
    """Merge touching spans on the same asset into one."""
    ordered = sorted(spans, key=lambda span: (span.asset_id, span.start))
    out: list[SourceSpan] = []
    for span in ordered:
        previous = out[-1] if out else None
        if (previous is not None
                and previous.asset_id == span.asset_id
                and span.start <= previous.end + JOIN_GAP):
            previous.end = max(previous.end, span.end)
            previous.segment_ids = list(dict.fromkeys(
                previous.segment_ids + span.segment_ids))
            previous.placement_ids = list(dict.fromkeys(
                previous.placement_ids + span.placement_ids))
            continue
        out.append(SourceSpan(
            asset_id=span.asset_id,
            source_file=span.source_file,
            start=span.start,
            end=span.end,
            segment_ids=list(span.segment_ids),
            placement_ids=list(span.placement_ids),
        ))
    return out


def covered_seconds(spans: Sequence[SourceSpan], asset_id: str,
                    start: float, end: float) -> float:
    """How much of a source range a set of spans covers."""
    return sum(span.covers(asset_id, start, end) for span in spans)


def any_overlap(spans: Sequence[SourceSpan], asset_id: str,
                start: float, end: float, *, tolerance: float = 0.05) -> bool:
    return covered_seconds(spans, asset_id, start, end) > tolerance


def total_seconds(spans: Sequence[SourceSpan]) -> float:
    return round(sum(span.duration for span in spans), 3)
