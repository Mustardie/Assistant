"""One linear episode, assembled from whatever exists.

Everything above this module reasons about "20 seconds in" and "the last
quarter". Nothing below it can answer that, because a ``StructureTimeline`` is
a set of source files with their own clocks and a ``RoughCutPlan`` is a set of
ranges on a sequence. This module turns either into one ordered list of slots
with a single clock, and records **which** clock it used.

That record is the important part. There are two timebases and they are not
interchangeable:

``roughcut``
    Sequence time on the scratch sequence, built from the placements. A later
    pass can hand these numbers straight to Premiere.

``timeline``
    A synthetic ordering: every segment of every asset, assets in discovery
    order, laid end to end. Useful for reasoning about story before a cut
    exists, and **wrong** to send to Premiere, because no sequence looks like
    this. A consumer has to go back through ``segment_ids``.

Conflating them would put captions in the wrong places on a real edit, so the
timebase travels on the memory and on the plan rather than being inferred.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from editing.schema import (
    IMPORTANCE_WEIGHT, StructureTimeline, TimelineSegment,
)

#: Audio types that mean a person reacted to something.
REACTION_AUDIO = frozenset({
    "sudden_reaction", "possible_laughter", "possible_scream",
    "loudness_spike",
})

#: Audio types that mean nothing is happening.
QUIET_AUDIO = frozenset({"silence", "long_pause", "low_energy",
                         "speech_sparse"})


@dataclass
class EpisodeSlot:
    """One segment of footage, placed on the episode clock.

    ``start``/``end`` are episode time. ``source_start``/``source_end`` are
    where it came from, which is what a later pass needs to find the frame.
    """

    index: int
    start: float
    end: float
    segment: TimelineSegment
    source_start: float
    source_end: float
    placement_id: str = ""
    #: True when the rough cut retimed this range. A grind detector that does
    #: not know a clip is already at 4x would recommend speeding it up again.
    speed: float = 1.0
    protected: bool = False

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def overlaps(self, start: float, end: float) -> float:
        return max(0.0, min(self.end, end) - max(self.start, start))

    @property
    def segment_id(self) -> str:
        return self.segment.segment_id

    @property
    def said(self) -> str:
        return self.segment.said

    @property
    def importance(self) -> str:
        return self.segment.importance

    @property
    def events(self) -> list:
        return list(self.segment.events)

    @property
    def audio_events(self) -> list:
        return list(self.segment.audio_events)

    @property
    def audio_types(self) -> set:
        return self.segment.audio_types()

    @property
    def environment(self) -> str:
        for event in self.segment.events:
            if event.environment != "unknown":
                return event.environment
        return "unknown"

    @property
    def actions(self) -> list[str]:
        out: list[str] = []
        for event in self.segment.events:
            for action in event.actions:
                if action not in out:
                    out.append(action)
        return out

    @property
    def primary_action(self) -> str:
        return self.actions[0] if self.actions else "unknown"

    @property
    def entities(self) -> list[str]:
        out: list[str] = []
        for event in self.segment.events:
            for entity in event.entities:
                if entity not in out:
                    out.append(entity)
        return out

    @property
    def threats(self) -> list[str]:
        out: list[str] = []
        for event in self.segment.events:
            for threat in event.threats:
                if threat not in out:
                    out.append(threat)
        return out

    @property
    def motion(self) -> float:
        """The strongest motion score any vision window here reported.

        ``0.0`` means either "nothing moved" or "motion probing was off", and
        this cannot tell the two apart. Callers that care use
        ``EpisodeTrack.has_motion`` before reading it.
        """
        return max((event.motion_score for event in self.segment.events),
                   default=0.0)

    @property
    def has_reaction(self) -> bool:
        return bool(self.audio_types & REACTION_AUDIO)

    @property
    def is_quiet(self) -> bool:
        return self.segment.is_dead_air or (
            not self.segment.has_speech and bool(self.audio_types & QUIET_AUDIO)
        )

    @property
    def visual_event_ids(self) -> list[str]:
        return [event.event_id for event in self.segment.events]

    @property
    def audio_event_ids(self) -> list[str]:
        return [event.event_id for event in self.segment.audio_events]

    def quotes(self, limit: int = 3) -> list[str]:
        lines = [
            entry.text.strip() for entry in self.segment.speech_entries
            if entry.text.strip()
        ]
        if not lines and self.segment.said.strip():
            lines = [self.segment.said.strip()]
        return lines[:limit]

    @property
    def interest(self) -> float:
        """0..1 measured interest in this slot.

        Importance weight is the spine; a reaction and real camera motion each
        add to it. This is a measurement of the *evidence*, not a prediction
        about a viewer, and it is used only to rank moments against each other
        inside one episode.
        """
        # Scaled to 0.85 so the bonuses below have somewhere to go. At full
        # weight a payoff already sits at 1.00 and a danger with a threat, a
        # reaction and real motion clips there too -- which made every strong
        # moment in an episode score identically, and left the climax selector
        # unable to tell a peak from a plateau.
        base = IMPORTANCE_WEIGHT.get(self.importance, 0.3) * 0.85
        bonus = 0.0
        if self.has_reaction:
            bonus += 0.12
        if self.threats:
            bonus += 0.08
        if self.motion >= 0.5:
            bonus += 0.06
        if self.is_quiet:
            bonus -= 0.15
        return max(0.0, min(1.0, base + bonus))

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "segment_id": self.segment_id,
            "source_start": round(self.source_start, 3),
            "source_end": round(self.source_end, 3),
            "placement_id": self.placement_id,
            "speed": round(self.speed, 3),
            "protected": self.protected,
            "environment": self.environment,
            "importance": self.importance,
            "interest": round(self.interest, 3),
        }


@dataclass
class EpisodeTrack:
    """The episode as one ordered clock, plus what it was built from."""

    slots: list[EpisodeSlot] = field(default_factory=list)
    timebase: str = "empty"
    sequence_name: str = ""
    duration: float = 0.0
    #: Whether motion probing was on for the analysis behind this. Without it,
    #: every ``motion`` is 0.0 and the low-visual-change detector would fire on
    #: the whole episode -- so it checks this first and stays quiet instead.
    has_motion: bool = False
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.slots)

    def __iter__(self):
        return iter(self.slots)

    @property
    def is_empty(self) -> bool:
        return not self.slots

    @property
    def has_speech(self) -> bool:
        return any(slot.segment.has_speech for slot in self.slots)

    @property
    def has_audio(self) -> bool:
        return any(slot.audio_events for slot in self.slots)

    @property
    def has_visual(self) -> bool:
        return any(slot.events for slot in self.slots)

    def position(self, when: float) -> float:
        return max(0.0, min(1.0, when / self.duration)) if self.duration else 0.0

    def slot_at(self, when: float) -> Optional[EpisodeSlot]:
        for slot in self.slots:
            if slot.start <= when < slot.end:
                return slot
        return self.slots[-1] if self.slots and when >= self.duration else None

    def between(self, start: float, end: float) -> list[EpisodeSlot]:
        return [slot for slot in self.slots if slot.overlaps(start, end) > 0] \
            if hasattr(EpisodeSlot, "overlaps") else [
                slot for slot in self.slots
                if max(0.0, min(slot.end, end) - max(slot.start, start)) > 0
            ]

    def quotes_between(self, start: float, end: float, limit: int = 6) -> list[str]:
        out: list[str] = []
        for slot in self.between(start, end):
            for line in slot.quotes():
                if line not in out:
                    out.append(line)
        return out[:limit]

    def interest_curve(self) -> list[list]:
        """``[[episode_time, 0..1], ...]`` sampled at each slot's midpoint."""
        return [
            [round((slot.start + slot.end) / 2.0, 3), round(slot.interest, 4)]
            for slot in self.slots
        ]

    def spikes(self, *, floor: float = 0.7) -> list[float]:
        """Local maxima of the interest curve above ``floor``.

        A plateau reports its first point rather than every point in it, so a
        30-second fight is one spike instead of six.
        """
        out: list[float] = []
        previous = 0.0
        for slot in self.slots:
            value = slot.interest
            if value >= floor and value > previous:
                out.append(round((slot.start + slot.end) / 2.0, 3))
            previous = value
        return out

    def to_dict(self) -> dict:
        return {
            "timebase": self.timebase,
            "sequence_name": self.sequence_name,
            "duration": round(self.duration, 3),
            "slots": len(self.slots),
            "has_motion": self.has_motion,
            "has_speech": self.has_speech,
            "has_audio": self.has_audio,
            "has_visual": self.has_visual,
            "warnings": list(self.warnings),
        }


