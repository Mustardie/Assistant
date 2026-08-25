"""Finding the moments that could earn visual emphasis.

Every moment here comes from something an earlier pass already recorded. There
is no detector in this module that looks at footage — the vision pass, the
audio pass, the director, the retention wiring and the caption pass have all
already looked, and this reads their conclusions.

That is a deliberate constraint rather than laziness. A twenty-first way to
decide "this is a reveal" would disagree with the twenty that already exist, and
the disagreement would surface as an arrow pointing at nothing. So: **no moment
without evidence, and the evidence is always somebody else's record.**

## Resolving where a moment is

Signals arrive in three different timebases and getting this wrong would put
every effect in the wrong place:

* **Caption decisions and audio cues** are already in sequence time. Used
  directly.
* **Director and retention decisions, and timeline segments** are in *source*
  time on a particular asset. Mapped through the cut's placements, and dropped
  when that footage is not in the cut.
* **Episode memory** carries a ``timebase`` field saying which of the two it
  used. When it says ``roughcut`` its numbers are sequence time; when it says
  anything else they are a synthetic ordering no sequence has ever seen, and
  the only honest thing to do is resolve through ``segment_ids`` or drop the
  finding. Session 10D learned this the expensive way.
"""
from __future__ import annotations

from typing import Optional, Sequence

from editing.roughcut.schema import RoughCutPlan
from editing.roughcut.select import map_to_sequence
from editing.schema import StructureTimeline, TimelineSegment
from editing.visuals.schema import (
    VisualMoment, moment_id_for,
)

#: Words that mark panic. Short and literal, like every other keyword list in
#: this system: a list whose mistakes are predictable beats a classifier whose
#: mistakes are not.
PANIC_WORDS = (
    "oh no", "oh god", "run", "go go go", "get out", "help", "i'm dying",
    "im dying", "no no no", "we're dead", "were dead", "get away",
)

#: Words that mark an argument or a spike of banter between two people.
BANTER_WORDS = (
    "what are you doing", "why did you", "that was you", "you idiot",
    "shut up", "are you serious", "i told you", "your fault", "stop it",
    "excuse me", "did you just",
)

#: Words that mark something worth finding.
FIND_WORDS = (
    "diamonds", "diamond", "ancient debris", "netherite", "emerald",
    "elytra", "totem", "enchanted golden apple", "shulker", "beacon",
    "found it", "look at this", "check this out",
)

#: Words that mark the end of a stretch of nothing.
CLIFFHANGER_WORDS = (
    "next time", "next episode", "to be continued", "we'll find out",
    "well find out", "tune in", "see you next",
)

#: Entities whose presence on screen makes a moment dangerous whatever else is
#: happening. Read off the vision pass's ``threats`` and ``entities``.
DANGER_ENTITIES = frozenset({
    "creeper", "warden", "wither", "enderman", "ravager", "evoker", "vindicator",
    "blaze", "ghast", "piglin_brute", "hoglin", "skeleton", "witch", "phantom",
})

#: Entities that mean a village situation is going wrong.
VILLAGER_ENTITIES = frozenset({
    "villager", "iron_golem", "zombie_villager", "pillager", "ravager",
    "witch", "evoker",
})

#: Retention actions that mark a stretch as compressed.
COMPRESSED_ACTIONS = frozenset({"speed_up", "shorten"})

#: Director actions that mark a stretch as worth keeping for a reason.
DIRECTOR_KEEP_ACTIONS = frozenset({"keep", "hold", "protect", "highlight"})

#: Caption moment -> visual moment. The caption pass has already decided a
#: line is one of nine things; re-deciding it here would be a second opinion
#: nobody asked for.
CAPTION_TO_MOMENT = {
    "death_or_fail": "death_or_fail",
    "funny_reaction": "funny_reaction",
    "reveal": "reveal",
    "payoff_line": "payoff",
    "callback": "callback",
    "danger": "danger",
    "objective": "objective_start",
    "transition_setup": "confusing_transition",
    "meme_quote": "funny_reaction",
}

