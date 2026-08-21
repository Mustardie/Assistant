"""Assembling what the episode is.

This is the module that produces ``EpisodeMemory``: objectives, places, people,
recurring motifs, and then the beats, setups, payoffs, callbacks and open loops
the other modules find. It is deliberately the only place that decides what the
episode *is about*, so there is one answer rather than four detectors each with
their own opinion.

Everything here is an observation. Nothing in this module says what to do --
that is the retention planner's job, and the split is what lets you disagree
with a suggestion without having to re-derive the story to do it.
"""
from __future__ import annotations

import time
from typing import Optional

from editing.episode import beats as beats_module
from editing.episode import language, loops as loops_module, track as track_module
from editing.episode.schema import (
    EpisodeCharacterRole, EpisodeEvidence, EpisodeLocation, EpisodeMemory,
    EpisodeMotif, EpisodeObjective, capped, new_id,
)
from editing.episode.track import EpisodeTrack
from editing.schema import StructureTimeline

#: An objective stated after this point in the episode is a secondary one. A
#: goal announced two thirds of the way through is not what the video is about.
MAIN_OBJECTIVE_BEFORE = 0.40

#: A thing has to happen this many times to be a motif.
MIN_OCCURRENCES = 2

#: Occurrences closer together than this are one event, not a recurrence.
MIN_MOTIF_GAP = 45.0

#: Vocative patterns that mean a name is a person in the room rather than a
#: person being talked about.
_ADDRESSED = ("hey ", "yo ", "okay ", "come on ", "look ", "wait ", "no ")


# ---------------------------------------------------------------------------
# Objectives
# ---------------------------------------------------------------------------

def find_objectives(
    track: EpisodeTrack, beats: list, loops: list
) -> tuple:
    """``(main, secondary)``. ``main`` is ``None`` when nothing states a goal.

    Returning ``None`` matters. An episode with no stated objective is a real
    and common thing -- and it is one of the retention risks this layer looks
    for -- so inventing one from whatever the player happened to be doing would
    destroy the finding.
    """
    candidates: list[EpisodeObjective] = []
    for loop in loops:
        line = loop.question
        if "objective" not in language.cue_hits(line) and \
                "plan" not in language.cue_hits(line):
            continue
        position = track.position(loop.start)
        objective = EpisodeObjective(
            item_id=new_id("objective", loop.item_id),
            start=loop.start,
            end=loop.end,
            text=loop.question,
            status="stated",
            topic=list(loop.topic),
            open_loop_id=loop.item_id,
            evidence=loop.evidence,
            confidence=loop.confidence,
            why=(
                "stated out loud in the first part of the episode"
                if position <= MAIN_OBJECTIVE_BEFORE else
                "stated out loud partway through"
            ),
            primary=position <= MAIN_OBJECTIVE_BEFORE,
        )
        _apply_outcome(objective, loop)
        objective.settle()
        candidates.append(objective)

    if not candidates:
        implied = _implied_objective(track, beats)
        return (implied, [])

    primary = [item for item in candidates if item.primary]
    main = (primary or candidates)[0]
    main.primary = True
    secondary = [item for item in candidates if item is not main]
    for item in secondary:
        item.primary = False
    return main, secondary


def _apply_outcome(objective: EpisodeObjective, loop) -> None:
    if loop.status == "resolved":
        objective.status = "achieved"
        objective.resolved_at = loop.resolved_at
    elif loop.status == "possibly_resolved":
        objective.status = "unresolved"
        objective.resolved_at = loop.resolved_at
        objective.notes = (
            "a later moment may have delivered this, but the link was too "
            "weak to call it achieved"
        )
    else:
        objective.status = "unresolved"


def _implied_objective(
    track: EpisodeTrack, beats: list
) -> Optional[EpisodeObjective]:
    """A goal nobody stated, read off what the episode actually spends time on.

    Only produced when one action dominates by a wide margin, and always marked
    ``implied`` with a low confidence -- it is a guess about intent from
    behaviour, which is exactly the kind of claim that should look uncertain.
    """
    totals: dict[str, float] = {}
    for slot in track.slots:
        action = slot.primary_action
        if action in ("unknown", "idle", "talking"):
            continue
        totals[action] = totals.get(action, 0.0) + slot.duration
    if not totals or track.duration <= 0:
        return None
    action, seconds = max(totals.items(), key=lambda pair: pair[1])
    share = seconds / track.duration
    if share < 0.45:
        return None

    evidence = EpisodeEvidence(
        segment_ids=[
            slot.segment_id for slot in track.slots
            if slot.primary_action == action
        ][:40],
        visual_event_ids=[
            event_id for slot in track.slots
            if slot.primary_action == action
            for event_id in slot.visual_event_ids
        ][:40],
        summary=f"{share:.0%} of the episode is {action}",
    )
    objective = EpisodeObjective(
        item_id=new_id("objective", "implied", action),
        start=0.0,
        end=track.duration,
        text=f"(never stated) the episode is mostly {action}",
        status="implied",
        topic=[action],
        evidence=evidence,
        confidence=capped(0.30, evidence.channels),
        why=(
            f"nobody states a goal, but {share:.0%} of the runtime is "
            f"{action}; this is inferred from behaviour, not from anything "
            "that was said"
        ),
        primary=True,
    )
    objective.needs_human_review = True
    objective.settle()
    return objective


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------

