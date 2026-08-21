"""Where the episode might lose someone, and why it might.

Thirteen detectors, each answering one question about the shape of the episode
rather than about any one moment. They read the memory -- beats, loops, setups,
the interest curve -- plus the track underneath it.

**This is not retention analytics.** Nothing here has seen an audience. Every
finding is a creative risk read off edit evidence: a three-minute grind, a goal
nobody states, a question asked eight minutes before it is answered. Those are
worth knowing and they are not predictions of a curve, which is why the word
"risk" appears everywhere and the word "will" appears nowhere.

Two rules keep the output conservative:

**A fix that changes timing is never automatic on inference.** Marker fixes are
always safe -- the worst case of a wrong marker is a marker in the wrong place.
A fix that shortens or retimes footage is automatic only when the risk it
answers is *measured* rather than inferred, which in practice means dead air
and nothing else. Boredom is a judgement; silence is a number.

**A detector that cannot see stays quiet.** Motion probing off means every
motion score is 0.0, and a low-visual-change detector that did not check would
fire on the whole episode. It checks.
"""
from __future__ import annotations

from typing import Optional

from editing.episode.schema import (
    EpisodeEvidence, EpisodeMemory, EpisodeRiskZone, MARKER_SUGGESTIONS,
    MEASURED_RISKS, MIN_EDIT_CONFIDENCE, TIMING_SUGGESTIONS, capped, new_id,
    severity_from,
)
from editing.episode.track import EpisodeTrack

#: A timing fix needs this much confidence *and* a measured risk behind it.
#: Set just under the two-channel cap (0.70) on purpose: dead air is seen by
#: the audio and the picture and never by the transcript, so two channels is
#: the most it can ever have, and a threshold above 0.70 would mean "no timing
#: fix is ever automatic" while looking like it meant something else.
AUTO_TIMING_CONFIDENCE = 0.68

#: A run of quiet this long is worth flagging.
BORING_SECONDS = 75.0

#: Explanation over static footage, past this, is a lecture.
EXPLAIN_SECONDS = 45.0

#: Measured silence adding up to this much inside one window.
DEAD_AIR_SECONDS = 12.0
DEAD_AIR_WINDOW = 60.0

#: Motion below this reads as a static picture.
MOTION_FLOOR = 0.12
LOW_MOTION_SECONDS = 60.0

#: This long with nothing at stake.
NO_STAKES_SECONDS = 120.0

#: A question held longer than this is being held too long.
PAYOFF_PATIENCE = 300.0

#: The opening a hook has to live in.
HOOK_WINDOW = 30.0

#: Interest the opening has to reach to not be a weak hook.
HOOK_FLOOR = 0.55

#: The middle band checked for a slump.
SLUMP_BAND = (0.35, 0.65)

#: How far below the episode mean the middle has to sit.
SLUMP_MARGIN = 0.12

#: The closing window checked for an ending.
ENDING_WINDOW = 45.0

#: Fix per risk. Every one of these is a *suggestion*; none is an operation.
FIX_FOR = {
    "boring_repetition": "speed_up_grind",
    "no_clear_objective": "clarify_objective",
    "overlong_explanation": "shorten_boring",
    "confusing_transition": "add_card",
    "dead_air": "shorten_boring",
    "low_visual_change": "add_teaser_marker",
    "no_stakes": "add_goal_marker",
    "payoff_delayed": "add_teaser_marker",
    "unresolved_setup": "needs_human_review",
    "weak_hook": "needs_human_review",
    "mid_video_slump": "add_goal_marker",
    "anticlimax": "mark_climax",
    "unclear_ending": "mark_ending_payoff",
}


def is_auto_safe(risk: str, fix: str, confidence: float) -> bool:
    """Whether a fix can be applied without a person looking first.

    One function so the rule cannot drift between detectors, and so a test can
    assert on it directly rather than on thirteen call sites.
    """
    if confidence < MIN_EDIT_CONFIDENCE:
        return False
    if fix in MARKER_SUGGESTIONS:
        return True
    if fix in TIMING_SUGGESTIONS:
        return risk in MEASURED_RISKS and confidence >= AUTO_TIMING_CONFIDENCE
    return False