#: Episode beat kind -> visual moment, for beats strong enough to matter.
BEAT_TO_MOMENT = {
    "failure": "death_or_fail",
    "danger": "danger",
    "escalation": "near_death",
    "discovery": "discovery",
    "reveal": "reveal",
    "payoff": "payoff",
    "callback": "callback",
    "climax": "payoff",
    "joke": "funny_reaction",
    "objective_stated": "objective_start",
    "grind": "grind_montage",
    "travel": "grind_montage",
    "resolution": "objective_complete",
    "outro": "cliffhanger",
}

#: A moment shorter than this is a frame, not a moment.
MIN_MOMENT_SECONDS = 0.4

#: Confidence a beat needs before it becomes a moment at all. Below this the
#: episode layer is guessing, and an effect built on a guess is worse than no
#: effect.
MIN_BEAT_CONFIDENCE = 0.4


def detect_moments(
    timeline: StructureTimeline,
    cut: RoughCutPlan,
    *,
    director_plan=None,
    retention_plan=None,
    caption_plan=None,
    audio_plan=None,
    memory=None,
    retention_findings=None,
) -> list[VisualMoment]:
    """Every moment the earlier passes justify, resolved onto the cut.

    Deduplicated by ``(kind, second)``: two layers noticing the same death is
    one moment with two pieces of evidence, not two moments that would each
    earn their own effect.
    """
    found: list[VisualMoment] = []
    context = _CutContext(timeline, cut)

    found.extend(_from_captions(caption_plan, context))
    found.extend(_from_director(director_plan, context))
    found.extend(_from_retention(retention_plan, context))
    found.extend(_from_hooks(retention_findings, retention_plan, context))
    found.extend(_from_audio(audio_plan, context))
    found.extend(_from_memory(memory, context))
    found.extend(_from_timeline(timeline, context))

    return _merge(found)


# ---------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------

