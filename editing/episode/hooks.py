"""Choosing what could open the episode, what its peak is, and what ends it.

Three selectors, all working off the same memory.

A hook is not simply the most exciting moment. It has to carry stakes a
stranger can read with no context, open a question, and have that question
answered later in the episode -- otherwise it is a lie told in the first ten
seconds. So the score is itemised across those things rather than being one
number, and ``score_parts`` travels with the candidate so a ranking can be
argued with.

**Nothing here writes clickbait.** ``suggested_text`` is either a line lifted
verbatim from the transcript or a plain description assembled from what the
vision model actually reported -- environment, threat, action. ``text_source``
says which, and a generated description always arrives flagged for review.
There is no third path where the system invents a claim about the footage.

The climax selector can decline. An episode whose top three moments are level
does not have a climax, and reporting one anyway would be exactly the confident
nonsense this layer exists to avoid; the runners-up are returned instead so the
flatness is visible.
"""
from __future__ import annotations

from typing import Optional

from editing.episode import beats as beats_module
from editing.episode import language
from editing.episode.schema import (
    ClimaxCandidate, EndingCandidate, EpisodeMemory, HookCandidate,
    PAYOFF_BEATS, capped, new_id,
)
from editing.episode.track import EpisodeTrack

#: How many hook candidates to return. More than one on purpose: choosing an
#: opening is an editorial call, and a list of five with their reasons is more
#: use than one with a number beside it.
HOOK_LIMIT = 5

#: A hook has to score at least this to be worth listing.
MIN_HOOK_SCORE = 0.30

#: Beats a hook can be cut from, with the hook type each implies.
HOOK_TYPE_FOR = {
    "danger": "danger",
    "failure": "failure",
    "joke": "comedy",
    "discovery": "reveal",
    "reveal": "reveal",
    "payoff": "reveal",
    "climax": "danger",
    "escalation": "challenge",
    "objective_stated": "goal",
}

#: Hooks longer than this need trimming before they are hooks.
MAX_HOOK_SECONDS = 12.0

#: A quote this long does not fit on an opening card.
MAX_QUOTE_CHARS = 70

#: The climax has to beat its runner-up by this much to be called one.
#: Re-exported from ``beats`` rather than restated, because two numbers
#: for one rule is how the memory and the plan came to disagree.
MIN_CLIMAX_MARGIN = beats_module.CLIMAX_MIN_MARGIN


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _describe(track: EpisodeTrack, start: float, end: float) -> str:
    """A plain description of what is on screen, from the closed vocabularies.

    Assembled only from tokens the vision model reported. It reads flatly on
    purpose: a description that sounds like a thumbnail title is a description
    that has started making claims.
    """
    covered = track.between(start, end)
    if not covered:
        return ""
    slot = max(covered, key=lambda item: item.interest)
    threat = slot.threats[0] if slot.threats else ""
    entity = slot.entities[0] if slot.entities else ""
    place = slot.environment if slot.environment != "unknown" else ""
    action = slot.primary_action if slot.primary_action != "unknown" else ""

    place_text = f" in the {place.replace('_', ' ')}" if place else ""
    if threat:
        return f"{threat.replace('_', ' ')}{place_text}".strip()
    if entity:
        return f"{entity.replace('_', ' ')}{place_text}".strip()
    if action:
        return f"{action}{place_text}".strip()
    return place.replace("_", " ")


def _hook_text(track: EpisodeTrack, beat) -> tuple:
    """``(text, source)`` for a hook. A quote when there is a usable one."""
    for line in track.quotes_between(beat.start, beat.end, limit=4):
        cleaned = line.strip()
        if 8 <= len(cleaned) <= MAX_QUOTE_CHARS:
            return cleaned, "transcript_quote"
    described = _describe(track, beat.start, beat.end)
    if described:
        return described, "generated_description"
    return "", "none"