def _zone(
    risk: str,
    start: float,
    end: float,
    *,
    score: float,
    channels: set,
    why: str,
    evidence: EpisodeEvidence,
    beat_ids: Optional[list] = None,
    marker: str = "",
    fix: Optional[str] = None,
) -> EpisodeRiskZone:
    chosen = fix or FIX_FOR.get(risk, "needs_human_review")
    confidence = capped(score, channels)
    zone = EpisodeRiskZone(
        item_id=new_id("risk", risk, round(start, 2), round(end, 2)),
        start=start,
        end=end,
        risk=risk,
        severity=severity_from(score),
        score=min(1.0, max(0.0, score)),
        suggested_fix=chosen,
        marker_fallback=marker or f"risk: {risk.replace('_', ' ')}",
        beat_ids=list(beat_ids or []),
        evidence=evidence,
        confidence=confidence,
        why=why,
    )
    zone.fix_is_safe_automatically = is_auto_safe(risk, chosen, confidence)
    zone.affects_edit = zone.fix_is_safe_automatically
    # ``settle`` may lower the confidence below the edit threshold once the
    # channel cap is applied, which un-sets ``affects_edit``. Reading the
    # flag back afterwards keeps the two from disagreeing -- a zone that
    # says its fix is automatic while saying it does not affect the edit
    # would be acted on by whichever consumer read the friendlier field.
    zone.settle()
    zone.fix_is_safe_automatically = zone.affects_edit
    return zone


def _evidence_over(track: EpisodeTrack, start: float, end: float) -> EpisodeEvidence:
    covered = track.between(start, end)
    return EpisodeEvidence(
        segment_ids=[slot.segment_id for slot in covered][:60],
        visual_event_ids=[
            event_id for slot in covered for event_id in slot.visual_event_ids
        ][:60],
        audio_event_ids=[
            event_id for slot in covered for event_id in slot.audio_event_ids
        ][:60],
        audio_types=sorted({
            kind for slot in covered for kind in slot.audio_types
        }),
        quotes=track.quotes_between(start, end, limit=3),
        placement_ids=sorted({
            slot.placement_id for slot in covered if slot.placement_id
        })[:20],
    )


def _channels_over(track: EpisodeTrack, start: float, end: float) -> set:
    covered = track.between(start, end)
    channels: set = set()
    if any(slot.events for slot in covered):
        channels.add("visual")
    if any(slot.audio_events for slot in covered):
        channels.add("audio")
    if any(slot.quotes() for slot in covered):
        channels.add("transcript")
    return channels


# ---------------------------------------------------------------------------
# The detectors
# ---------------------------------------------------------------------------

def boring_repetition(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """Long runs of the same low-interest thing.

    Reads merged beats rather than slots, because the merge already answered
    "is this the same thing continuing" and re-deriving it here would be a
    second, differently-wrong answer to the same question.
    """
    out: list[EpisodeRiskZone] = []
    runs: list[list] = []
    current: list = []
    for beat in memory.beats:
        if beat.is_quiet and beat.interest < 0.35:
            current.append(beat)
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)

    for run in runs:
        start, end = run[0].start, run[-1].end
        span = end - start
        if span < BORING_SECONDS:
            continue
        interest = sum(
            beat.interest * beat.duration for beat in run
        ) / max(1e-6, sum(beat.duration for beat in run))
        # Longer and flatter is worse, but the scale tops out: a five-minute
        # grind and a nine-minute one are the same finding.
        score = min(0.95, 0.30 + (span - BORING_SECONDS) / 240.0
                    + (0.35 - interest))
        retimed = any(
            slot.speed > 1.2 for slot in track.between(start, end)
        )
        why = (
            f"{span:.0f}s of {run[0].kind} with an average interest of "
            f"{interest:.2f}"
        )
        if retimed:
            why += "; the rough cut already speeds part of this up"
        out.append(_zone(
            "boring_repetition", start, end,
            score=score * (0.7 if retimed else 1.0),
            channels=_channels_over(track, start, end),
            why=why,
            evidence=_evidence_over(track, start, end),
            beat_ids=[beat.item_id for beat in run],
            marker=f"boring: {span:.0f}s of {run[0].kind}",
        ))
    return out