class _CutContext:
    """Resolution of source time onto the cut, and what is on screen there.

    Built once per pass because every detector needs it and each of them
    would otherwise walk the timeline again.
    """

    def __init__(self, timeline: StructureTimeline, cut: RoughCutPlan):
        self.timeline = timeline
        self.cut = cut
        self.duration = cut.total_duration
        self._by_segment = {
            segment.segment_id: segment for segment in timeline.segments}

    # -- resolution ------------------------------------------------------

    def at(self, asset_id: str, source_time: float) -> Optional[float]:
        """Where a source timestamp lands on the cut, or None if it was cut."""
        if not asset_id:
            return None
        return map_to_sequence(self.cut.placements, asset_id, source_time)

    def range_at(self, asset_id: str, start: float, end: float) -> Optional[tuple]:
        """A source range on the cut, or None when its start was cut out."""
        low = self.at(asset_id, start)
        if low is None:
            return None
        high = self.at(asset_id, end)
        if high is None or high <= low:
            # The range runs past the end of its placement. Clamping to the
            # clip's own end is right: the moment is in the cut, and only its
            # tail is not.
            placement = self.cut.placement_at(low)
            high = placement.sequence_end if placement else low + 1.0
        return (low, max(low + MIN_MOMENT_SECONDS, high))

    def by_segment_ids(self, segment_ids: Sequence[str]) -> Optional[tuple]:
        """A range resolved through segment ids rather than through numbers.

        The only honest way to place an episode-layer finding whose timebase
        is not the cut's. Session 10D's rule, applied here.
        """
        spans = []
        for segment_id in segment_ids or ():
            segment = self._by_segment.get(segment_id)
            if segment is None:
                continue
            resolved = self.range_at(
                segment.asset_id, segment.start, segment.end)
            if resolved is not None:
                spans.append(resolved)
        if not spans:
            return None
        return (min(low for low, _ in spans), max(high for _, high in spans))

    def placement_id(self, at: float) -> str:
        placement = self.cut.placement_at(at)
        return placement.placement_id if placement else ""

    # -- what is on screen ------------------------------------------------

    def segments_over(self, start: float, end: float) -> list[TimelineSegment]:
        """Timeline segments whose footage plays in ``[start, end)``."""
        out: list[TimelineSegment] = []
        for segment in self.timeline.segments:
            resolved = self.range_at(
                segment.asset_id, segment.start, segment.end)
            if resolved is None:
                continue
            low, high = resolved
            if high > start and low < end:
                out.append(segment)
        return out

    def entities_at(self, start: float, end: float) -> list[str]:
        """Named things the vision pass saw there. Threats first."""
        threats: list[str] = []
        others: list[str] = []
        for segment in self.segments_over(start, end):
            for event in segment.events:
                threats.extend(event.threats)
                others.extend(event.entities)
        seen: set = set()
        out: list[str] = []
        for name in list(threats) + list(others):
            key = str(name).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(str(name).strip())
        return out[:12]

    def hud_at(self, start: float, end: float) -> dict:
        """HUD state over a range, as the union of what was seen.

        Conservative on purpose: if a menu was open in *any* frame of the
        range, the range counts as having a menu open. An effect that is only
        sometimes covering the inventory is still covering the inventory.
        """
        state = {
            "inventory_open": False, "crafting_open": False,
            "chest_open": False, "map_open": False, "death_screen": False,
            "chat_open": False, "low_health": False,
            "achievement_toast": False,
        }
        coordinates = ""
        for segment in self.segments_over(start, end):
            for event in segment.events:
                for key in state:
                    if getattr(event.ui, key, False):
                        state[key] = True
                if not coordinates and getattr(event.ui, "coordinates", ""):
                    coordinates = str(event.ui.coordinates)
        state["coordinates"] = coordinates
        return state

    def transcript_confidence_at(self, start: float, end: float) -> float:
        """Mean ASR confidence over a range, or -1 when nothing said so."""
        values: list[float] = []
        for segment in self.segments_over(start, end):
            for entry in segment.speech_entries:
                # 1.0 is ``TranscriptEntry``'s default and means "nobody
                # said", not "very sure" -- the same trap the caption pass
                # documents.
                if entry.confidence < 1.0:
                    values.append(entry.confidence)
        return round(sum(values) / len(values), 3) if values else -1.0

    def said_over(self, start: float, end: float) -> str:
        return " ".join(
            segment.said for segment in self.segments_over(start, end)
            if segment.said
        ).lower()


def _make(
    kind: str,
    context: _CutContext,
    start: float,
    end: float,
    *,
    source_type: str,
    source_id: str,
    confidence: float,
    importance: float,
    label: str,
    evidence: Sequence[str] = (),
    asset_id: str = "",
    segment_ids: Sequence[str] = (),
) -> VisualMoment:
    """One moment, with everything the safety pass will need already on it."""
    end = max(start + MIN_MOMENT_SECONDS, end)
    return VisualMoment(
        moment_id=moment_id_for(kind, start, source_id or source_type),
        kind=kind,
        source_type=source_type,
        source_id=source_id,
        start=round(start, 3),
        end=round(end, 3),
        placement_id=context.placement_id(start),
        segment_ids=list(segment_ids)[:20],
        asset_id=asset_id,
        confidence=max(0.0, min(1.0, confidence)),
        importance=max(0.0, min(1.0, importance)),
        label=label[:300],
        evidence=[str(item)[:300] for item in evidence][:12],
        entities=context.entities_at(start, end),
        hud=context.hud_at(start, end),
        transcript_confidence=context.transcript_confidence_at(start, end),
    )


# ---------------------------------------------------------------------------
# The detectors
# ---------------------------------------------------------------------------