def find_locations(track: EpisodeTrack) -> list[EpisodeLocation]:
    """Every environment the episode is in, with how long and how often."""
    totals: dict[str, float] = {}
    visits: dict[str, list[float]] = {}
    spans: dict[str, tuple] = {}
    segments: dict[str, list[str]] = {}
    events: dict[str, list[str]] = {}
    previous = ""

    for slot in track.slots:
        place = slot.environment
        if place in ("", "unknown"):
            previous = place
            continue
        totals[place] = totals.get(place, 0.0) + slot.duration
        segments.setdefault(place, []).append(slot.segment_id)
        events.setdefault(place, []).extend(slot.visual_event_ids)
        if place != previous:
            visits.setdefault(place, []).append(slot.start)
        first, last = spans.get(place, (slot.start, slot.end))
        spans[place] = (min(first, slot.start), max(last, slot.end))
        previous = place

    if not totals:
        return []

    biggest = max(totals.values())
    out: list[EpisodeLocation] = []
    for place, seconds in sorted(totals.items(), key=lambda pair: -pair[1]):
        first, last = spans[place]
        starts = visits.get(place, [first])
        evidence = EpisodeEvidence(
            segment_ids=segments.get(place, [])[:60],
            visual_event_ids=events.get(place, [])[:60],
            summary=f"{seconds:.0f}s across {len(starts)} visit(s)",
        )
        location = EpisodeLocation(
            item_id=new_id("place", place),
            start=first,
            end=last,
            environment=place,
            total_seconds=seconds,
            visits=len(starts),
            visit_starts=[round(value, 3) for value in starts[:40]],
            primary=seconds >= biggest,
            evidence=evidence,
            confidence=capped(
                0.35 + min(0.35, seconds / max(1.0, track.duration)),
                evidence.channels),
            why=(
                f"{seconds / max(1.0, track.duration):.0%} of the episode, "
                f"across {len(starts)} visit(s)"
            ),
        )
        location.affects_edit = False
        location.settle()
        out.append(location)
    return out


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

def find_roles(track: EpisodeTrack) -> list[EpisodeCharacterRole]:
    """Names that keep coming up.

    This is the weakest detector in the layer and it is built to look weak.
    It reads capitalised words out of one channel, so every result caps below
    the edit threshold and arrives flagged for review. A name is a name because
    a person says so, and nothing here has asked one.
    """
    mentions: dict[str, list[float]] = {}
    addressed: set = set()
    segments: dict[str, list[str]] = {}
    quotes: dict[str, list[str]] = {}

    for slot in track.slots:
        for line in slot.quotes(limit=4):
            lowered = language.normalise(line)
            for name in language.candidate_names(line):
                mentions.setdefault(name, []).append(slot.start)
                segments.setdefault(name, []).append(slot.segment_id)
                if len(quotes.setdefault(name, [])) < 3:
                    quotes[name].append(line)
                token = name.lower()
                if any(
                    lowered.startswith(pattern + token)
                    or f" {pattern}{token}" in lowered
                    for pattern in _ADDRESSED
                ):
                    addressed.add(name)

    out: list[EpisodeCharacterRole] = []
    for name, times in sorted(mentions.items(), key=lambda pair: -len(pair[1])):
        if len(times) < MIN_OCCURRENCES:
            continue
        evidence = EpisodeEvidence(
            segment_ids=segments[name][:40],
            quotes=quotes.get(name, [])[:3],
            summary=f"named {len(times)} time(s)",
        )
        role = EpisodeCharacterRole(
            item_id=new_id("role", name),
            start=min(times),
            end=max(times),
            name=name,
            role="co_op" if name in addressed else "mentioned",
            mentions=len(times),
            mention_times=[round(value, 3) for value in times[:40]],
            evidence=evidence,
            confidence=capped(0.28 + min(0.12, 0.03 * len(times)),
                              evidence.channels),
            why=(
                f"'{name}' is spoken to directly" if name in addressed
                else f"'{name}' is named {len(times)} times"
            ),
        )
        role.needs_human_review = True
        role.settle()
        out.append(role)
    return out[:12]


