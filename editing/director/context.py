"""Everything the director is told, assembled and compacted.

## The compaction problem

A 40-minute episode is ~300 timeline segments, a transcript of eight thousand
words, a hundred visual events, two hundred audio events, thirty
recommendations and a whole episode memory. Handing that to a model raw does
two bad things: it costs a fortune in context, and it makes the decisions
*worse* -- a model given three hundred near-identical mining segments writes
three hundred shallow judgements instead of twenty good ones.

So this module is mostly about what to leave out, and the rules are:

* **Merge before you drop.** Adjacent segments that would get the same verdict
  are one candidate range, not eight. That is the single biggest reduction and
  it loses nothing, because the director would have made one decision about
  them anyway.
* **Summarise speech, never invent it.** A segment's line is trimmed to a
  budget and marked when it was trimmed. Nothing is paraphrased -- a
  paraphrase is a claim about what somebody said.
* **Keep the story layer whole.** Beats, open loops, setups, payoffs and risks
  are small and are the entire reason this pass can do better than a
  threshold. They are the last thing to go.
* **Say what was dropped.** ``context.dropped`` lists every reduction, so a
  bad decision can be traced to the fact the model never saw the evidence.

## What is *not* in here

No operations, no scores the model is asked to reproduce, and no instruction.
This module builds the facts; ``prompt.py`` decides how to ask about them.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.director.schema import (
    ContextSegment, DirectorConfig, DirectorContext, StyleGuide, now,
)
from editing.schema import StructureTimeline, TimelineSegment

logger = logging.getLogger("nova.editing.director.context")

#: Adjacent segments merge into one candidate when they are this close and
#: would read the same way. The rough cut merges at 0.75s for a different
#: reason (cut points); this is about how many things the model has to weigh.
MERGE_GAP = 2.0

#: A merged candidate never gets longer than this. Past a minute the director
#: is deciding about a scene rather than a shot, and cannot shorten within it.
MAX_CANDIDATE_SECONDS = 60.0

#: Audio event types worth a word in the context. The rest are noise at this
#: altitude.
INTERESTING_AUDIO = (
    "sudden_reaction", "possible_laughter", "possible_scream",
    "loudness_spike", "silence", "long_pause", "low_energy", "music",
)


def build(
    timeline: StructureTimeline,
    *,
    config: Optional[DirectorConfig] = None,
    style_guide: Optional[StyleGuide] = None,
    memory=None,
    retention=None,
    recommendations=None,
    roughcut=None,
    preferences: Optional[Sequence] = None,
    style_preset=None,
    name: str = "structure",
) -> DirectorContext:
    """Assemble the director's view of one episode.

    Every input except the timeline is optional, and the context records which
    were present. A director working without the episode memory is working
    without the story layer, and a plan built that way should say so rather
    than look identical to one that had it.
    """
    config = (config or DirectorConfig()).validated()
    guide = style_guide or StyleGuide()

    context = DirectorContext(
        name=name,
        duration=timeline.duration if hasattr(timeline, "duration") else 0.0,
        style_guide=guide,
        generated_at=now(),
        sources={
            "timeline": True,
            "transcript": any(s.has_speech for s in timeline.segments),
            "episode_memory": memory is not None,
            "retention_plan": retention is not None,
            "recommendations": recommendations is not None,
            "roughcut": roughcut is not None,
            "preferences": bool(preferences),
            "style_preset": style_preset is not None,
        },
    )

    context.segments = _candidates(timeline, config, memory)
    context.duration = context.duration or _duration_of(timeline)

    if memory is not None:
        _from_memory(context, memory, config)
    if retention is not None:
        _from_retention(context, retention, config)
    if recommendations is not None:
        _from_recommendations(context, recommendations, config)
    if preferences:
        _from_preferences(context, preferences, config)
    if style_preset is not None:
        context.style_summary = _style_summary(style_preset)

    context.episode_id = getattr(memory, "episode_id", "") or ""
    context.summary = _summary(context, timeline)
    _warn(context, timeline)
    _fit_budget(context, config)
    return context


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def _candidates(
    timeline: StructureTimeline, config: DirectorConfig, memory
) -> list[ContextSegment]:
    """Timeline segments, merged and trimmed into decidable candidates."""
    ordered = sorted(
        timeline.segments, key=lambda s: (s.source_file, s.start))
    duration = _duration_of(timeline)
    elapsed = 0.0

    merged: list[ContextSegment] = []
    for segment in ordered:
        candidate = _describe(segment, elapsed, duration, memory)
        elapsed += segment.duration
        previous = merged[-1] if merged else None
        if previous is not None and _mergeable(previous, candidate):
            _absorb(previous, candidate, config)
            continue
        merged.append(candidate)

    if len(merged) > config.max_segments:
        merged = _thin(merged, config.max_segments)
    for candidate in merged:
        candidate.said = _trim_speech(candidate.said,
                                      config.max_transcript_chars)
    return merged


def _describe(
    segment: TimelineSegment, elapsed: float, duration: float, memory
) -> ContextSegment:
    """One timeline segment as the model will see it."""
    events = [event for event in segment.events if not event.error]
    environment = events[0].environment if events else ""
    actions: list[str] = []
    for event in events:
        for action in event.actions:
            if action not in actions:
                actions.append(action)

    audio = []
    for event in segment.audio_events:
        if event.type in INTERESTING_AUDIO and event.type not in audio:
            audio.append(event.type)

    beat = ""
    if memory is not None and getattr(memory, "timebase", "") == "timeline":
        found = memory.beat_at(elapsed)
        beat = found.kind if found is not None else ""

    return ContextSegment(
        segment_id=segment.segment_id,
        asset_id=segment.asset_id,
        source_file=segment.source_file,
        start=segment.start,
        end=segment.end,
        position=round(elapsed / duration, 4) if duration > 0 else 0.0,
        said=segment.said.strip(),
        environment=environment,
        actions=actions[:5],
        importance=segment.importance,
        audio=audio[:5],
        usefulness=round(segment.usefulness, 3),
        dead_air=segment.is_dead_air,
        beat=beat,
        heuristic=_heuristic_verdict(segment),
    )


def _heuristic_verdict(segment: TimelineSegment) -> str:
    """What the Session 3 selector would do with this, in one word.

    Included so the model can *disagree* with the existing system rather than
    start from nothing -- and so a report can count how often it did, which is
    the only cheap measure of whether this layer is adding anything.
    """
    if segment.is_dead_air:
        return "cut"
    if segment.importance in ("payoff", "reveal", "danger", "funny"):
        return "keep"
    if segment.usefulness >= 0.40 or segment.usable:
        return "keep"
    if segment.has_speech:
        return "keep-talking"
    return "speed_up"


def _mergeable(previous: ContextSegment, candidate: ContextSegment) -> bool:
    """Whether two candidates would get the same decision anyway."""
    if previous.asset_id != candidate.asset_id:
        return False
    if candidate.start - previous.end > MERGE_GAP:
        return False
    if previous.end - previous.start >= MAX_CANDIDATE_SECONDS:
        return False
    # Never merge across a change of verdict, a change of importance, or into
    # or out of dead air: those are exactly the boundaries a director cuts on.
    return (previous.heuristic == candidate.heuristic
            and previous.importance == candidate.importance
            and previous.dead_air == candidate.dead_air)


def _absorb(
    previous: ContextSegment, candidate: ContextSegment, config: DirectorConfig
) -> None:
    """Fold ``candidate`` into ``previous``."""
    previous.end = max(previous.end, candidate.end)
    if candidate.said:
        joined = f"{previous.said} {candidate.said}".strip()
        previous.said = joined
    for action in candidate.actions:
        if action not in previous.actions:
            previous.actions.append(action)
    for kind in candidate.audio:
        if kind not in previous.audio:
            previous.audio.append(kind)
    previous.usefulness = max(previous.usefulness, candidate.usefulness)
    previous.beat = previous.beat or candidate.beat
    previous.environment = previous.environment or candidate.environment


def _thin(candidates: list[ContextSegment], limit: int) -> list[ContextSegment]:
    """Drop the least decidable candidates until the list fits.

    Ranked by what a director would miss: high-importance moments, speech and
    audio reactions stay; long silent stretches of the same thing go first.
    Order is restored afterwards, because position in the episode is itself
    information.
    """
    def value(entry: ContextSegment) -> float:
        score = entry.usefulness
        if entry.importance in ("payoff", "reveal", "danger", "funny"):
            score += 1.0
        if entry.said:
            score += 0.4
        if entry.audio:
            score += 0.3
        if entry.dead_air:
            score -= 0.5
        return score

    # The opening and the ending are structurally important whatever is in
    # them: one is where a hook has to come from and the other is the last
    # thing a viewer sees. Taken by position in the list rather than by the
    # computed fraction, because a segment's ``position`` is measured at its
    # *start* -- the final candidate of an episode never reaches 1.0, so a
    # threshold on it would silently never protect the ending.
    protected = {id(candidates[0]), id(candidates[-1])} if candidates else set()
    ranked = sorted(
        candidates,
        key=lambda entry: (id(entry) in protected, value(entry)),
        reverse=True,
    )
    kept = ranked[:limit]
    order = {id(entry): index for index, entry in enumerate(candidates)}
    return sorted(kept, key=lambda entry: order[id(entry)])


def _trim_speech(said: str, limit: int) -> str:
    """Cap a line, and say it was capped. Never paraphrase.

    Keeping the head and the tail rather than the head alone: the end of a
    stretch of commentary is where the reaction usually is, and a director
    deciding whether a joke lands needs the punchline more than the setup.
    """
    text = " ".join(said.split())
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit < 40:
        return text[:limit].rstrip() + "..."
    head = limit // 2 - 3
    tail = limit - head - 5
    return f"{text[:head].rstrip()} ... {text[-tail:].lstrip()}"


# ---------------------------------------------------------------------------
# The story layer
# ---------------------------------------------------------------------------

def _from_memory(context: DirectorContext, memory, config: DirectorConfig
                 ) -> None:
    objective = memory.main_objective
    if objective is not None:
        context.objective = objective.text[:400] if hasattr(
            objective, "text") else str(objective)[:400]
        context.objective_status = getattr(objective, "status", "")

    context.beats = [
        {
            "id": beat.item_id,
            "kind": beat.kind,
            "start": round(beat.start, 1),
            "end": round(beat.end, 1),
            "why": beat.why[:200],
            "confidence": round(beat.confidence, 2),
        }
        for beat in memory.beats[:config.max_beats]
    ]
    context.open_loops = [
        {
            "id": loop.item_id,
            "question": getattr(loop, "question", "")[:200],
            "opened_at": round(loop.start, 1),
            "resolved": bool(getattr(loop, "resolved", False)),
            "resolved_at": round(getattr(loop, "resolved_at", 0.0) or 0.0, 1),
        }
        for loop in memory.open_loops[:config.max_open_loops]
    ]
    context.setups = [
        {
            "id": setup.item_id,
            "start": round(setup.start, 1),
            "end": round(setup.end, 1),
            "what": setup.why[:200],
            "payoff_id": getattr(setup, "payoff_id", ""),
        }
        for setup in memory.setups[:20]
    ]
    context.payoffs = [
        {
            "id": payoff.item_id,
            "start": round(payoff.start, 1),
            "end": round(payoff.end, 1),
            "what": payoff.why[:200],
            "setup_id": getattr(payoff, "setup_id", ""),
        }
        for payoff in memory.payoffs[:20]
    ]
    context.callbacks = [
        {
            "id": item.item_id,
            "start": round(item.start, 1),
            "what": item.why[:200],
        }
        for item in memory.callbacks[:20]
    ]


def _from_retention(context: DirectorContext, retention,
                    config: DirectorConfig) -> None:
    context.risks = [
        {
            "id": risk.item_id,
            "risk": risk.risk,
            "severity": risk.severity,
            "start": round(risk.start, 1),
            "end": round(risk.end, 1),
            "why": risk.why[:200],
        }
        for risk in retention.risks[:config.max_risks]
    ]
    context.hook_candidates = [
        {
            "id": hook.item_id,
            "start": round(hook.start, 1),
            "end": round(hook.end, 1),
            "score": round(getattr(hook, "score", 0.0), 2),
            "why": hook.why[:200],
        }
        for hook in retention.top_hooks(config.max_hooks)
    ]
    if retention.climax is not None:
        context.climax = {
            "id": retention.climax.item_id,
            "start": round(retention.climax.start, 1),
            "end": round(retention.climax.end, 1),
            "why": retention.climax.why[:200],
        }
    if retention.ending is not None:
        context.ending = {
            "id": retention.ending.item_id,
            "start": round(retention.ending.start, 1),
            "end": round(retention.ending.end, 1),
            "why": retention.ending.why[:200],
        }


def _from_recommendations(context: DirectorContext, recommendations,
                          config: DirectorConfig) -> None:
    """The heuristic layer's proposals, so the director can weigh them.

    Only accepted ones, and only the categories that say something about
    *content*. A punch-in is a decision for the style pass; whether a stretch
    is worth keeping is a decision for this one.
    """
    entries = [
        entry for entry in recommendations.recommendations
        if entry.status == "accepted"
    ]
    entries.sort(key=lambda entry: entry.priority, reverse=True)
    context.recommendations = [
        {
            "id": entry.recommendation_id,
            "category": entry.category,
            "start": round(entry.start, 1),
            "end": round(entry.end, 1),
            "priority": round(entry.priority, 2),
            "why": (entry.reason or "")[:160],
        }
        for entry in entries[:config.max_recommendations]
    ]


def _from_preferences(context: DirectorContext, preferences,
                      config: DirectorConfig) -> None:
    """What this person has already said about earlier cuts.

    Statements only, and every one of them stays a *statement* -- Session 9 is
    explicit that a preference signal is evidence about taste and not
    permission to act, so it reaches the model as something to weigh rather
    than a rule the safety layer enforces.
    """
    out: list[str] = []
    for signal in preferences:
        statement = getattr(signal, "statement", "") or str(signal)
        count = getattr(signal, "evidence_count", 0)
        agreement = getattr(signal, "agreement", 0.0)
        suffix = ""
        if count:
            suffix = f"  (from {count} rating(s), {agreement:.0%} consistent)"
        out.append(f"{statement}{suffix}"[:300])
        if len(out) >= config.max_preferences:
            break
    context.preferences = out


def _style_summary(preset) -> str:
    """What the later style pass will do on top of this cut.

    The director needs it because a style that adds a caption every eight
    seconds changes what a silent stretch is worth.
    """
    parts = [
        f"style '{getattr(preset, 'name', '?')}' "
        f"({getattr(preset, 'pacing', '?')} pacing)",
        f"up to {getattr(preset, 'max_edits_per_minute', 0):g} active "
        "edits/min",
        f"up to {getattr(preset, 'max_captions_per_minute', 0):g} "
        "captions/min",
    ]
    if not getattr(preset, "zooms_allowed", False):
        parts.append("no zooms")
    if getattr(preset, "title_cards", False):
        parts.append("title cards on")
    description = getattr(preset, "description", "")
    if description:
        parts.append(description)
    return "; ".join(str(part) for part in parts)


# ---------------------------------------------------------------------------
# Summary, warnings and the budget
# ---------------------------------------------------------------------------

def _summary(context: DirectorContext, timeline: StructureTimeline) -> str:
    """A paragraph describing the episode, from measurements only.

    Deliberately not written by a model. This is the first thing the director
    reads, and a generated summary would be a claim about the episode that
    every later decision inherits without anybody checking it.
    """
    total = context.duration
    speaking = sum(1 for s in context.segments if s.said)
    environments: dict = {}
    for segment in context.segments:
        if segment.environment:
            environments[segment.environment] = environments.get(
                segment.environment, 0) + 1
    top = sorted(environments.items(), key=lambda pair: -pair[1])[:3]

    parts = [
        f"{len(timeline.assets)} clip(s), {total / 60:.0f} minutes of footage, "
        f"{len(context.segments)} candidate range(s) after merging."
    ]
    if top:
        parts.append(
            "Mostly " + ", ".join(f"{name} ({count})" for name, count in top)
            + "."
        )
    if speaking:
        parts.append(f"{speaking} of them have commentary over them.")
    else:
        parts.append(
            "None of them have commentary: there is no transcript, so every "
            "story judgement below rests on picture and sound alone."
        )
    if context.beats:
        kinds = {}
        for beat in context.beats:
            kinds[beat["kind"]] = kinds.get(beat["kind"], 0) + 1
        parts.append(
            "Story beats found: "
            + ", ".join(f"{kind} x{count}" for kind, count in
                        sorted(kinds.items(), key=lambda p: -p[1])[:5]) + "."
        )
    return " ".join(parts)


def _warn(context: DirectorContext, timeline: StructureTimeline) -> None:
    if not context.sources.get("transcript"):
        context.warnings.append(
            "No transcript: the director cannot hear the episode, so hooks, "
            "open loops and comedy timing are guesses from picture and sound."
        )
    if not context.sources.get("episode_memory"):
        context.warnings.append(
            "No episode memory: no beats, setups, payoffs or open loops were "
            "available, which is most of what this pass is for. Run "
            "`episode build-memory` first."
        )
    if not context.sources.get("retention_plan"):
        context.warnings.append(
            "No retention plan: no risk zones or hook candidates were "
            "available. Run `episode plan-retention` first."
        )
    if not context.segments:
        context.warnings.append(
            "No candidate ranges: the timeline is empty, so there is nothing "
            "to decide about."
        )


def _fit_budget(context: DirectorContext, config: DirectorConfig) -> None:
    """Drop sections until the rendered context fits, worst-value first.

    The order is the argument. Recommendations go first because the director
    can re-derive them; preferences next because they are advice rather than
    facts; speech is shortened before segments are removed because *which*
    ranges exist matters more than what was said in each; and the story layer
    is never dropped, because without it this pass is a slower threshold.
    """
    from editing.director import prompt as prompt_module

    def size() -> int:
        return len(prompt_module.render_context(context))

    if size() <= config.max_context_chars:
        return

    if context.recommendations:
        removed = len(context.recommendations)
        context.recommendations = context.recommendations[:10]
        context.dropped.append(
            f"trimmed heuristic recommendations from {removed} to 10 to fit "
            "the context budget")
        if size() <= config.max_context_chars:
            return

    if context.preferences:
        removed = len(context.preferences)
        context.preferences = context.preferences[:5]
        context.dropped.append(
            f"trimmed learned preferences from {removed} to 5")
        if size() <= config.max_context_chars:
            return

    for limit in (140, 90, 50, 0):
        for segment in context.segments:
            segment.said = _trim_speech(segment.said, limit)
        context.dropped.append(
            f"shortened every quoted line to {limit} characters"
            if limit else "removed every quoted line")
        if size() <= config.max_context_chars:
            return

    while len(context.segments) > 20 and size() > config.max_context_chars:
        before = len(context.segments)
        context.segments = _thin(context.segments, max(20, before // 2))
        context.dropped.append(
            f"dropped {before - len(context.segments)} candidate range(s), "
            "least decidable first")

    if size() > config.max_context_chars:
        context.warnings.append(
            f"The context is {size()} characters, over the "
            f"{config.max_context_chars} budget, and could not be reduced "
            "further. Raise --context-chars or narrow the footage."
        )


def _duration_of(timeline: StructureTimeline) -> float:
    total = getattr(timeline, "duration", 0.0) or 0.0
    if total:
        return float(total)
    return round(sum(segment.duration for segment in timeline.segments), 3)