def _from_captions(caption_plan, context: _CutContext) -> list[VisualMoment]:
    """Accepted captions. The strongest signal there is.

    A caption survived nine kinds of check to be there, it is already in
    sequence time, and it already carries the moment kind. Re-deriving any of
    that would be a second opinion nobody asked for.
    """
    if caption_plan is None:
        return []
    out: list[VisualMoment] = []
    for decision in getattr(caption_plan, "accepted", []) or []:
        kind = CAPTION_TO_MOMENT.get(decision.moment or "")
        if kind is None or decision.start < 0:
            continue
        out.append(_make(
            kind, context, decision.start, decision.end,
            source_type="polish",
            source_id=decision.caption_id,
            confidence=max(0.55, decision.priority),
            importance=decision.priority,
            label=decision.text or decision.full_line,
            evidence=[f'caption "{decision.text}"', decision.reason],
            asset_id=decision.asset_id,
            segment_ids=[decision.segment_id] if decision.segment_id else (),
        ))
    return out


def _from_director(director_plan, context: _CutContext) -> list[VisualMoment]:
    """Decisions the director made and the rules accepted.

    Only accepted ones. A refused decision is a thing the safety layer already
    said no to, and building an effect on it would route around that refusal.
    """
    if director_plan is None:
        return []
    out: list[VisualMoment] = []
    for decision in getattr(director_plan, "decisions", []) or []:
        if not getattr(decision, "accepted", False):
            continue
        if decision.action not in DIRECTOR_KEEP_ACTIONS:
            continue
        if decision.priority < 0.6 and decision.confidence < 0.6:
            continue

        resolved = context.range_at(
            decision.asset_id, decision.out_start or decision.start,
            decision.out_end or decision.end)
        if resolved is None:
            continue
        start, end = resolved

        effect = str(getattr(decision, "viewer_effect", "") or "")
        kind = {
            "raises_tension": "danger",
            "releases_tension": "payoff",
            "lands_a_joke": "funny_reaction",
            "answers_a_question": "reveal",
            "opens_a_question": "cliffhanger",
            "protects_a_payoff": "payoff",
            "restates_the_goal": "objective_start",
            "closes_the_episode": "objective_complete",
        }.get(effect, "")
        if not kind:
            continue

        reason = getattr(decision.reason, "text", "") or getattr(
            decision.reason, "category", "")
        out.append(_make(
            kind, context, start, end,
            source_type="director",
            source_id=decision.decision_id,
            confidence=max(decision.confidence, 0.55),
            importance=decision.priority,
            label=str(reason)[:200],
            evidence=[f"director {decision.action}: {reason}"[:280]]
            + list(decision.evidence)[:3],
            asset_id=decision.asset_id,
            segment_ids=decision.segment_ids,
        ))
    return out


def _from_retention(retention_plan, context: _CutContext) -> list[VisualMoment]:
    """The cold open, and the stretches the retention pass compressed."""
    if retention_plan is None:
        return []
    out: list[VisualMoment] = []

    cold = getattr(retention_plan, "cold_open", None)
    if cold is not None and getattr(cold, "chosen", False):
        # A cold open is at the front of the cut by construction. Its own
        # numbers describe where the footage came *from*, not where it is now.
        duration = max(MIN_MOMENT_SECONDS, float(cold.duration or 0.0))
        out.append(_make(
            "opening_hook", context, 0.0, duration,
            source_type="retention",
            source_id=getattr(cold, "hook_id", "") or "cold_open",
            confidence=0.85,
            importance=0.95,
            label=(getattr(cold, "suggested_text", "")
                   or f"a {cold.hook_type} opening"),
            evidence=[f"cold open: {cold.hook_type}",
                      f"lifted from {cold.original_start:.1f}s"],
        ))

    for decision in getattr(retention_plan, "decisions", []) or []:
        if not getattr(decision, "accepted", False):
            continue
        if decision.action not in COMPRESSED_ACTIONS:
            continue
        for span in getattr(decision, "spans", []) or []:
            resolved = context.range_at(span.asset_id, span.start, span.end)
            if resolved is None:
                continue
            start, end = resolved
            kind = ("grind_montage" if decision.action == "speed_up"
                    else "boring_compression")
            out.append(_make(
                kind, context, start, end,
                source_type="retention",
                source_id=decision.decision_id,
                confidence=max(decision.confidence, 0.5),
                importance=min(0.6, decision.priority),
                label=decision.reason[:200],
                evidence=[f"retention {decision.action}: {decision.reason}"[:280]],
                asset_id=span.asset_id,
                segment_ids=span.segment_ids,
            ))
            break                       # one moment per decision, not per span
    return out