def _viewer_question(hook_type: str, loop, topic: list) -> str:
    """What the hook makes the viewer want to find out.

    A loop's own wording is used only when it was actually a question. Loops
    also get opened by stated goals, and "the plan is to find diamonds" is a
    statement -- putting it under a hook as the viewer's question would be
    quoting the wrong half of the exchange.
    """
    if loop is not None:
        if language.is_question(loop.question):
            return loop.question
        topic = loop.topic or topic
    subject = topic[0].replace("_", " ") if topic else "this"
    return {
        "danger": f"does the {subject} go wrong?",
        "failure": f"how did the {subject} go wrong?",
        "comedy": "what happened here?",
        "reveal": f"what is the {subject}?",
        "mystery": f"what is the {subject}?",
        "goal": f"do they get the {subject}?",
        "challenge": f"can they handle the {subject}?",
    }.get(hook_type, "what happens next?")


def _loop_covering(memory: EpisodeMemory, beat) -> Optional[object]:
    """An open loop this beat sits inside, or that this beat opens.

    Preferring a *resolved* loop is deliberate: a hook is only honest when the
    question it raises gets answered, so a loop with a known answer is the
    better anchor even when an unresolved one also covers the moment.
    """
    covering = [
        loop for loop in memory.open_loops
        if loop.start <= beat.end and (
            loop.resolved_at is None or loop.resolved_at >= beat.start
        )
    ]
    if not covering:
        return None
    resolved = [loop for loop in covering if loop.resolved_at is not None]
    pool = resolved or covering
    return max(pool, key=lambda loop: loop.confidence)


def _payoff_after(memory: EpisodeMemory, beat) -> tuple:
    """``(time, id)`` of the first payoff-shaped beat after this one."""
    for candidate in memory.beats:
        if candidate.start <= beat.end:
            continue
        if candidate.kind in PAYOFF_BEATS:
            return candidate.start, candidate.item_id
    return None, ""


def find_hooks(
    memory: EpisodeMemory, track: EpisodeTrack, *, limit: int = HOOK_LIMIT
) -> list[HookCandidate]:
    """Candidate openings, best first.

    Every beat whose kind reads as a hook is scored; the ones that clear the
    floor are returned with their score broken out. A moment that is already at
    the very start of the episode is skipped -- it is not a hook, it is the
    beginning.
    """
    out: list[HookCandidate] = []
    for beat in memory.beats:
        hook_type = HOOK_TYPE_FOR.get(beat.kind)
        if hook_type is None:
            continue
        if beat.position <= 0.02:
            continue

        loop = _loop_covering(memory, beat)
        payoff_at, payoff_id = _payoff_after(memory, beat)
        text, source = _hook_text(track, beat)
        covered = track.between(beat.start, beat.end)
        peak = max(covered, key=lambda slot: slot.interest) if covered else None

        parts = {
            "visual_interest": round(beat.interest * 0.28, 3),
            "stakes": 0.0,
            "curiosity": 0.0,
            "payoff_exists": 0.0,
            "self_contained": 0.0,
            "reaction": 0.0,
        }
        if peak is not None and peak.threats:
            parts["stakes"] = 0.18
        elif beat.kind in ("danger", "failure", "escalation", "climax"):
            parts["stakes"] = 0.12
        if loop is not None:
            parts["curiosity"] = 0.08 + 0.10 * loop.confidence
        elif beat.kind in ("discovery", "reveal"):
            parts["curiosity"] = 0.08
        if payoff_at is not None:
            parts["payoff_exists"] = 0.18
        if beat.kind in ("danger", "failure", "joke", "discovery", "reveal",
                         "climax"):
            parts["self_contained"] = 0.12
        if peak is not None and peak.has_reaction:
            parts["reaction"] = 0.10

        score = min(1.0, sum(parts.values()))
        if score < MIN_HOOK_SCORE:
            continue

        risks = []
        if payoff_at is None:
            risks.append("no payoff later in the episode")
        if source == "generated_description":
            risks.append("suggested text is generated, not spoken")
        if source == "none":
            risks.append("nothing to put on screen; picture only")
        if beat.duration > MAX_HOOK_SECONDS:
            risks.append(
                f"the beat is {beat.duration:.0f}s and needs trimming to "
                f"{MAX_HOOK_SECONDS:.0f}s or less"
            )
        if beat.kind in ("payoff", "climax"):
            risks.append("this is a payoff; opening on it spoils it")

        candidate = HookCandidate(
            item_id=new_id("hook", beat.item_id),
            start=beat.start,
            end=min(beat.end, beat.start + MAX_HOOK_SECONDS),
            hook_type=hook_type,
            suggested_text=language.condense(text, limit=MAX_QUOTE_CHARS),
            text_source=source,
            viewer_question=_viewer_question(
                hook_type, loop, beat.evidence.quotes and
                language.topic(beat.evidence.quotes[0]) or []),
            payoff_at=payoff_at,
            payoff_id=payoff_id,
            score=score,
            score_parts=parts,
            setup_seconds=round(beat.start, 3),
            risks=risks,
            evidence=beat.evidence,
            confidence=capped(beat.confidence, beat.evidence.channels),
            why=(
                f"a {beat.kind} at {beat.start:.0f}s"
                + (f", answered at {payoff_at:.0f}s" if payoff_at else
                   ", with nothing later that answers it")
            ),
        )
        candidate.needs_human_review = (
            source != "transcript_quote" or payoff_at is None
        )
        candidate.settle()
        out.append(candidate)

    out.sort(key=lambda item: (-item.score, item.start))
    return out[:max(0, limit)]