def _segments_in(
    timeline: StructureTimeline, asset_id: str, start: float, end: float
) -> list[TimelineSegment]:
    """Segments of one asset overlapping ``[start, end)``, in order."""
    hits = [
        segment for segment in timeline.segments
        if segment.asset_id == asset_id
        and max(0.0, min(segment.end, end) - max(segment.start, start)) > 0
    ]
    return sorted(hits, key=lambda segment: segment.start)


def _clipped(
    segment: TimelineSegment, start: float, end: float
) -> TimelineSegment:
    """A copy of ``segment`` narrowed to ``[start, end)``.

    The events are kept whole rather than re-cut: a vision window that
    overlaps the kept range described the footage in it, and dropping it
    because its edges fall outside would throw away the only description this
    range has. The *range* is clipped; the evidence is not re-derived.
    """
    if segment.start >= start and segment.end <= end:
        return segment
    narrowed = TimelineSegment(
        segment_id=segment.segment_id,
        asset_id=segment.asset_id,
        source_file=segment.source_file,
        start=max(segment.start, start),
        end=min(segment.end, end),
        said=segment.said,
        speech_entries=[
            entry for entry in segment.speech_entries
            if entry.overlaps(start, end) > 0
        ],
        events=list(segment.events),
        audio_events=[
            event for event in segment.audio_events
            if event.overlaps(start, end) > 0
        ],
        alignment=segment.alignment,
        alignment_reason=segment.alignment_reason,
        usefulness=segment.usefulness,
        usable=segment.usable,
        reasons=list(segment.reasons),
    )
    narrowed.said = " ".join(
        entry.text.strip() for entry in narrowed.speech_entries
        if entry.text.strip()
    ) or (segment.said if not segment.speech_entries else "")
    return narrowed