# ---------------------------------------------------------------------------
# Motifs
# ---------------------------------------------------------------------------

def find_motifs(track: EpisodeTrack, beats: list) -> list[EpisodeMotif]:
    """Everything that happens more than once: jokes, failures, dangers, items."""
    out: list[EpisodeMotif] = []
    out.extend(_beat_motifs(track, beats, "joke", "joke"))
    out.extend(_beat_motifs(track, beats, "failure", "failure"))
    out.extend(_threat_motifs(track))
    out.extend(_item_motifs(track))
    out.extend(_phrase_motifs(track))
    out.sort(key=lambda motif: (-motif.occurrences, motif.start))
    return out[:20]


def _spread_out(times: list) -> list:
    """Drop occurrences that are really the same event continuing."""
    kept: list[float] = []
    for when in sorted(times):
        if not kept or when - kept[-1] >= MIN_MOTIF_GAP:
            kept.append(when)
    return kept


def _beat_motifs(
    track: EpisodeTrack, beats: list, kind: str, label: str
) -> list[EpisodeMotif]:
    matching = [beat for beat in beats if beat.kind == kind]
    times = _spread_out([beat.start for beat in matching])
    if len(times) < MIN_OCCURRENCES:
        return []
    evidence = EpisodeEvidence()
    for beat in matching:
        evidence = evidence.merged(beat.evidence)
    motif = EpisodeMotif(
        item_id=new_id("motif", kind),
        start=min(times),
        end=max(beat.end for beat in matching),
        label=f"repeated {label}",
        kind=kind,
        occurrences=len(times),
        occurrence_times=[round(value, 3) for value in times],
        evidence=evidence,
        confidence=capped(
            max((beat.confidence for beat in matching), default=0.0) + 0.05,
            evidence.channels),
        why=f"{len(times)} separate {label} beats across the episode",
    )
    motif.settle()
    return [motif]


def _threat_motifs(track: EpisodeTrack) -> list[EpisodeMotif]:
    seen: dict[str, list] = {}
    for slot in track.slots:
        for threat in slot.threats:
            seen.setdefault(threat.lower(), []).append(slot)
    out: list[EpisodeMotif] = []
    for threat, slots in seen.items():
        times = _spread_out([slot.start for slot in slots])
        if len(times) < MIN_OCCURRENCES:
            continue
        evidence = EpisodeEvidence(
            segment_ids=[slot.segment_id for slot in slots][:40],
            visual_event_ids=[
                event_id for slot in slots for event_id in slot.visual_event_ids
            ][:40],
        )
        motif = EpisodeMotif(
            item_id=new_id("motif", "danger", threat),
            start=min(times),
            end=max(slot.end for slot in slots),
            label=threat,
            kind="danger",
            occurrences=len(times),
            occurrence_times=[round(value, 3) for value in times],
            evidence=evidence,
            confidence=capped(0.35 + min(0.2, 0.05 * len(times)),
                              evidence.channels),
            why=f"'{threat}' threatens the run {len(times)} separate times",
        )
        motif.settle()
        out.append(motif)
    return out


def _item_motifs(track: EpisodeTrack) -> list[EpisodeMotif]:
    seen: dict[str, list] = {}
    for slot in track.slots:
        for entity in slot.entities:
            token = entity.lower()
            if token not in language.SALIENT:
                continue
            seen.setdefault(token, []).append(slot)
    out: list[EpisodeMotif] = []
    for item, slots in seen.items():
        times = _spread_out([slot.start for slot in slots])
        if len(times) < MIN_OCCURRENCES:
            continue
        evidence = EpisodeEvidence(
            segment_ids=[slot.segment_id for slot in slots][:40],
            visual_event_ids=[
                event_id for slot in slots for event_id in slot.visual_event_ids
            ][:40],
        )
        motif = EpisodeMotif(
            item_id=new_id("motif", "item", item),
            start=min(times),
            end=max(slot.end for slot in slots),
            label=item,
            kind="item",
            occurrences=len(times),
            occurrence_times=[round(value, 3) for value in times],
            evidence=evidence,
            confidence=capped(0.32 + min(0.2, 0.05 * len(times)),
                              evidence.channels),
            why=f"'{item}' matters at {len(times)} separate points",
        )
        motif.settle()
        out.append(motif)
    return out