def _from_hooks(retention_findings, retention_plan,
                context: _CutContext) -> list[VisualMoment]:
    """Hook candidates the retention planner found but nothing used.

    Worth knowing about even when no cold open was cut: a strong moment sitting
    in the middle of an episode is exactly what a punch-in or a card is for.
    """
    if retention_findings is None:
        return []
    used = ""
    cold = getattr(retention_plan, "cold_open", None)
    if cold is not None and getattr(cold, "chosen", False):
        used = getattr(cold, "hook_id", "")

    out: list[VisualMoment] = []
    for hook in getattr(retention_findings, "hooks", []) or []:
        if hook.item_id == used or hook.score < 0.4:
            continue
        resolved = _episode_range(hook, retention_findings, context)
        if resolved is None:
            continue
        start, end = resolved
        kind = {
            "danger": "danger", "failure": "death_or_fail",
            "comedy": "funny_reaction", "reveal": "reveal",
            "mystery": "cliffhanger", "goal": "objective_start",
            "challenge": "objective_start",
        }.get(hook.hook_type, "reveal")
        out.append(_make(
            kind, context, start, end,
            source_type="episode",
            source_id=hook.item_id,
            confidence=max(0.45, hook.confidence),
            importance=hook.score,
            label=hook.suggested_text or hook.why or f"a {hook.hook_type} moment",
            evidence=[f"hook candidate ({hook.hook_type}), "
                      f"score {hook.score:.2f}", hook.why[:200]],
            segment_ids=getattr(hook, "segment_ids", ()) or (),
        ))
    return out


def _from_audio(audio_plan, context: _CutContext) -> list[VisualMoment]:
    """Sound cues the audio polish pass accepted.

    Where sound was earned, a picture usually was too. The audio pass has
    already resolved these onto the cut and refused anything that would land
    on a word, so its accepted list is a clean set of "something happened
    here" markers.
    """
    if audio_plan is None:
        return []
    out: list[VisualMoment] = []
    for cue in getattr(audio_plan, "accepted", []) or []:
        kind = {
            "hit": "danger", "riser": "reveal", "silence_drop": "reveal",
        }.get(cue.kind, "")
        if not kind:
            continue
        moment = cue.moment or ""
        kind = {
            "death_or_fail": "death_or_fail", "reveal": "reveal",
            "payoff_line": "payoff", "danger": "danger",
        }.get(moment, kind)
        out.append(_make(
            kind, context, cue.start, cue.end,
            source_type="audio",
            source_id=cue.cue_id,
            confidence=max(0.45, cue.priority),
            importance=cue.priority,
            label=cue.target[:200],
            evidence=[f"audio cue ({cue.kind}): {cue.reason}"[:280]],
            segment_ids=[cue.segment_id] if cue.segment_id else (),
        ))
    return out