def _motion_was_probed(timeline: StructureTimeline) -> bool:
    """Whether the analysis behind this timeline measured motion at all.

    ``sampling`` records it when the pipeline wrote the timeline; a non-zero
    score anywhere proves it regardless. Both are checked because a hand-built
    timeline in a test has no sampling block and should still work.
    """
    sampling = timeline.sampling or {}
    if sampling.get("use_motion") is not None:
        return bool(sampling.get("use_motion"))
    return any(
        event.motion_score > 0.0
        for segment in timeline.segments for event in segment.events
    )


def from_roughcut(
    timeline: StructureTimeline, roughcut
) -> EpisodeTrack:
    """The episode as the rough cut actually assembles it.

    Placements are walked in sequence order, and each contributes the segments
    its source range covers. A placement whose range matches no segment still
    contributes a slot -- built from an empty segment -- because the footage is
    on the timeline whether or not anything described it, and silently skipping
    it would shorten the episode clock and shift everything after it.
    """
    track = EpisodeTrack(
        timebase="roughcut",
        sequence_name=roughcut.sequence_name,
        has_motion=_motion_was_probed(timeline),
    )
    ordered = sorted(
        roughcut.placements, key=lambda p: (p.sequence_start, p.index)
    )
    index = 0
    for placement in ordered:
        covered = _segments_in(
            timeline, placement.asset_id, placement.source_in,
            placement.source_out,
        )
        if not covered:
            track.warnings.append(
                f"placement {placement.placement_id} covers no analysed "
                f"segment; its {placement.sequence_duration:.1f}s is on the "
                "episode clock but carries no evidence"
            )
            track.slots.append(EpisodeSlot(
                index=index,
                start=placement.sequence_start,
                end=placement.sequence_end,
                segment=TimelineSegment(
                    segment_id=f"{placement.placement_id}_gap",
                    asset_id=placement.asset_id,
                    source_file=placement.source_file,
                    start=placement.source_in,
                    end=placement.source_out,
                ),
                source_start=placement.source_in,
                source_end=placement.source_out,
                placement_id=placement.placement_id,
                speed=placement.speed,
                protected=placement.protected,
            ))
            index += 1
            continue

        for segment in covered:
            source_start = max(segment.start, placement.source_in)
            source_end = min(segment.end, placement.source_out)
            if source_end <= source_start:
                continue
            episode_start = placement.source_to_sequence(source_start)
            episode_end = placement.source_to_sequence(source_end)
            if episode_start is None or episode_end is None:
                continue
            track.slots.append(EpisodeSlot(
                index=index,
                start=episode_start,
                end=episode_end,
                segment=_clipped(segment, source_start, source_end),
                source_start=source_start,
                source_end=source_end,
                placement_id=placement.placement_id,
                speed=placement.speed,
                protected=placement.protected,
            ))
            index += 1

    track.slots.sort(key=lambda slot: slot.start)
    for position, slot in enumerate(track.slots):
        slot.index = position
    track.duration = max(
        (slot.end for slot in track.slots),
        default=roughcut.total_duration,
    )
    return track


def from_timeline(timeline: StructureTimeline) -> EpisodeTrack:
    """The episode as the raw footage, before any cut exists.

    Assets in discovery order, each asset's segments in source order, laid end
    to end. This is a *synthetic* clock: it is useful for reasoning about story
    and must never be handed to Premiere. ``timebase`` says so.
    """
    track = EpisodeTrack(
        timebase="timeline", has_motion=_motion_was_probed(timeline)
    )
    order = [asset.asset_id for asset in timeline.assets]
    for segment in timeline.segments:
        if segment.asset_id not in order:
            order.append(segment.asset_id)

    cursor = 0.0
    index = 0
    for asset_id in order:
        for segment in sorted(
            timeline.segments_for(asset_id), key=lambda s: s.start
        ):
            length = segment.duration
            if length <= 0:
                continue
            track.slots.append(EpisodeSlot(
                index=index,
                start=cursor,
                end=cursor + length,
                segment=segment,
                source_start=segment.start,
                source_end=segment.end,
            ))
            cursor += length
            index += 1

    track.duration = cursor
    if len(order) > 1:
        track.warnings.append(
            f"no rough cut: {len(order)} files were laid end to end in "
            "discovery order, so these times are a synthetic ordering rather "
            "than a real sequence"
        )
    return track


def build(timeline: StructureTimeline, roughcut=None) -> EpisodeTrack:
    """The episode clock, from the rough cut when there is one.

    Falls back to the raw timeline rather than refusing, because reasoning
    about story before a cut exists is a legitimate thing to want -- it just
    has to be labelled, and it is.
    """
    if roughcut is not None and getattr(roughcut, "placements", None):
        return from_roughcut(timeline, roughcut)
    track = from_timeline(timeline)
    if roughcut is not None:
        track.warnings.append(
            "a rough cut was supplied but has no placements; fell back to the "
            "raw timeline ordering"
        )
    return track
