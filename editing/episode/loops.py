"""Questions the episode asks, and whether it ever answers them.

An open loop is the thing that makes someone watch the next thirty seconds:
"can we get diamonds", "will this plan work", "what is in that structure". This
module finds them, then goes looking for the moment that closes each one.

The hard part is not finding questions -- it is refusing to claim a question
was answered when it was not. So resolution is **topical**, not positional: a
payoff later in the episode does not resolve an earlier loop unless the two are
about the same thing, and "about the same thing" means shared content words
with at least one of them being a word that identifies a thread rather than a
word that appears in every sentence. Anything weaker is recorded as a
*candidate* payoff -- a place to look -- and the loop stays open.

The three outcomes are deliberately distinct:

``resolved``
    A later moment shares a salient word with the question and reads as a
    payoff. Two channels agreed.
``possibly_resolved``
    A topical link exists but is weak, or only one channel saw it.
``open``
    Nothing matched. Which is a finding, not a failure: an unresolved setup is
    one of the most useful things this layer can tell you.
"""
from __future__ import annotations

from typing import Optional

from editing.episode import language
from editing.episode.schema import (
    EpisodeCallback, EpisodeEvidence, EpisodeOpenLoop, EpisodePayoff,
    EpisodeSetup, MIN_EDIT_CONFIDENCE, PAYOFF_BEATS, capped, new_id,
)
from editing.episode.track import EpisodeTrack

#: A topical match this strong, with a shared salient word, closes a loop.
STRONG_MATCH = 0.45

#: This strong is a candidate worth naming, and nothing more.
WEAK_MATCH = 0.20

#: A loop cannot be closed by something before it, obviously -- but it also
#: cannot be closed by something a second after it. A question and its answer
#: in the same breath is one statement, not a loop.
MIN_RESOLUTION_GAP = 8.0

#: Two mentions closer together than this are the same conversation, not a
#: callback to an earlier one.
MIN_CALLBACK_GAP = 60.0

#: A setup and its payoff further apart than this are worth flagging: the
#: viewer has probably forgotten, which is what a callback caption is for.
LONG_GAP = 240.0

#: Cue families that open a question about something.
_OPENING_CUES = ("objective", "plan", "preparation", "escalation")

#: Cue families that read as an answer.
_CLOSING_CUES = ("payoff", "discovery", "failure", "recovery")


def _slot_topic(slot) -> list[str]:
    """What a slot is about: what was said, plus what was on screen."""
    spoken = language.topic(" ".join(slot.quotes()))
    seen = [
        token for token in (
            list(slot.entities) + list(slot.threats)
            + ([slot.environment] if slot.environment != "unknown" else [])
        )
        if token
    ]
    out = list(spoken)
    for token in seen:
        normalised = token.lower().replace(" ", "_")
        if normalised not in out:
            out.append(normalised)
    return out[:12]


def _evidence_for(slot, *, quotes: Optional[list] = None) -> EpisodeEvidence:
    return EpisodeEvidence(
        segment_ids=[slot.segment_id],
        visual_event_ids=list(slot.visual_event_ids),
        audio_event_ids=list(slot.audio_event_ids),
        audio_types=sorted(slot.audio_types),
        quotes=list(quotes if quotes is not None else slot.quotes(2)),
        placement_ids=[slot.placement_id] if slot.placement_id else [],
    )


# ---------------------------------------------------------------------------
# Open loops
# ---------------------------------------------------------------------------

def _why_cares(kind: str, topic: list[str]) -> str:
    subject = topic[0].replace("_", " ") if topic else "this"
    if kind == "question":
        return (
            f"the episode asks something about {subject} out loud, and a "
            "viewer who wants the answer has a reason to keep watching"
        )
    if kind == "objective":
        return (
            f"a goal involving {subject} is stated, which sets up an "
            "expectation that the episode either meets or does not"
        )
    return (
        f"something about {subject} is set up here that the episode can "
        "spend later"
    )