def _from_memory(memory, context: _CutContext) -> list[VisualMoment]:
    """Beats, objectives and callbacks the episode layer read off the cut.

    Only when the memory's timebase is the cut's. A memory built against the
    synthetic timeline ordering has numbers that would place every finding
    somewhere -- every one of them a number, all of them wrong.
    """
    if memory is None:
        return []
    out: list[VisualMoment] = []

    for beat in getattr(memory, "beats", []) or []:
        kind = BEAT_TO_MOMENT.get(beat.kind)
        if kind is None or beat.confidence < MIN_BEAT_CONFIDENCE:
            continue
        resolved = _episode_range(beat, memory, context)
        if resolved is None:
            continue
        start, end = resolved
        out.append(_make(
            kind, context, start, end,
            source_type="episode",
            source_id=beat.item_id,
            confidence=beat.confidence,
            importance=max(beat.interest, 0.3),
            label=beat.why[:200] or f"a {beat.kind} beat",
            evidence=[f"beat: {beat.kind} ({beat.confidence:.2f})",
                      beat.why[:200]],
            segment_ids=getattr(beat, "segment_ids", ()) or (),
        ))

    objective = getattr(memory, "main_objective", None)
    if objective is not None and getattr(objective, "text", ""):
        resolved = _episode_range(objective, memory, context)
        if resolved is not None:
            status = getattr(objective, "status", "stated")
            kind = ("objective_complete" if status == "achieved"
                    else "objective_start")
            out.append(_make(
                kind, context, resolved[0], resolved[1],
                source_type="episode",
                source_id=objective.item_id,
                confidence=max(0.5, objective.confidence),
                importance=0.9,
                label=objective.text[:200],
                evidence=[f"the episode's stated objective ({status})",
                          objective.text[:200]],
                segment_ids=getattr(objective, "segment_ids", ()) or (),
            ))

    for callback in getattr(memory, "callbacks", []) or []:
        resolved = _episode_range(callback, memory, context)
        if resolved is None:
            continue
        out.append(_make(
            "callback", context, resolved[0], resolved[1],
            source_type="episode",
            source_id=callback.item_id,
            confidence=max(0.45, callback.confidence),
            importance=0.6,
            label=callback.why[:200] or "a callback to something earlier",
            evidence=[f"callback: {callback.why[:200]}"],
            segment_ids=getattr(callback, "segment_ids", ()) or (),
        ))
    return out


def _episode_range(item, memory, context: _CutContext) -> Optional[tuple]:
    """One episode-layer finding, placed on the cut, or None.

    Two paths, and the memory's own ``timebase`` picks between them. Guessing
    would put every finding *somewhere*, which is the failure mode that looks
    like success.
    """
    timebase = str(getattr(memory, "timebase", "") or "")
    segment_ids = getattr(item, "segment_ids", ()) or ()

    if timebase == "roughcut":
        start = float(getattr(item, "start", 0.0) or 0.0)
        end = float(getattr(item, "end", 0.0) or 0.0)
        if end > start and start <= context.duration:
            return (start, min(end, context.duration))

    return context.by_segment_ids(segment_ids)


