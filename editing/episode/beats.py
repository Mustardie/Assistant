"""Detecting what each stretch of the episode is doing.

The detector scores every beat kind for every slot, from as many channels as
the slot has, and then takes the best. Four rules shape it, and each is
enforced rather than intended:

**Never on keywords alone.** A cue phrase contributes score like anything
else, but a beat whose only evidence is the transcript ends up with one channel
and is capped below the edit threshold by ``schema.capped``. It is still
recorded, still visible, and still cannot move a frame.

**Do not over-label.** A slot whose best kind scores under ``MIN_BEAT_SCORE``
is labelled ``unknown`` and kept. Labelling everything would make the beat list
useless in the same way a search that matches every document is useless.

**Preserve uncertainty.** Every beat carries the runner-up kind and the full
score table, so "danger 0.51 / joke 0.49" stays legible as a close call rather
than being flattened into "danger".

**Merge, do not fragment.** Adjacent slots doing the same job are one beat.
Four consecutive twenty-second mining windows are one grind, not four.
"""
from __future__ import annotations

from typing import Optional

from editing.episode import language
from editing.episode.schema import (
    BEAT_KINDS, EpisodeBeat, EpisodeEvidence, MIN_EDIT_CONFIDENCE, capped,
    new_id,
)
from editing.episode.track import EpisodeTrack

#: Below this, the slot is not doing anything nameable and is left ``unknown``.
MIN_BEAT_SCORE = 0.30

#: Slots this far apart or closer merge into one beat when they agree.
MERGE_GAP = 2.0

#: Actions that are only interesting the first time. A run of these is a grind.
REPEATABLE_ACTIONS = frozenset({
    "mining", "building", "farming", "crafting", "searching", "travelling",
    "looting",
})

#: How long the same repeatable action has to run before it reads as a grind.
GRIND_SECONDS = 45.0

#: ...or how many consecutive slots, whichever comes first.
GRIND_SLOTS = 3

#: A beat has to reach this interest to be callable the climax at all.
CLIMAX_MIN_INTEREST = 0.6

#: ...and beat the runner-up by this much. Below it the episode does not
#: have a peak, and nothing is labelled.
CLIMAX_MIN_MARGIN = 0.05

#: Beats that could be the biggest moment. Shared with the retention plan,
#: which reports the margin rather than recomputing the winner.
CLIMAX_ELIGIBLE = (
    "payoff", "danger", "reveal", "discovery", "failure", "escalation",
)

#: What each visual importance says about the beat.
_IMPORTANCE_SCORES = {
    "danger": {"danger": 0.50, "escalation": 0.15},
    "payoff": {"payoff": 0.50, "discovery": 0.15},
    "reveal": {"reveal": 0.45, "discovery": 0.25},
    "funny": {"joke": 0.45},
    "tension": {"danger": 0.25, "escalation": 0.22},
    "setup": {"setup": 0.24, "preparation": 0.14},
    "boring": {"travel": 0.20, "grind": 0.14},
}

#: What each player action says about it.
_ACTION_SCORES = {
    "travelling": {"travel": 0.40},
    "dying": {"failure": 0.55},
    "fighting": {"danger": 0.40, "escalation": 0.12},
    "escaping": {"danger": 0.35},
    "looting": {"discovery": 0.35},
    "mining": {"grind": 0.22},
    "farming": {"grind": 0.22},
    "building": {"grind": 0.18, "preparation": 0.12},
    "crafting": {"preparation": 0.30},
    "enchanting": {"preparation": 0.30},
    "brewing": {"preparation": 0.30},
    "trading": {"preparation": 0.22},
    "searching": {"travel": 0.18, "discovery": 0.14},
    "exploring": {"travel": 0.20, "discovery": 0.16},
    "talking": {"plan_explained": 0.16},
    "idle": {"travel": 0.10},
}

#: What each audio event type says about it. Measured events are worth more
#: than guessed ones, and the guessed ones all start with ``possible_``.
_AUDIO_SCORES = {
    "possible_laughter": {"joke": 0.45},
    "possible_scream": {"danger": 0.40},
    "sudden_reaction": {"discovery": 0.20, "danger": 0.15, "joke": 0.10},
    "loudness_spike": {"escalation": 0.15, "danger": 0.10},
    "speech_dense": {"plan_explained": 0.20, "objective_stated": 0.10},
    "speech_sparse": {"travel": 0.14, "grind": 0.12},
    "silence": {"travel": 0.14, "grind": 0.10},
    "long_pause": {"travel": 0.12, "grind": 0.10},
    "low_energy": {"travel": 0.14, "grind": 0.12},
    "music_region": {"outro": 0.10, "travel": 0.08},
}