def find_open_loops(track: EpisodeTrack) -> list[EpisodeOpenLoop]:
    """Every question the episode raises, in order.

    A loop needs a topic. "What?" raises nothing trackable, so it is skipped
    rather than recorded as an unanswerable open loop that would then show up
    forever in the unresolved list.
    """
    loops: list[EpisodeOpenLoop] = []
    for slot in track.slots:
        for line in slot.quotes(limit=4):
            topic = language.topic(line)
            if not topic:
                continue
            hits = language.cue_hits(line)
            question = language.is_question(line)
            opening = [family for family in _OPENING_CUES if family in hits]
            if not question and not opening:
                continue

            kind = "question" if question else "objective"
            score = 0.34 if question else 0.30
            channels = {"transcript"}
            evidence = _evidence_for(slot, quotes=[line])

            # The picture agreeing about the subject is a second channel, and
            # it is what lifts a loop out of "someone said a sentence".
            seen = [
                token.lower() for token in
                list(slot.entities) + list(slot.threats)
            ]
            if any(token in " ".join(topic) or token in topic for token in seen):
                score += 0.20
                channels.add("visual")
            elif slot.events and slot.importance in ("setup", "tension",
                                                     "danger"):
                score += 0.10
                channels.add("visual")
            if slot.has_reaction:
                score += 0.10
                channels.add("audio")

            loop = EpisodeOpenLoop(
                item_id=new_id("loop", slot.segment_id, round(slot.start, 2),
                               " ".join(topic[:3])),
                start=slot.start,
                end=slot.end,
                question=language.condense(line, limit=140),
                topic=topic,
                why_viewer_cares=_why_cares(kind, topic),
                status="open",
                evidence=evidence,
                confidence=capped(score, channels),
                why=(
                    "a question was asked out loud" if question
                    else "a goal was stated out loud"
                ),
                suggested_use="keep_setup",
            )
            loop.settle()
            loops.append(loop)
    return _dedupe_loops(loops)


def _dedupe_loops(loops: list) -> list:
    """Drop a loop that restates one already open on the same topic.

    Someone saying "we need diamonds" three times in a minute has one goal,
    not three. The first mention wins because that is where the viewer's
    question actually starts, and the later ones become callbacks elsewhere.
    """
    kept: list[EpisodeOpenLoop] = []
    for loop in loops:
        duplicate = None
        for existing in kept:
            if loop.start - existing.start > LONG_GAP:
                continue
            if language.topic_overlap(existing.topic, loop.topic) >= 0.6:
                duplicate = existing
                break
        if duplicate is None:
            kept.append(loop)
            continue
        duplicate.evidence = duplicate.evidence.merged(loop.evidence)
        duplicate.confidence = capped(
            duplicate.confidence + 0.05, duplicate.evidence.channels)
        duplicate.notes = (
            duplicate.notes
            or "restated later in the episode; the first mention is kept"
        )
    return kept