def _from_timeline(timeline: StructureTimeline,
                   context: _CutContext) -> list[VisualMoment]:
    """What the vision and audio passes saw directly.

    The floor under everything else: a death screen, a creeper on screen, a
    villager situation, a panicked line. These are things that were *measured*,
    and they are what makes the layer produce anything at all on footage that
    never went near a director or a retention pass.
    """
    out: list[VisualMoment] = []
    for segment in timeline.segments:
        resolved = context.range_at(
            segment.asset_id, segment.start, segment.end)
        if resolved is None:
            continue
        start, end = resolved

        events = segment.events
        threats = sorted({t for event in events for t in event.threats})
        entities = sorted({e for event in events for e in event.entities})
        said = (segment.said or "").lower()
        death = any(getattr(event.ui, "death_screen", False)
                    for event in events)
        low_health = any(getattr(event.ui, "low_health", False)
                         for event in events)
        audio_types = segment.audio_types()
        common = {
            "asset_id": segment.asset_id,
            "segment_ids": [segment.segment_id],
        }

        if death:
            out.append(_make(
                "death_or_fail", context, start, end,
                source_type="visual", source_id=segment.segment_id,
                confidence=0.9, importance=0.9,
                label="a death screen is on screen",
                evidence=["vision: death_screen"], **common))
            continue

        if low_health and threats:
            out.append(_make(
                "near_death", context, start, end,
                source_type="visual", source_id=segment.segment_id,
                confidence=0.75, importance=0.85,
                label=f"low health with {threats[0]} on screen",
                evidence=[f"vision: low_health, threats {', '.join(threats[:3])}"],
                **common))
            continue

        danger = sorted(DANGER_ENTITIES & {t.lower() for t in threats})
        if danger:
            out.append(_make(
                "danger", context, start, end,
                source_type="visual", source_id=segment.segment_id,
                confidence=0.7, importance=0.7,
                label=f"{danger[0]} on screen",
                evidence=[f"vision: threats {', '.join(danger[:3])}"],
                **common))

        village = VILLAGER_ENTITIES & {e.lower() for e in entities}
        if len(village) >= 2:
            out.append(_make(
                "villager_chaos", context, start, end,
                source_type="visual", source_id=segment.segment_id,
                confidence=0.55, importance=0.6,
                label="several villager-adjacent entities at once: "
                      + ", ".join(sorted(village)[:4]),
                evidence=[f"vision: entities {', '.join(sorted(village)[:4])}"],
                **common))

        if _hit(said, PANIC_WORDS) and (
                threats or "sudden_reaction" in audio_types):
            out.append(_make(
                "panic", context, start, end,
                source_type="transcript", source_id=segment.segment_id,
                confidence=0.6, importance=0.75,
                label=f'panic: "{_hit(said, PANIC_WORDS)}"',
                evidence=[f'said "{segment.said[:120]}"'], **common))

        if _hit(said, BANTER_WORDS) and "possible_laughter" in audio_types:
            out.append(_make(
                "banter_spike", context, start, end,
                source_type="transcript", source_id=segment.segment_id,
                confidence=0.5, importance=0.55,
                label=f'banter: "{_hit(said, BANTER_WORDS)}"',
                evidence=[f'said "{segment.said[:120]}"',
                          "audio: possible_laughter"], **common))

        found = _hit(said, FIND_WORDS)
        if found and segment.importance in ("reveal", "payoff"):
            out.append(_make(
                "important_find", context, start, end,
                source_type="transcript", source_id=segment.segment_id,
                confidence=0.65, importance=0.8,
                label=f'found: "{found}"',
                evidence=[f'said "{segment.said[:120]}"',
                          f"picture is a {segment.importance}"], **common))

        if _hit(said, CLIFFHANGER_WORDS):
            out.append(_make(
                "cliffhanger", context, start, end,
                source_type="transcript", source_id=segment.segment_id,
                confidence=0.6, importance=0.7,
                label=f'sign-off: "{_hit(said, CLIFFHANGER_WORDS)}"',
                evidence=[f'said "{segment.said[:120]}"'], **common))
    return out


def _hit(lowered: str, words: Sequence[str]) -> str:
    for word in words:
        if word in lowered:
            return word
    return ""


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

#: Two moments of the same kind closer than this are the same moment seen by
#: two layers.
MERGE_WINDOW = 1.5


def _merge(moments: Sequence[VisualMoment]) -> list[VisualMoment]:
    """One moment per thing that happened, however many layers noticed it.

    The strongest evidence wins the record and the rest is folded into it. Two
    moments would each earn their own effect, which is how a single death ends
    up with a zoom, a freeze frame and an arrow on it.
    """
    ordered = sorted(
        moments,
        key=lambda m: (m.kind, m.start, -m.confidence, -m.importance),
    )
    out: list[VisualMoment] = []
    for moment in ordered:
        merged = False
        for existing in out:
            if existing.kind != moment.kind:
                continue
            if abs(existing.start - moment.start) > MERGE_WINDOW:
                continue
            existing.end = max(existing.end, moment.end)
            existing.confidence = max(existing.confidence, moment.confidence)
            existing.importance = max(existing.importance, moment.importance)
            for item in moment.evidence:
                if item not in existing.evidence and len(existing.evidence) < 12:
                    existing.evidence.append(item)
            for name in moment.entities:
                if name not in existing.entities:
                    existing.entities.append(name)
            for segment_id in moment.segment_ids:
                if segment_id not in existing.segment_ids:
                    existing.segment_ids.append(segment_id)
            if not existing.label:
                existing.label = moment.label
            merged = True
            break
        if not merged:
            out.append(moment)

    out.sort(key=lambda m: (m.start, m.kind))
    return out