#: What each transcript cue family says about it.
_CUE_SCORES = {
    "objective": {"objective_stated": 0.50},
    "plan": {"plan_explained": 0.45},
    "explanation": {"plan_explained": 0.30},
    "discovery": {"discovery": 0.45, "reveal": 0.20},
    "payoff": {"payoff": 0.50},
    "failure": {"failure": 0.50},
    "recovery": {"recovery": 0.45},
    "danger": {"danger": 0.45},
    "joke": {"joke": 0.40},
    "callback": {"callback": 0.50},
    "outro": {"outro": 0.60, "resolution": 0.20},
    "preparation": {"preparation": 0.40},
    "escalation": {"escalation": 0.40},
}

#: What a Session 2 recommendation's intended effect hints at. Small, because
#: a recommendation is corroboration rather than an observation -- it was
#: derived from the same three channels this detector is already reading.
_EFFECT_SCORES = {
    "comedy": {"joke": 0.10},
    "tension": {"danger": 0.10, "escalation": 0.08},
    "payoff": {"payoff": 0.10},
    "anticipation": {"escalation": 0.10},
    "explanation": {"plan_explained": 0.10},
    "impact": {"discovery": 0.06, "danger": 0.06},
}

#: Position priors. Not a channel: where a slot falls says something about what
#: it probably is, but it is not an observation of the footage.
_OPENING = 0.08
_CLOSING = 0.90


def _add(table: dict, contributions: dict, weight: float = 1.0) -> None:
    for kind, value in contributions.items():
        table[kind] = table.get(kind, 0.0) + value * weight


def _score_slot(slot, track: EpisodeTrack, recommendations=None) -> tuple:
    """``(scores, channels, evidence)`` for one slot.

    ``channels`` is the set of *observation* channels that actually
    contributed, which is what caps the confidence downstream. A channel that
    is present but said nothing about any beat kind does not count.
    """
    scores: dict[str, float] = {}
    channels: set = set()
    evidence = EpisodeEvidence(
        segment_ids=[slot.segment_id],
        placement_ids=[slot.placement_id] if slot.placement_id else [],
    )

    # -- what was seen ----------------------------------------------------
    if slot.events:
        before = dict(scores)
        _add(scores, _IMPORTANCE_SCORES.get(slot.importance, {}))
        for action in slot.actions:
            _add(scores, _ACTION_SCORES.get(action, {}))
        if slot.threats:
            _add(scores, {"danger": 0.25})
        if slot.entities:
            _add(scores, {"discovery": 0.10})
        for event in slot.events:
            if event.ui.death_screen:
                _add(scores, {"failure": 0.45})
            if event.ui.low_health:
                _add(scores, {"danger": 0.25})
            if event.ui.achievement_toast:
                _add(scores, {"payoff": 0.25})
            if event.ui.any_screen_open:
                _add(scores, {"preparation": 0.12})
        if scores != before:
            channels.add("visual")
            evidence.visual_event_ids.extend(slot.visual_event_ids)

    # -- what was measured in the sound ------------------------------------
    if slot.audio_events:
        before = dict(scores)
        for event in slot.audio_events:
            _add(scores, _AUDIO_SCORES.get(event.type, {}),
                 weight=max(0.3, event.confidence))
        if scores != before:
            channels.add("audio")
            evidence.audio_event_ids.extend(slot.audio_event_ids)
            evidence.audio_types = sorted(slot.audio_types)

    # -- what was said -----------------------------------------------------
    quotes = slot.quotes()
    if quotes:
        before = dict(scores)
        hits = language.cue_hits(" ".join(quotes))
        for family in hits:
            _add(scores, _CUE_SCORES.get(family, {}))
        if scores != before:
            channels.add("transcript")
            evidence.quotes.extend(quotes[:2])

    # -- what an earlier pass already proposed ------------------------------
    if recommendations is not None:
        for rec in getattr(recommendations, "recommendations", []):
            if rec.status == "rejected":
                continue
            if rec.asset_id != slot.segment.asset_id:
                continue
            if rec.overlaps(slot.source_start, slot.source_end) <= 0:
                continue
            for effect in rec.effects:
                _add(scores, _EFFECT_SCORES.get(effect, {}))
            if rec.recommendation_id not in evidence.recommendation_ids:
                evidence.recommendation_ids.append(rec.recommendation_id)

    # -- where it falls ----------------------------------------------------
    position = track.position((slot.start + slot.end) / 2.0)
    if position <= _OPENING:
        _add(scores, {"setup": 0.20})
    if position >= _CLOSING:
        _add(scores, {"outro": 0.20, "resolution": 0.15})

    return scores, channels, evidence


