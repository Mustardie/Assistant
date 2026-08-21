"""The audio placeholder layer.

No music exists on this timeline. No sound library is wired up. That is the
whole constraint, and pretending otherwise would be the easiest lie in the
session — emitting ``audio.duck`` operations against a bed clip that is not
there produces a plan that dry-runs clean and fails the moment it touches
Premiere.

So this layer is honest about a sharp division:

**Marker-only, because the asset does not exist.** Music starts and rises,
tension beds, impact and comedic SFX, whooshes, ambience, beat anchors,
narration ducking. Each becomes a marker carrying the type, the intensity, the
reason and the evidence — everything a person or a later pass needs to place
the real thing. A marker is not a consolation prize here; it is the correct
output for "something belongs at 4:12 and nobody has chosen what".

**Genuinely convertible, because the clip is already there.** Fading the first
clip up and the last clip down. ``audio.fade`` writes level keyframes on a clip
that exists, it is reversible, and it is the one audio operation this system
can perform truthfully today. So it does — when the style allows audio
operations at all.

The rest of the design is placement. A music cue in the wrong place is worse
than none, so cues are anchored to things the timeline already knows: the start
of the cut, the boundary into a tense stretch, the moment after a payoff, a
long silence. Nothing is placed on a grid.
"""
from __future__ import annotations

from typing import Optional, Sequence

from editing.roughcut.schema import ClipPlacement, RoughCutPlan
from editing.roughcut.select import map_to_sequence
from editing.schema import StructureTimeline, TimelineSegment
from editing.style.presets import StylePreset
from editing.style.schema import LayerEvidence, LayerItem, item_id_for

#: The one audio operation this layer can perform honestly today.
CONVERTIBLE_KINDS = frozenset({"audio_fade_in", "audio_fade_out"})

#: Everything else. Named here rather than inferred so the list is auditable.
PLACEHOLDER_KINDS = frozenset({
    "music_start", "music_rise", "tension_bed", "impact_sfx", "comedic_sfx",
    "whoosh", "silence_hold", "duck_narration", "beat_marker", "ambience",
})

#: Default fade length at the head and tail of the cut.
FADE_SECONDS = 1.0

#: A silence has to run at least this long before it is worth marking as a
#: deliberate hold rather than a gap between words.
MIN_SILENCE_HOLD = 2.0

#: Audio events that suggest an impact sound.
IMPACT_AUDIO = frozenset({"sudden_reaction", "loudness_spike", "possible_scream"})

#: Audio events that suggest a comedic sting.
COMEDY_AUDIO = frozenset({"possible_laughter"})

#: Importance transitions that earn a music cue, as ``(from, to)``.
RISE_INTO = frozenset({"tension", "danger"})


def build_audio(
    timeline: StructureTimeline,
    roughcut: RoughCutPlan,
    style: StylePreset,
    *,
    blocked_ranges: Sequence[tuple] = (),
) -> list[LayerItem]:
    """Audio placeholders and the two real fades, for this cut and style."""
    items: list[LayerItem] = []
    if not roughcut.placements:
        return items

    items.extend(_fades(roughcut, style))
    items.extend(_music(timeline, roughcut, style))
    items.extend(_moment_sounds(timeline, roughcut, style))
    items.extend(_silences(timeline, roughcut, style))
    items.extend(_narration(timeline, roughcut, style))

    items = [item for item in items if style.allows(item.kind)]
    items = [
        item for item in items
        if item.kind in CONVERTIBLE_KINDS or item.kind in style.audio_kinds
    ]
    items.sort(key=lambda item: item.start)
    return items


# ---------------------------------------------------------------------------
# The real operations
# ---------------------------------------------------------------------------

def _fades(roughcut: RoughCutPlan, style: StylePreset) -> list[LayerItem]:
    """Fade the cut up at the top and down at the end.

    The only audio edit here that acts on a clip that genuinely exists. When
    the style forbids audio operations it still gets marked, so the intent
    survives even in ``minimal_clean``.
    """
    items: list[LayerItem] = []
    first = roughcut.placements[0]
    last = max(roughcut.placements, key=lambda p: p.sequence_end)

    for kind, placement, at, reason, effect in (
        ("audio_fade_in", first, first.sequence_start,
         "the cut starts cold; a short fade up stops the first frame landing "
         "as a click", "clarity"),
        ("audio_fade_out", last, max(0.0, last.sequence_end - FADE_SECONDS),
         "the cut ends abruptly; a short fade down is what makes it read as "
         "an ending", "clarity"),
    ):
        if placement.sequence_duration < FADE_SECONDS * 2:
            continue
        item = LayerItem(
            item_id=item_id_for(kind, at, placement.placement_id),
            layer="audio",
            kind=kind,
            placement_id=placement.placement_id,
            start=at,
            end=at + FADE_SECONDS,
            asset_id=placement.asset_id,
            style=style.name,
            reason=reason,
            effect=effect,
            priority=0.8,
            evidence=LayerEvidence(
                segment_ids=list(placement.segment_ids),
                summary=f"the {'first' if kind.endswith('in') else 'last'} "
                        f"clip of the cut",
            ),
            payload={"seconds": FADE_SECONDS, "edge":
                     "in" if kind.endswith("in") else "out"},
        )
        if style.allow_audio_ops:
            key = "in" if kind.endswith("in") else "out"
            item.premiere_ops = [{
                "op": "audio.fade",
                "clip": placement.selector(),
                key: FADE_SECONDS,
                "easing": "ease_in_out",
                "note": f"{kind} [{style.name}] [{item.item_id}]",
            }]
        else:
            _as_marker(item, style,
                       f"the {style.name} style leaves audio to the editor")
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------