def resolve_loops(
    loops: list, track: EpisodeTrack, beats: list
) -> list[EpisodeOpenLoop]:
    """Look for the moment that answers each loop. Mutates and returns ``loops``.

    A loop closes only on a topical match with a shared salient word. Everything
    weaker becomes a candidate payoff and leaves the loop open, because an
    unresolved loop reported honestly is useful and a resolved loop reported
    wrongly makes the whole artifact untrustworthy.
    """
    answers = _answer_moments(track, beats)
    for loop in loops:
        best: Optional[tuple] = None
        candidates: list[float] = []
        for moment in answers:
            if moment["start"] < loop.end + MIN_RESOLUTION_GAP:
                continue
            overlap = language.topic_overlap(loop.topic, moment["topic"])
            salient = language.shared_salient(loop.topic, moment["topic"])
            if overlap >= WEAK_MATCH:
                candidates.append(round(moment["start"], 3))
            strength = overlap + (0.15 if salient else 0.0)
            if best is None or strength > best[0]:
                if overlap >= WEAK_MATCH:
                    best = (strength, moment, overlap, salient)

        loop.candidate_payoffs = candidates[:6]

        if best is None:
            loop.status = "open"
            loop.why = (
                f"{loop.why}; nothing later in the episode was about "
                f"{', '.join(loop.topic[:2]) or 'the same thing'}"
            )
            loop.suggested_use = "tease_payoff"
            loop.needs_human_review = True
            continue

        _, moment, overlap, salient = best
        loop.resolved_at = moment["start"]
        loop.resolution_id = moment["id"]
        loop.evidence = loop.evidence.merged(moment["evidence"])
        shared = ", ".join(salient) if salient else ", ".join(
            sorted(set(loop.topic) & set(moment["topic"]))[:3])
        if overlap >= STRONG_MATCH and salient:
            loop.status = "resolved"
            loop.resolution_reason = (
                f"a {moment['kind']} at {moment['start']:.1f}s shares "
                f"'{shared}' with the question"
            )
            loop.confidence = capped(
                loop.confidence + 0.15, loop.evidence.channels)
            loop.suggested_use = "keep_setup"
        else:
            loop.status = "possibly_resolved"
            loop.resolution_reason = (
                f"a {moment['kind']} at {moment['start']:.1f}s is loosely "
                f"about {shared or 'a related subject'}; the link is weak "
                "enough that a person should confirm it"
            )
            loop.needs_human_review = True
            loop.suggested_use = "needs_human_review"
        loop.settle()
    return loops


def _answer_moments(track: EpisodeTrack, beats: list) -> list[dict]:
    """Every moment that could close a loop, with what it is about."""
    out: list[dict] = []
    for beat in beats:
        if beat.kind not in PAYOFF_BEATS:
            continue
        covered = track.between(beat.start, beat.end)
        topic: list[str] = []
        for slot in covered:
            for token in _slot_topic(slot):
                if token not in topic:
                    topic.append(token)
        out.append({
            "id": beat.item_id,
            "kind": beat.kind,
            "start": beat.start,
            "end": beat.end,
            "topic": topic[:16],
            "evidence": beat.evidence,
        })

    # A payoff cue in the transcript is worth checking even where the beat
    # detector called the stretch something else -- the two disagree often
    # enough that only looking at labelled payoffs would miss real answers.
    for slot in track.slots:
        line = " ".join(slot.quotes())
        if not line:
            continue
        hits = language.cue_hits(line)
        if not any(family in hits for family in _CLOSING_CUES):
            continue
        if any(
            moment["start"] <= slot.start < moment["end"] for moment in out
        ):
            continue
        out.append({
            "id": new_id("answer", slot.segment_id, round(slot.start, 2)),
            "kind": "spoken payoff",
            "start": slot.start,
            "end": slot.end,
            "topic": _slot_topic(slot),
            "evidence": _evidence_for(slot),
        })

    out.sort(key=lambda moment: moment["start"])
    return out


# ---------------------------------------------------------------------------
# Setup and payoff
# ---------------------------------------------------------------------------

def find_setups(track: EpisodeTrack, beats: list) -> list[EpisodeSetup]:
    """Moments that plant something the episode can spend later."""
    setups: list[EpisodeSetup] = []
    for beat in beats:
        if beat.kind not in ("objective_stated", "plan_explained",
                             "preparation", "setup", "escalation", "danger"):
            continue
        covered = track.between(beat.start, beat.end)
        if not covered:
            continue
        topic: list[str] = []
        quotes: list[str] = []
        for slot in covered:
            for token in _slot_topic(slot):
                if token not in topic:
                    topic.append(token)
            quotes.extend(slot.quotes(1))
        if not topic:
            continue
        setup = EpisodeSetup(
            item_id=new_id("setup", beat.item_id),
            start=beat.start,
            end=beat.end,
            text=language.condense(
                quotes[0] if quotes else f"{beat.kind} at {beat.start:.0f}s",
                limit=140),
            topic=topic[:10],
            evidence=beat.evidence,
            confidence=beat.confidence,
            why=f"a {beat.kind.replace('_', ' ')} that the episode can spend later",
            suggested_use="keep_setup",
        )
        setup.settle()
        setups.append(setup)
    return setups