def _label(scores: dict) -> tuple:
    """``(kind, best, alternative, second)`` from a score table."""
    if not scores:
        return "unknown", 0.0, "", 0.0
    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    kind, best = ranked[0]
    alternative, second = (ranked[1] if len(ranked) > 1 else ("", 0.0))
    if best < MIN_BEAT_SCORE:
        return "unknown", best, kind, best
    return kind, best, alternative, second


def _apply_grind(slots: list, labels: list) -> None:
    """Relabel long runs of one repeatable action as a grind.

    Repetition is the only signal here that cannot be read off a single slot:
    twenty seconds of mining is fine, three minutes of it is the thing the
    viewer leaves during. So it is a second pass over the labels rather than a
    score inside ``_score_slot``.
    """
    start = 0
    while start < len(slots):
        action = slots[start].primary_action
        if action not in REPEATABLE_ACTIONS:
            start += 1
            continue
        end = start
        while (
            end + 1 < len(slots)
            and slots[end + 1].primary_action == action
        ):
            end += 1
        span = slots[end].end - slots[start].start
        count = end - start + 1
        if count >= GRIND_SLOTS or span >= GRIND_SECONDS:
            for index in range(start, end + 1):
                scores, channels, evidence = labels[index]
                # Travel is a grind when it goes on this long; a discovery in
                # the middle of one is still a discovery and keeps its label.
                _add(scores, {"grind": 0.35})
                channels.add("visual")
        start = end + 1


def _merge(beats: list) -> list:
    """Collapse adjacent beats doing the same job into one.

    Agreement across a merge raises confidence a little -- three consecutive
    slots all reading "grind" is a stronger claim than one -- but the cap still
    applies over the *union* of channels, so merging identical single-channel
    slots can never lift a keyword-only beat over the edit threshold.
    """
    if not beats:
        return []
    merged = [beats[0]]
    for beat in beats[1:]:
        last = merged[-1]
        if beat.kind == last.kind and beat.start - last.end <= MERGE_GAP:
            last.end = max(last.end, beat.end)
            last.evidence = last.evidence.merged(beat.evidence)
            last.span_count += beat.span_count
            for kind, value in beat.scores.items():
                last.scores[kind] = max(last.scores.get(kind, 0.0), value)
            agreement = 0.05 * (last.span_count - 1)
            last.confidence = capped(
                max(last.confidence, beat.confidence) + agreement,
                last.evidence.channels,
            )
            if beat.interest > last.interest:
                last.alternative = last.alternative or beat.alternative
            continue
        merged.append(beat)
    return merged


def _mark_climax(beats: list, track: EpisodeTrack) -> None:
    """Relabel the single biggest late moment as the climax.

    An observation, not an opinion, which is why it lives in the memory: "the
    highest-interest payoff-shaped beat in the back half" is a fact about the
    footage. Whether the episode should be *cut* to that moment is the
    retention plan's business.

    Nothing is relabelled when the peak is not clearly a peak. An episode whose
    top three moments are level does not have a climax, and inventing one would
    be exactly the kind of confident nonsense this layer exists to avoid.
    """
    eligible = [
        beat for beat in beats
        if beat.position >= 0.5 and beat.kind in CLIMAX_ELIGIBLE
    ]
    # "The biggest moment" needs something to be bigger than. An episode that
    # is one continuous beat has no peak, and marking that single beat as the
    # climax would be labelling the whole video as its own high point.
    if not eligible or len(beats) < 2:
        return

    top = max(eligible, key=lambda beat: beat.interest)
    # Measured against the other *late* candidates, not against the whole
    # episode. A mid-episode moment of equal intensity does not stop the back
    # half from having a peak -- an episode can find diamonds at the midpoint
    # and still climax on the boss fight -- so comparing against everything
    # would make this decline on almost every real cut.
    runner_up = max(
        (beat.interest for beat in eligible if beat is not top), default=0.0)
    if (top.interest < CLIMAX_MIN_INTEREST
            or top.interest - runner_up < CLIMAX_MIN_MARGIN):
        return
    top.notes = (
        f"relabelled from '{top.kind}': highest-interest beat in the back "
        f"half, {top.interest:.2f} against {runner_up:.2f} for the next"
    )
    top.alternative = top.kind
    top.kind = "climax"