def _music(
    timeline: StructureTimeline, roughcut: RoughCutPlan, style: StylePreset
) -> list[LayerItem]:
    """Where music should start, rise, and sit under tension."""
    items: list[LayerItem] = []
    first = roughcut.placements[0]

    items.append(_placeholder(
        "music_start", first.sequence_start, style,
        reason="the top of the cut is where a bed normally comes in",
        effect="atmosphere", priority=0.75, placement=first,
        evidence=LayerEvidence(summary="the first clip of the cut"),
        intensity="low",
    ))

    previous: Optional[TimelineSegment] = None
    for segment in timeline.segments:
        if previous is not None and (
            segment.importance in RISE_INTO
            and previous.importance not in RISE_INTO
        ):
            at = map_to_sequence(
                roughcut.placements, segment.asset_id, segment.start
            )
            if at is not None:
                placement = roughcut.placement_at(at)
                kind = ("tension_bed" if segment.importance == "danger"
                        else "music_rise")
                items.append(_placeholder(
                    kind, at, style,
                    reason=f"the cut moves from {previous.importance} into "
                           f"{segment.importance} here",
                    effect="tension",
                    priority=round(0.55 + segment.usefulness * 0.3, 3),
                    placement=placement,
                    evidence=_segment_evidence(segment),
                    intensity="medium",
                    end=_segment_end(segment, roughcut) or at,
                ))
        previous = segment
    return items


def _moment_sounds(
    timeline: StructureTimeline, roughcut: RoughCutPlan, style: StylePreset
) -> list[LayerItem]:
    """Impact, comedy and whoosh placeholders, anchored to real audio events."""
    items: list[LayerItem] = []
    for segment in timeline.segments:
        for event in segment.audio_events:
            if event.type in IMPACT_AUDIO:
                kind, effect = "impact_sfx", "impact"
            elif event.type in COMEDY_AUDIO:
                kind, effect = "comedic_sfx", "comedy"
            else:
                continue
            at = map_to_sequence(
                roughcut.placements, segment.asset_id, event.start
            )
            if at is None:
                continue
            items.append(_placeholder(
                kind, at, style,
                reason=f"the audio pass found {event.type} here "
                       f"({event.confidence:.0%} confident, {event.detection})",
                effect=effect,
                # An inferred event is capped at 0.45 upstream; carrying its own
                # confidence through means a guess cannot outrank a measurement.
                priority=round(min(0.9, 0.35 + event.confidence * 0.5), 3),
                placement=roughcut.placement_at(at),
                evidence=LayerEvidence(
                    audio_event_ids=[event.event_id],
                    audio_types=[event.type],
                    segment_ids=[segment.segment_id],
                    summary=f"{event.type} at {event.start:.2f}s, "
                            f"{event.detection}",
                ),
                intensity="high" if event.confidence > 0.7 else "medium",
            ))

    # A whoosh belongs on a transition, not on a moment -- the boundary between
    # two clips whose content genuinely changes.
    for previous, following in zip(roughcut.placements, roughcut.placements[1:]):
        if previous.keep_reason == following.keep_reason:
            continue
        items.append(_placeholder(
            "whoosh", following.sequence_start, style,
            reason=f"the cut changes from {previous.keep_reason} to "
                   f"{following.keep_reason} here",
            effect="pacing", priority=0.45, placement=following,
            evidence=LayerEvidence(
                segment_ids=list(following.segment_ids),
                summary="a change of section on the timeline",
            ),
            intensity="low",
        ))
    return items