def find_payoffs(
    track: EpisodeTrack, beats: list, setups: list
) -> list[EpisodePayoff]:
    """Moments that spend an earlier setup, linked to the setup they spend.

    A payoff with no setup is not recorded here. That is not an oversight: this
    list exists to answer "did the thing that was planted ever land", and a
    good moment with nothing behind it is already in the beat list.
    """
    payoffs: list[EpisodePayoff] = []
    for beat in beats:
        if beat.kind not in PAYOFF_BEATS:
            continue
        covered = track.between(beat.start, beat.end)
        topic: list[str] = []
        quotes: list[str] = []
        for slot in covered:
            for token in _slot_topic(slot):
                if token not in topic:
                    topic.append(token)
            quotes.extend(slot.quotes(1))
        if not topic:
            continue

        best = None
        for setup in setups:
            if setup.end > beat.start - MIN_RESOLUTION_GAP:
                continue
            if setup.paid_off:
                continue
            overlap = language.topic_overlap(setup.topic, topic)
            salient = language.shared_salient(setup.topic, topic)
            strength = overlap + (0.15 if salient else 0.0)
            if overlap >= WEAK_MATCH and (best is None or strength > best[0]):
                best = (strength, setup, overlap, salient)
        if best is None:
            continue

        _, setup, overlap, salient = best
        shared = ", ".join(salient) if salient else ", ".join(
            sorted(set(setup.topic) & set(topic))[:3])
        gap = beat.start - setup.start
        payoff = EpisodePayoff(
            item_id=new_id("payoff", beat.item_id, setup.item_id),
            start=beat.start,
            end=beat.end,
            text=language.condense(
                quotes[0] if quotes else f"{beat.kind} at {beat.start:.0f}s",
                limit=140),
            topic=topic[:10],
            setup_id=setup.item_id,
            gap_seconds=gap,
            match_reason=(
                f"shares '{shared}' with the setup at {setup.start:.1f}s"
                if shared else
                f"weakly related to the setup at {setup.start:.1f}s"
            ),
            evidence=beat.evidence.merged(setup.evidence),
            confidence=capped(
                min(beat.confidence, setup.confidence)
                + (0.10 if overlap >= STRONG_MATCH and salient else 0.0),
                beat.evidence.merged(setup.evidence).channels,
            ),
            why=(
                f"lands a setup from {gap:.0f}s earlier"
                if gap < LONG_GAP else
                f"lands a setup from {gap / 60.0:.1f} minutes earlier, which a "
                "viewer has probably lost track of"
            ),
            suggested_use=(
                "add_callback_marker" if gap >= LONG_GAP
                else "use_as_ending_payoff"
            ),
        )
        payoff.settle()
        setup.payoff_id = payoff.item_id
        payoffs.append(payoff)
    return payoffs


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

def find_callbacks(
    track: EpisodeTrack, beats: list, motifs: Optional[list] = None
) -> list[EpisodeCallback]:
    """Places where the episode refers to something it did earlier.

    Three sources, strongest first: someone says so ("remember when"), a phrase
    is repeated, or the episode returns to a place or a threat it has already
    been. The third is the weakest and says so -- returning to a base is a
    callback opportunity, not a callback.
    """
    callbacks: list[EpisodeCallback] = []
    callbacks.extend(_spoken_callbacks(track))
    callbacks.extend(_phrase_callbacks(track))
    callbacks.extend(_return_callbacks(track))
    if motifs:
        callbacks.extend(_motif_callbacks(track, motifs))

    callbacks.sort(key=lambda item: item.start)
    return _dedupe_callbacks(callbacks)