def no_clear_objective(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """Nobody ever says what this video is for.

    Scoped to the opening rather than the whole episode: the fix is a line or a
    card near the start, and a risk zone spanning the entire runtime is not
    something anyone can act on.
    """
    objective = memory.main_objective
    if objective is not None and objective.status not in ("implied", "unknown"):
        return []
    end = min(track.duration, max(HOOK_WINDOW, track.duration * 0.15))
    if objective is None:
        score, why = 0.75, (
            "no goal is stated or implied anywhere; a viewer has nothing to "
            "find out by staying"
        )
    else:
        score, why = 0.50, (
            f"no goal is stated out loud -- {objective.text} is inferred from "
            "what the player spends time doing, which the viewer cannot see"
        )
    return [_zone(
        "no_clear_objective", 0.0, end,
        score=score,
        channels=_channels_over(track, 0.0, end),
        why=why,
        evidence=_evidence_over(track, 0.0, end),
        marker="no stated objective",
    )]


def overlong_explanation(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """Talking, at length, over footage that is not doing anything."""
    out: list[EpisodeRiskZone] = []
    for beat in memory.beats:
        if beat.kind not in ("plan_explained", "objective_stated"):
            continue
        if beat.duration < EXPLAIN_SECONDS:
            continue
        if beat.interest >= 0.45:
            continue
        span = beat.duration
        score = min(0.9, 0.32 + (span - EXPLAIN_SECONDS) / 120.0)
        out.append(_zone(
            "overlong_explanation", beat.start, beat.end,
            score=score,
            channels=_channels_over(track, beat.start, beat.end),
            why=(
                f"{span:.0f}s of explanation over footage scoring "
                f"{beat.interest:.2f}"
            ),
            evidence=beat.evidence,
            beat_ids=[beat.item_id],
            marker=f"long explanation: {span:.0f}s",
        ))
    return out


def confusing_transition(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """A cut that drops the viewer somewhere new with no bridge.

    Three things have to be true: the place changes, no travel or setup beat
    covers the change, and nothing is said across it. Any one of them alone is
    normal editing; all three together is the viewer wondering where they are.
    """
    out: list[EpisodeRiskZone] = []
    previous = None
    for slot in track.slots:
        place = slot.environment
        if previous is None or place in ("", "unknown"):
            if place not in ("", "unknown"):
                previous = slot
            continue
        if place == previous.environment:
            previous = slot
            continue

        beat = memory.beat_at(slot.start)
        bridged = beat is not None and beat.kind in (
            "travel", "setup", "reveal", "plan_explained", "objective_stated")
        spoken = bool(previous.quotes()) and bool(slot.quotes())
        same_clip = (
            previous.placement_id and previous.placement_id == slot.placement_id
        )
        if bridged or spoken or same_clip:
            previous = slot
            continue

        start, end = previous.end - 2.0, slot.start + 3.0
        out.append(_zone(
            "confusing_transition", max(0.0, start), min(track.duration, end),
            score=0.42,
            channels=_channels_over(track, max(0.0, start), end),
            why=(
                f"the cut goes from {previous.environment} to {place} with "
                "nothing said across it and no travel beat in between"
            ),
            evidence=_evidence_over(track, max(0.0, start), end),
            marker=f"{previous.environment} -> {place}: unbridged cut",
        ))
        previous = slot
    return out


def dead_air(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """Measured silence, aggregated.

    The one detector here whose evidence is a number rather than a judgement,
    which is why it is the one whose fix can be automatic.
    """
    out: list[EpisodeRiskZone] = []
    window_start = 0.0
    while window_start < track.duration:
        window_end = min(track.duration, window_start + DEAD_AIR_WINDOW)
        quiet = 0.0
        for slot in track.between(window_start, window_end):
            for event in slot.audio_events:
                if event.type not in ("silence", "long_pause"):
                    continue
                quiet += max(0.0, min(event.end, slot.source_end)
                             - max(event.start, slot.source_start))
        if quiet >= DEAD_AIR_SECONDS:
            share = quiet / max(1e-6, window_end - window_start)
            out.append(_zone(
                "dead_air", window_start, window_end,
                score=min(0.92, 0.40 + share),
                channels=_channels_over(track, window_start, window_end),
                why=(
                    f"{quiet:.0f}s of measured silence in a "
                    f"{window_end - window_start:.0f}s window ({share:.0%})"
                ),
                evidence=_evidence_over(track, window_start, window_end),
                marker=f"dead air: {quiet:.0f}s",
            ))
        window_start = window_end
    return out


def low_visual_change(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """The picture stops moving.

    Returns nothing at all when motion was never probed. A zero motion score
    means "not measured" as often as it means "nothing moved", and a detector
    that cannot tell those apart should say nothing rather than flag the whole
    episode.
    """
    if not track.has_motion:
        return []
    out: list[EpisodeRiskZone] = []
    run: list = []
    for slot in list(track.slots) + [None]:
        static = slot is not None and slot.motion < MOTION_FLOOR
        if static:
            run.append(slot)
            continue
        if run:
            start, end = run[0].start, run[-1].end
            if end - start >= LOW_MOTION_SECONDS:
                out.append(_zone(
                    "low_visual_change", start, end,
                    score=min(0.85, 0.34 + (end - start) / 300.0),
                    channels=_channels_over(track, start, end),
                    why=(
                        f"{end - start:.0f}s where the strongest measured "
                        f"motion was {max(s.motion for s in run):.2f}"
                    ),
                    evidence=_evidence_over(track, start, end),
                    marker=f"static picture: {end - start:.0f}s",
                ))
            run = []
    return out


def no_stakes(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """A long stretch where nothing could go wrong.

    An unresolved open loop counts as stakes even when the footage is calm --
    the viewer is waiting for an answer, which is a reason to stay. So a
    stretch only qualifies when there is no danger *and* no question hanging.
    """
    out: list[EpisodeRiskZone] = []
    tense = [
        beat for beat in memory.beats
        if beat.kind in ("danger", "escalation", "failure", "climax")
    ]
    starts = [0.0] + [beat.end for beat in tense]
    ends = [beat.start for beat in tense] + [track.duration]

    for start, end in zip(starts, ends):
        if end - start < NO_STAKES_SECONDS:
            continue
        pending = [
            loop for loop in memory.open_loops
            if loop.start <= start and (
                loop.resolved_at is None or loop.resolved_at >= end
            )
        ]
        if pending:
            continue
        out.append(_zone(
            "no_stakes", start, end,
            score=min(0.85, 0.32 + (end - start - NO_STAKES_SECONDS) / 300.0),
            channels=_channels_over(track, start, end),
            why=(
                f"{end - start:.0f}s with no danger beat and no question left "
                "hanging, so there is nothing the viewer is waiting on"
            ),
            evidence=_evidence_over(track, start, end),
            marker=f"no stakes: {end - start:.0f}s",
        ))
    return out


def payoff_delayed(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """A question the episode makes the viewer hold too long."""
    out: list[EpisodeRiskZone] = []
    for loop in memory.open_loops:
        if loop.resolved_at is None:
            continue
        wait = loop.wait_seconds
        if wait < PAYOFF_PATIENCE:
            continue
        out.append(_zone(
            "payoff_delayed", loop.end, loop.resolved_at,
            score=min(0.85, 0.35 + (wait - PAYOFF_PATIENCE) / 600.0),
            channels=set(loop.evidence.channels),
            why=(
                f"the question '{loop.question[:50]}' waits "
                f"{wait / 60.0:.1f} minutes for its answer"
            ),
            evidence=loop.evidence,
            marker=f"payoff {wait / 60.0:.1f}min away",
        ))
    return out


def unresolved_setup(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """Something planted and never spent.

    Only the loops, not every setup. A setup with no payoff is common and
    usually fine; a *question asked out loud* and never answered is the one a
    viewer notices, and flagging both would bury the second in the first.
    """
    out: list[EpisodeRiskZone] = []
    for loop in memory.open_loops:
        if loop.status != "open":
            continue
        if loop.confidence < 0.30:
            continue
        remaining = track.duration - loop.start
        out.append(_zone(
            "unresolved_setup", loop.start, loop.end,
            score=min(0.80, 0.34 + remaining / max(1.0, track.duration) * 0.4),
            channels=set(loop.evidence.channels),
            why=(
                f"'{loop.question[:60]}' is asked at {loop.start:.0f}s and "
                "nothing later in the episode is about it"
            ),
            evidence=loop.evidence,
            marker="unresolved: " + loop.question[:40],
        ))
    return out


def weak_hook(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """The opening carries no reason to keep watching."""
    end = min(track.duration, HOOK_WINDOW)
    if end <= 0:
        return []
    opening = track.between(0.0, end)
    if not opening:
        return []
    peak = max(slot.interest for slot in opening)
    strong = [
        beat for beat in memory.beats
        if beat.start < end and beat.kind in (
            "danger", "discovery", "reveal", "failure", "joke", "climax")
    ]
    stated = any(
        beat.start < end and beat.kind == "objective_stated"
        for beat in memory.beats
    )
    if peak >= HOOK_FLOOR or strong:
        return []
    score = 0.45 + (HOOK_FLOOR - peak)
    if stated:
        # A stated goal is not a hook, but it is a reason to stay.
        score -= 0.15
    return [_zone(
        "weak_hook", 0.0, end,
        score=max(0.0, score),
        channels=_channels_over(track, 0.0, end),
        why=(
            f"the first {end:.0f}s peak at {peak:.2f} interest with no "
            "danger, discovery, reveal or laugh in them"
        ),
        evidence=_evidence_over(track, 0.0, end),
        marker="weak opening",
    )]


def mid_video_slump(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """The middle is the flattest part of the episode.

    Relative *and* absolute: the middle has to be both below the episode's own
    average by a margin and below an absolute floor. Relative alone would flag
    the middle of every well-paced episode, since something has to be lowest.
    """
    if track.duration <= 0 or len(memory.beats) < 3:
        return []
    start = track.duration * SLUMP_BAND[0]
    end = track.duration * SLUMP_BAND[1]
    middle = track.between(start, end)
    if not middle:
        return []
    everything = list(track.slots)
    mean_all = sum(slot.interest for slot in everything) / len(everything)
    mean_mid = sum(slot.interest for slot in middle) / len(middle)
    if mean_mid >= 0.40 or mean_all - mean_mid < SLUMP_MARGIN:
        return []
    return [_zone(
        "mid_video_slump", start, end,
        score=min(0.85, 0.36 + (mean_all - mean_mid)),
        channels=_channels_over(track, start, end),
        why=(
            f"the middle averages {mean_mid:.2f} interest against "
            f"{mean_all:.2f} across the episode"
        ),
        evidence=_evidence_over(track, start, end),
        marker=f"mid slump: {mean_mid:.2f} vs {mean_all:.2f}",
    )]


def anticlimax(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """The biggest moment happens early and nothing tops it."""
    if not memory.beats or track.duration <= 0:
        return []
    peak = max(memory.beats, key=lambda beat: beat.interest)
    if peak.interest < 0.5:
        return []
    if peak.position >= 0.5:
        return []
    tail = [
        beat for beat in memory.beats
        if beat.position >= 0.75
    ]
    if not tail:
        return []
    tail_peak = max(beat.interest for beat in tail)
    if tail_peak >= peak.interest - 0.1:
        return []
    return [_zone(
        "anticlimax", track.duration * 0.75, track.duration,
        score=min(0.80, 0.36 + (peak.interest - tail_peak)),
        channels=_channels_over(track, track.duration * 0.75, track.duration),
        why=(
            f"the strongest moment is at {peak.start:.0f}s "
            f"({peak.interest:.2f}) and the last quarter peaks at "
            f"{tail_peak:.2f}"
        ),
        evidence=peak.evidence,
        beat_ids=[peak.item_id],
        marker="peak is early; ending is flatter",
    )]


def unclear_ending(
    memory: EpisodeMemory, track: EpisodeTrack
) -> list[EpisodeRiskZone]:
    """The episode stops rather than ends."""
    if track.duration <= 0:
        return []
    start = max(0.0, track.duration - ENDING_WINDOW)
    closing = [
        beat for beat in memory.beats
        if beat.end > start and beat.kind in (
            "resolution", "outro", "payoff", "climax")
    ]
    if closing:
        return []
    return [_zone(
        "unclear_ending", start, track.duration,
        score=0.50,
        channels=_channels_over(track, start, track.duration),
        why=(
            f"the last {track.duration - start:.0f}s contain no resolution, "
            "payoff or sign-off; the episode ends on whatever was happening"
        ),
        evidence=_evidence_over(track, start, track.duration),
        marker="no clear ending",
    )]


#: Every detector, in the order they run. A table rather than a function body
#: so a caller can run one of them, and so the list is auditable in one screen.
DETECTORS = (
    ("weak_hook", weak_hook),
    ("no_clear_objective", no_clear_objective),
    ("boring_repetition", boring_repetition),
    ("overlong_explanation", overlong_explanation),
    ("dead_air", dead_air),
    ("low_visual_change", low_visual_change),
    ("confusing_transition", confusing_transition),
    ("no_stakes", no_stakes),
    ("payoff_delayed", payoff_delayed),
    ("unresolved_setup", unresolved_setup),
    ("mid_video_slump", mid_video_slump),
    ("anticlimax", anticlimax),
    ("unclear_ending", unclear_ending),
)


def detect(memory: EpisodeMemory, track: EpisodeTrack) -> list[EpisodeRiskZone]:
    """Every risk in the episode, worst first.

    A detector that raises is a bug, but it is a bug that must not cost you the
    other twelve findings, so it is caught and turned into a warning-shaped
    zone rather than allowed to end the pass.
    """
    out: list[EpisodeRiskZone] = []
    for name, detector in DETECTORS:
        try:
            out.extend(detector(memory, track))
        except Exception as exc:  # noqa: BLE001 - one detector must not sink the rest
            zone = EpisodeRiskZone(
                item_id=new_id("risk", "failed", name),
                start=0.0, end=0.0, risk="unresolved_setup", severity="low",
                suggested_fix="needs_human_review",
                why=f"the {name} detector failed: {exc}",
                confidence=0.0,
            )
            zone.notes = "detector error; the other detectors still ran"
            out.append(zone)
    out.sort(key=lambda zone: (-zone.score, zone.start))
    return out