# ---------------------------------------------------------------------------
# Climax
# ---------------------------------------------------------------------------

def find_climax(
    memory: EpisodeMemory, track: EpisodeTrack
) -> tuple:
    """``(climax, alternatives)``. ``climax`` is ``None`` when there is no peak.

    **The memory already made this call.** ``beats._mark_climax`` labels at most
    one beat ``climax``, using the same two thresholds imported below, and this
    function reports that verdict with its supporting numbers rather than
    ranking the field again. An earlier version scored independently here and
    disagreed with the beat list on real footage -- the memory said the payoff
    at 83% was the peak while the plan pointed at a discovery at 36% -- which is
    the worst possible outcome for two artifacts meant to be read together.

    Declining is a real answer. An episode whose best moments are within
    ``CLIMAX_MIN_MARGIN`` of each other does not have a climax, and every
    downstream use of one -- marking it, cutting toward it, saving the music for
    it -- is wrong if the peak was invented. The field is returned instead, so
    the flatness is what a reader sees.
    """
    eligible = [
        beat for beat in memory.beats
        if beat.kind == "climax" or (
            beat.position >= 0.5 and beat.kind in beats_module.CLIMAX_ELIGIBLE
        )
    ]
    if not eligible:
        return None, []

    ranked = sorted(eligible, key=lambda beat: -beat.interest)

    def build(beat, margin: float) -> ClimaxCandidate:
        closed = [
            loop.item_id for loop in memory.open_loops
            if loop.resolved_at is not None
            and beat.start <= loop.resolved_at <= beat.end
        ]
        parts = {
            "interest": round(beat.interest, 3),
            "lateness": round(beat.position, 3),
            "confidence": round(beat.confidence, 3),
            "resolves_loops": float(len(closed)),
        }
        candidate = ClimaxCandidate(
            item_id=new_id("climax", beat.item_id),
            start=beat.start,
            end=beat.end,
            score=beat.interest,
            score_parts=parts,
            position=beat.position,
            margin=margin,
            resolves_loop_ids=closed,
            beat_ids=[beat.item_id],
            evidence=beat.evidence,
            confidence=capped(beat.confidence, beat.evidence.channels),
            why=(
                f"a {beat.alternative or beat.kind} at {beat.position:.0%} "
                f"through, interest {beat.interest:.2f}"
                + (f", {margin:.2f} ahead of the next candidate"
                   if margin > 0 else
                   ", level with the next candidate")
            ),
        )
        candidate.settle()
        return candidate

    marked = next(
        (beat for beat in memory.beats if beat.kind == "climax"), None)
    others = [beat for beat in ranked if beat is not marked][:3]

    if marked is None:
        # The memory declined: nothing was clearly the peak. Say so by
        # returning the field with no winner.
        return None, [build(beat, 0.0) for beat in ranked[:4]]

    runner_up = max(
        (beat.interest for beat in ranked if beat is not marked), default=0.0)
    return (
        build(marked, max(0.0, marked.interest - runner_up)),
        [build(beat, 0.0) for beat in others],
    )