def _spoken_callbacks(track: EpisodeTrack) -> list[EpisodeCallback]:
    out: list[EpisodeCallback] = []
    for slot in track.slots:
        for line in slot.quotes(limit=3):
            if "callback" not in language.cue_hits(line):
                continue
            topic = language.topic(line)
            earlier = _earliest_mention(track, topic, before=slot.start)
            channels = {"transcript"}
            score = 0.40
            if earlier is not None:
                score += 0.20
                channels.add("visual" if slot.events else "audio")
            callback = EpisodeCallback(
                item_id=new_id("callback", slot.segment_id,
                               round(slot.start, 2)),
                start=slot.start,
                end=slot.end,
                label=language.condense(line, limit=80),
                kind="phrase",
                refers_to_time=earlier if earlier is not None else 0.0,
                topic=topic,
                evidence=_evidence_for(slot, quotes=[line]),
                confidence=capped(score, channels),
                why=(
                    "someone refers back to something explicitly"
                    if earlier is not None else
                    "someone refers back to something, but the earlier moment "
                    "could not be located"
                ),
                suggested_use="add_callback_marker",
                suggested_text=language.condense(line, limit=60),
            )
            callback.settle()
            out.append(callback)
    return out


def _phrase_callbacks(track: EpisodeTrack) -> list[EpisodeCallback]:
    """A phrase said in one place and said again much later.

    The gap is what makes this a callback rather than a verbal tic, and the
    check is against the *first* occurrence so a phrase said four times gives
    three callbacks to one origin instead of a chain.
    """
    lines: list[tuple] = []
    for slot in track.slots:
        for line in slot.quotes(limit=3):
            lines.append((slot, line))

    repeated = language.repeated_phrases([line for _, line in lines])
    out: list[EpisodeCallback] = []
    for phrase, count in repeated[:12]:
        occurrences = [
            (slot, line) for slot, line in lines
            if phrase in language.normalise(line)
        ]
        if len(occurrences) < 2:
            continue
        origin = occurrences[0][0]
        for slot, line in occurrences[1:]:
            if slot.start - origin.start < MIN_CALLBACK_GAP:
                continue
            channels = {"transcript"}
            score = 0.32 + min(0.12, 0.04 * count)
            if slot.events and origin.events:
                score += 0.12
                channels.add("visual")
            callback = EpisodeCallback(
                item_id=new_id("callback", "phrase", phrase,
                               round(slot.start, 2)),
                start=slot.start,
                end=slot.end,
                label=phrase,
                kind="phrase",
                refers_to_time=origin.start,
                topic=language.topic(phrase),
                evidence=_evidence_for(slot, quotes=[line]).merged(
                    _evidence_for(origin, quotes=occurrences[0][1:2])),
                confidence=capped(score, channels),
                why=(
                    f"'{phrase}' was said {count} times across the episode, "
                    f"first at {origin.start:.0f}s"
                ),
                suggested_use="add_callback_marker",
                suggested_text=language.condense(phrase, limit=48),
            )
            callback.settle()
            out.append(callback)
    return out