def _phrase_motifs(track: EpisodeTrack) -> list[EpisodeMotif]:
    pairs: list[tuple] = []
    for slot in track.slots:
        for line in slot.quotes(limit=3):
            pairs.append((slot, line))
    out: list[EpisodeMotif] = []
    for phrase, count in language.repeated_phrases(
        [line for _, line in pairs]
    )[:6]:
        slots = [
            slot for slot, line in pairs
            if phrase in language.normalise(line)
        ]
        times = _spread_out([slot.start for slot in slots])
        if len(times) < MIN_OCCURRENCES:
            continue
        laughs = any(slot.has_reaction for slot in slots)
        evidence = EpisodeEvidence(
            segment_ids=[slot.segment_id for slot in slots][:40],
            quotes=[line for _, line in pairs if phrase in
                    language.normalise(line)][:3],
            audio_event_ids=[
                event_id for slot in slots
                for event_id in slot.audio_event_ids
            ][:40] if laughs else [],
        )
        motif = EpisodeMotif(
            item_id=new_id("motif", "phrase", phrase),
            start=min(times),
            end=max(slot.end for slot in slots),
            label=phrase,
            kind="joke" if laughs else "phrase",
            occurrences=len(times),
            occurrence_times=[round(value, 3) for value in times],
            evidence=evidence,
            confidence=capped(0.30 + min(0.15, 0.04 * count),
                              evidence.channels),
            why=(
                f"'{phrase}' recurs {count} times"
                + (" and lands on a reaction" if laughs else "")
            ),
        )
        motif.settle()
        out.append(motif)
    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build(
    timeline: StructureTimeline,
    *,
    roughcut=None,
    recommendations=None,
    layers=None,
    asset_plan=None,
    name: str = "structure",
) -> EpisodeMemory:
    """Everything this layer can say about what the episode is.

    Every optional argument is genuinely optional and its absence is recorded
    in ``sources`` rather than worked around silently. A memory built without a
    transcript is a different claim from one built with, and a report that
    cannot say which is a report you cannot act on.
    """
    episode = track_module.build(timeline, roughcut)
    memory = EpisodeMemory(
        episode_id=new_id(
            "ep", name, episode.sequence_name, round(episode.duration, 1)),
        name=name,
        sequence_name=episode.sequence_name,
        timebase=episode.timebase,
        duration=episode.duration,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        warnings=list(episode.warnings),
    )
    memory.sources = {
        "timeline": True,
        "roughcut": roughcut is not None and bool(
            getattr(roughcut, "placements", None)),
        "recommendations": recommendations is not None,
        "layers": layers is not None,
        "asset_plan": asset_plan is not None,
        "transcript": episode.has_speech,
        "audio_events": episode.has_audio,
        "visual_events": episode.has_visual,
        "motion_probed": episode.has_motion,
        "segments": len(episode),
    }

    if episode.is_empty:
        memory.warnings.append(
            "the timeline has no usable segments, so there is nothing to "
            "read an episode off; every list below is empty on purpose"
        )
        return memory

    if not episode.has_speech:
        memory.warnings.append(
            "no transcript: objectives, open loops and callbacks all lean on "
            "what was said, so most of them will be missing rather than absent"
        )
    if not episode.has_visual:
        memory.warnings.append(
            "no visual events: every finding here is capped at one channel "
            "and none of them can affect an edit"
        )
    if not episode.has_motion:
        memory.warnings.append(
            "motion was not probed, so the low-visual-change detector will "
            "stay quiet rather than fire on the whole episode"
        )

    memory.beats = beats_module.detect(
        episode, recommendations=recommendations, layers=layers)
    memory.open_loops = loops_module.resolve_loops(
        loops_module.find_open_loops(episode), episode, memory.beats)
    main, secondary = find_objectives(episode, memory.beats, memory.open_loops)
    memory.main_objective = main
    memory.secondary_objectives = secondary
    memory.locations = find_locations(episode)
    memory.roles = find_roles(episode)
    memory.motifs = find_motifs(episode, memory.beats)
    memory.setups = loops_module.find_setups(episode, memory.beats)
    memory.payoffs = loops_module.find_payoffs(
        episode, memory.beats, memory.setups)
    memory.callbacks = loops_module.find_callbacks(
        episode, memory.beats, memory.motifs)
    memory.interest_curve = episode.interest_curve()
    memory.retention_spikes = episode.spikes()

    if memory.main_objective is None:
        memory.warnings.append(
            "no objective was stated or implied; the retention planner will "
            "flag this, and it is usually the single most fixable thing"
        )
    return memory


def rebuild_track(
    timeline: StructureTimeline, roughcut=None
) -> EpisodeTrack:
    """The episode clock on its own, for callers that need slots not findings."""
    return track_module.build(timeline, roughcut)