# ---------------------------------------------------------------------------
# Ending
# ---------------------------------------------------------------------------

def find_ending(
    memory: EpisodeMemory, track: EpisodeTrack
) -> tuple:
    """``(ending, alternatives)``: the moment the episode could end on.

    An ending that closes the *stated* objective beats one that merely happens
    last. When nothing closes the objective, the latest payoff-shaped beat wins
    and says so, and when there is neither, this returns ``None`` -- which is
    what the ``unclear_ending`` risk is about.
    """
    candidates: list[tuple] = []
    objective = memory.main_objective
    for beat in memory.beats:
        if beat.kind not in ("resolution", "outro", "payoff", "climax",
                             "recovery"):
            continue
        closed = [
            loop.item_id for loop in memory.open_loops
            if loop.resolved_at is not None
            and beat.start <= loop.resolved_at <= beat.end
        ]
        closes_objective = bool(
            objective is not None
            and objective.open_loop_id
            and objective.open_loop_id in closed
        )
        score = min(1.0, (
            0.35 * beat.position
            + 0.20 * beat.interest
            + (0.30 if closes_objective else 0.0)
            + (0.10 if closed else 0.0)
            + (0.10 if beat.kind in ("resolution", "outro") else 0.0)
        ))
        text, source = _hook_text(track, beat)
        candidates.append((score, beat, closed, closes_objective, text, source))

    if not candidates:
        return None, []

    candidates.sort(key=lambda row: -row[0])

    def build(row) -> EndingCandidate:
        score, beat, closed, closes_objective, text, source = row
        candidate = EndingCandidate(
            item_id=new_id("ending", beat.item_id),
            start=beat.start,
            end=beat.end,
            kind=beat.kind,
            score=score,
            position=beat.position,
            closes_main_objective=closes_objective,
            resolves_loop_ids=closed,
            suggested_text=language.condense(text, limit=MAX_QUOTE_CHARS),
            text_source=source,
            evidence=beat.evidence,
            confidence=capped(beat.confidence, beat.evidence.channels),
            why=(
                "closes the episode's stated objective" if closes_objective
                else f"the strongest {beat.kind} near the end"
            ),
        )
        candidate.needs_human_review = not closes_objective
        candidate.settle()
        return candidate

    return build(candidates[0]), [build(row) for row in candidates[1:3]]


def midpoint_moment(
    memory: EpisodeMemory, track: EpisodeTrack
) -> Optional[tuple]:
    """Where a mid-episode reset would go, and what evidence supports it.

    Returns ``(start, end, evidence, why)`` or ``None``. The suggestion itself
    is built by the planner; this only finds the spot, which is the last quiet
    moment before the back half starts -- a reset lands better on a lull than
    on top of something happening.
    """
    if track.duration <= 0 or not memory.beats:
        return None
    target = track.duration * 0.5
    quiet = [
        beat for beat in memory.beats
        if beat.is_quiet and abs(beat.position - 0.5) <= 0.2
    ]
    if quiet:
        beat = min(quiet, key=lambda item: abs(item.position - 0.5))
        return (
            beat.start,
            min(beat.end, beat.start + 6.0),
            beat.evidence,
            f"a {beat.kind} at {beat.position:.0%} through, the quietest "
            "moment near the midpoint",
        )

    # No lull. Rather than drop the marker on whatever is happening -- which
    # once put "restate the goal" in the middle of a fight -- put it on the
    # nearest *boundary* between beats. A cut is always a legitimate place for
    # a marker, and it interrupts nothing.
    boundaries = [beat.start for beat in memory.beats if beat.start > 0]
    if not boundaries:
        return None
    edge = min(boundaries, key=lambda value: abs(value - target))
    beat = memory.beat_at(edge) or memory.beats[0]
    return (
        edge,
        min(edge + 6.0, track.duration),
        beat.evidence,
        f"the beat boundary nearest the midpoint ({edge:.0f}s); nothing here "
        "is a lull, so the marker goes on a cut rather than over the action",
    )