def _return_callbacks(track: EpisodeTrack) -> list[EpisodeCallback]:
    """The episode going back somewhere it has already been.

    Weak on purpose. Returning to a base is an *opportunity* for a callback,
    not evidence that one happened, and it is labelled that way so nobody reads
    the list as a set of confirmed references.
    """
    out: list[EpisodeCallback] = []
    first_seen: dict[str, float] = {}
    last_seen: dict[str, float] = {}
    for slot in track.slots:
        place = slot.environment
        if place in ("unknown", ""):
            continue
        if place not in first_seen:
            first_seen[place] = slot.start
            last_seen[place] = slot.end
            continue
        if slot.start - last_seen[place] >= MIN_CALLBACK_GAP:
            callback = EpisodeCallback(
                item_id=new_id("callback", "place", place,
                               round(slot.start, 2)),
                start=slot.start,
                end=slot.end,
                label=place,
                kind="place",
                refers_to_time=first_seen[place],
                topic=[place],
                evidence=_evidence_for(slot),
                confidence=capped(0.30, {"visual"}),
                why=(
                    f"the episode returns to the {place} it was in at "
                    f"{first_seen[place]:.0f}s; that is an opportunity for a "
                    "callback rather than one that happened"
                ),
                suggested_use="add_callback_marker",
                suggested_text=f"back in the {place.replace('_', ' ')}",
            )
            callback.settle()
            out.append(callback)
        last_seen[place] = max(last_seen[place], slot.end)
    return out


def _motif_callbacks(track: EpisodeTrack, motifs: list) -> list[EpisodeCallback]:
    """Every occurrence of a running joke or repeated failure after the first."""
    out: list[EpisodeCallback] = []
    for motif in motifs:
        if motif.kind not in ("joke", "failure", "danger"):
            continue
        times = sorted(motif.occurrence_times)
        if len(times) < 2:
            continue
        origin = times[0]
        for when in times[1:]:
            if when - origin < MIN_CALLBACK_GAP:
                continue
            slot = track.slot_at(when)
            if slot is None:
                continue
            callback = EpisodeCallback(
                item_id=new_id("callback", motif.item_id, round(when, 2)),
                start=slot.start,
                end=slot.end,
                label=motif.label,
                kind=motif.kind,
                refers_to_time=origin,
                topic=language.topic(motif.label),
                evidence=_evidence_for(slot).merged(motif.evidence),
                confidence=capped(motif.confidence + 0.05,
                                  motif.evidence.channels),
                why=(
                    f"the '{motif.label}' {motif.kind} recurs here, having "
                    f"started at {origin:.0f}s"
                ),
                suggested_use="add_callback_marker",
                suggested_text=language.condense(motif.label, limit=48),
            )
            callback.settle()
            out.append(callback)
    return out


def _dedupe_callbacks(callbacks: list) -> list:
    """One callback per moment: the best-evidenced wins, the rest merge into it."""
    kept: list[EpisodeCallback] = []
    for callback in callbacks:
        existing = next(
            (item for item in kept if abs(item.start - callback.start) < 1.0),
            None,
        )
        if existing is None:
            kept.append(callback)
            continue
        if callback.confidence > existing.confidence:
            callback.evidence = callback.evidence.merged(existing.evidence)
            kept[kept.index(existing)] = callback
        else:
            existing.evidence = existing.evidence.merged(callback.evidence)
    return kept


def _earliest_mention(
    track: EpisodeTrack, topic: list, *, before: float
) -> Optional[float]:
    """When the episode first talked about this, before ``before``."""
    if not topic:
        return None
    for slot in track.slots:
        if slot.start >= before - MIN_CALLBACK_GAP:
            break
        if language.topic_overlap(topic, _slot_topic(slot)) >= STRONG_MATCH:
            return slot.start
    return None


def link_summary(loops: list, setups: list, payoffs: list) -> dict:
    """Counts for a report header."""
    return {
        "open_loops": len(loops),
        "resolved": sum(1 for loop in loops if loop.status == "resolved"),
        "possibly_resolved": sum(
            1 for loop in loops if loop.status == "possibly_resolved"),
        "still_open": sum(1 for loop in loops if loop.status == "open"),
        "setups": len(setups),
        "setups_paid_off": sum(1 for setup in setups if setup.paid_off),
        "payoffs": len(payoffs),
        "edit_affecting_loops": sum(
            1 for loop in loops
            if loop.confidence >= MIN_EDIT_CONFIDENCE
        ),
    }