def _silences(
    timeline: StructureTimeline, roughcut: RoughCutPlan, style: StylePreset
) -> list[LayerItem]:
    """Long quiet stretches that survived the cut, marked as deliberate.

    A silence the rough cut chose to keep is either a mistake or a beat. Marking
    it makes the editor decide which, instead of leaving it ambiguous.
    """
    items: list[LayerItem] = []
    for segment in timeline.segments:
        for event in segment.audio_events:
            if event.type not in ("silence", "long_pause"):
                continue
            if event.duration < max(MIN_SILENCE_HOLD, style.dead_air_tolerance):
                continue
            at = map_to_sequence(
                roughcut.placements, segment.asset_id, event.start
            )
            if at is None:
                continue
            items.append(_placeholder(
                "silence_hold", at, style,
                reason=f"{event.duration:.1f}s of {event.type} survived the "
                       f"cut; this style tolerates "
                       f"{style.dead_air_tolerance:.1f}s",
                effect="pacing", priority=0.5,
                placement=roughcut.placement_at(at),
                evidence=LayerEvidence(
                    audio_event_ids=[event.event_id],
                    audio_types=[event.type],
                    segment_ids=[segment.segment_id],
                    summary=f"{event.type} of {event.duration:.1f}s",
                ),
                intensity="low",
                end=at + min(event.duration, 6.0),
            ))
    return items


def _narration(
    timeline: StructureTimeline, roughcut: RoughCutPlan, style: StylePreset
) -> list[LayerItem]:
    """One ducking placeholder covering the speech in the cut.

    ``audio.duck`` needs a music clip to duck, and there is none, so this is a
    marker carrying the speech ranges the operation would need. When a bed is
    added by hand, the ranges are already computed.
    """
    ranges: list[dict] = []
    for segment in timeline.segments:
        for entry in segment.speech_entries:
            start = map_to_sequence(
                roughcut.placements, segment.asset_id, entry.start
            )
            end = map_to_sequence(
                roughcut.placements, segment.asset_id, entry.end
            )
            if start is None:
                continue
            ranges.append({
                "start": round(start, 3),
                "end": round(end if end and end > start else start + 1.0, 3),
            })
    if not ranges:
        return []

    at = roughcut.placements[0].sequence_start
    item = _placeholder(
        "duck_narration", at, style,
        reason=f"{len(ranges)} speech range(s) in the cut would need the bed "
               "ducked under them",
        effect="clarity", priority=0.6, placement=roughcut.placements[0],
        evidence=LayerEvidence(
            summary=f"{len(ranges)} speech ranges, in sequence time",
        ),
        intensity="medium",
        end=max(entry["end"] for entry in ranges),
    )
    item.payload["under"] = ranges[:200]
    item.payload["note"] = (
        "audio.duck needs a music/bed clip on the timeline. None exists, so "
        "these ranges are recorded rather than applied."
    )
    if "not_convertible" not in item.risks:
        item.risks.append("not_convertible")
    return [item]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _placeholder(
    kind: str,
    at: float,
    style: StylePreset,
    *,
    reason: str,
    effect: str,
    priority: float,
    placement: Optional[ClipPlacement],
    evidence: LayerEvidence,
    intensity: str = "low",
    end: Optional[float] = None,
) -> LayerItem:
    item = LayerItem(
        item_id=item_id_for(kind, at, reason[:40]),
        layer="audio",
        kind=kind,
        placement_id=placement.placement_id if placement else "",
        start=at,
        end=end if end is not None and end > at else at,
        asset_id=placement.asset_id if placement else "",
        style=style.name,
        reason=reason,
        effect=effect,
        intensity=intensity,
        priority=max(0.0, min(1.0, priority)),
        evidence=evidence,
        payload={"placeholder": kind, "convertible": False},
    )
    _as_marker(item, style, "")
    return item


def _segment_evidence(segment: TimelineSegment) -> LayerEvidence:
    return LayerEvidence(
        visual_event_ids=[event.event_id for event in segment.events][:5],
        audio_event_ids=[event.event_id for event in segment.audio_events][:5],
        audio_types=sorted(segment.audio_types()),
        transcript_quotes=[segment.said[:200]] if segment.said else [],
        segment_ids=[segment.segment_id],
        summary=segment.summary()[:200],
    )


def _segment_end(
    segment: TimelineSegment, roughcut: RoughCutPlan
) -> Optional[float]:
    return map_to_sequence(roughcut.placements, segment.asset_id, segment.end)


def _as_marker(item: LayerItem, style: StylePreset, extra: str) -> None:
    """Every placeholder is a marker, and says why it is only a marker."""
    if "placeholder_only" not in item.risks:
        item.risks.append("placeholder_only")
    note = extra or (
        "no sound library is wired up, so this records where the sound goes "
        "rather than placing one"
    )
    item.notes = (item.notes + " | " if item.notes else "") + note
    op: dict = {
        "op": "marker.add",
        "time": round(item.start, 3),
        "name": style.marker_name(item.kind),
        "type": "comment",
        "comment": (
            f"{item.kind.replace('_', ' ').upper()} ({item.intensity}): "
            f"{item.reason} | {note} [{item.item_id}]"
        )[:500],
        "note": f"{item.kind} placeholder [{style.name}]",
    }
    if item.duration >= 0.25:
        op["duration"] = round(item.duration, 3)
    item.premiere_ops = [op]