def detect(
    track: EpisodeTrack, *, recommendations=None, layers=None
) -> list[EpisodeBeat]:
    """Every beat in the episode, merged, in order.

    ``layers`` is optional corroboration from Session 5: a styled plan that
    already put a title card somewhere agrees that something changed there. It
    can strengthen a beat and can never create one, because it was itself
    derived from the same evidence.
    """
    if track.is_empty:
        return []

    scored = [
        _score_slot(slot, track, recommendations) for slot in track.slots
    ]
    _apply_grind(list(track.slots), scored)

    if layers is not None:
        _corroborate(track, scored, layers)

    beats: list[EpisodeBeat] = []
    for slot, (scores, channels, evidence) in zip(track.slots, scored):
        kind, best, alternative, second = _label(scores)
        midpoint = (slot.start + slot.end) / 2.0
        beat = EpisodeBeat(
            item_id=new_id("beat", slot.segment_id, round(slot.start, 2), kind),
            start=slot.start,
            end=slot.end,
            kind=kind,
            alternative=alternative,
            scores={k: round(v, 3) for k, v in sorted(
                scores.items(), key=lambda pair: -pair[1])[:6]},
            position=track.position(midpoint),
            interest=slot.interest,
            evidence=evidence,
            confidence=capped(best, channels),
            why=_why(kind, best, second, channels),
            affects_edit=False,
        )
        beat.settle()
        beat.affects_edit = (
            beat.confidence >= MIN_EDIT_CONFIDENCE and kind != "unknown"
        )
        beat.needs_human_review = not beat.affects_edit
        beats.append(beat)

    merged = _merge(beats)
    for beat in merged:
        beat.position = track.position((beat.start + beat.end) / 2.0)
        beat.interest = _mean_interest(track, beat.start, beat.end)
    _mark_climax(merged, track)
    return merged


def _corroborate(track: EpisodeTrack, scored: list, layers) -> None:
    """Let a styled plan agree with a beat that was already detected.

    Only *agree*: the bonus is small and adds no channel, so a card the style
    pass placed cannot invent a beat where the footage shows nothing. Session 5
    read the same timeline this detector is reading.
    """
    items = getattr(layers, "items", None) or []
    for index, slot in enumerate(track.slots):
        scores, channels, evidence = scored[index]
        for item in items:
            if item.status != "planned":
                continue
            if max(0.0, min(item.end, slot.end) - max(item.start, slot.start)) <= 0:
                continue
            if item.kind in ("chapter_card", "title_card", "section_marker"):
                _add(scores, {"setup": 0.08, "objective_stated": 0.06})
            elif item.kind in ("punch_in", "impact_hit"):
                _add(scores, {"danger": 0.06, "payoff": 0.06})
            if item.item_id not in evidence.layer_item_ids:
                evidence.layer_item_ids.append(item.item_id)


def _mean_interest(track: EpisodeTrack, start: float, end: float) -> float:
    """Duration-weighted interest across a range."""
    covered = track.between(start, end)
    if not covered:
        return 0.0
    total = sum(slot.overlaps(start, end) for slot in covered)
    if total <= 0:
        return max(slot.interest for slot in covered)
    return round(sum(
        slot.interest * slot.overlaps(start, end) for slot in covered
    ) / total, 4)


def _why(kind: str, best: float, second: float, channels: set) -> str:
    if kind == "unknown":
        return (
            "no beat kind scored above the floor here; the slot is kept "
            "unlabelled rather than forced into the nearest one"
        )
    named = ", ".join(sorted(channels)) or "no observation channel"
    close = (
        f", but the runner-up was within {best - second:.2f}"
        if second and best - second < 0.1 else ""
    )
    return f"scored {best:.2f} on {named}{close}"


def summarise(beats: list) -> dict:
    """Counts by kind, for a report header."""
    out: dict[str, int] = {}
    for beat in beats:
        out[beat.kind] = out.get(beat.kind, 0) + 1
    return dict(sorted(out.items(), key=lambda pair: -pair[1]))


def kind_is_known(kind: str) -> bool:
    return kind in BEAT_KINDS and kind != "unknown"


def first_of(beats: list, *kinds: str) -> Optional[EpisodeBeat]:
    wanted = set(kinds)
    for beat in beats:
        if beat.kind in wanted:
            return beat
    return None
