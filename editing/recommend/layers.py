"""The six recommendation layers.

Each layer is a pure function from timeline segments to recommendations. They
run in order and each one can see what the earlier ones proposed, which is what
makes the last layer able to do its job: the safety pass can only judge
over-editing if it can see the whole set at once.

    1. story    what kind of moment is this?
    2. pacing   should it be cut, trimmed, held, or re-timed?
    3. visual   does it want emphasis -- a punch-in, text, a marker?
    4. audio    does it want a music cue, an impact, a duck?
    5. polish   is anything wrong with it -- exposure, readability, framing?
    6. safety   which of the above is actually a bad idea?

Layers 1-5 are deliberately generous. They propose. Layer 6 is deliberately
strict, and it is where most of the quality comes from: it enforces a budget on
active edits, kills anything without cross-channel support, spaces repeats out,
and refuses to cover gameplay the viewer needs to see. An unedited moment costs
nothing; a bad edit costs the viewer's trust.

The house style is cinematic Minecraft: clean pacing, tension and payoff
respected, punch-ins that mean something, readable text, no spam.
"""
from __future__ import annotations

from typing import Optional, Sequence

from editing.recommend.schema import (
    ACTIVE_CATEGORIES, EditRecommendation, Evidence,
)
from editing.schema import IMPORTANCE_WEIGHT, TimelineSegment, short_hash

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

#: Importance levels that carry a video. Everything else is connective tissue.
HIGH_VALUE = frozenset({"payoff", "reveal", "danger", "funny"})

#: Importance levels worth building anticipation into.
TENSION_LEVELS = frozenset({"tension", "danger"})

#: A segment shorter than this cannot hold an emphasis edit -- a punch-in that
#: starts and ends inside a second reads as a glitch, not as emphasis.
MIN_EMPHASIS_SECONDS = 1.8

#: Minimum spacing between two active edits of the same category. Two punch-ins
#: four seconds apart look like a tic.
MIN_REPEAT_GAP = 12.0

#: Ceiling on active edits as a fraction of covered runtime, expressed as
#: "at most one active edit per N seconds of footage".
ACTIVE_EDIT_BUDGET_SECONDS = 20.0

#: A recommendation acting on a single channel needs to be at least this
#: confident to survive the safety pass.
SINGLE_CHANNEL_MIN_PRIORITY = 0.55


def _rid(segment: TimelineSegment, category: str) -> str:
    return "r_" + short_hash(
        segment.asset_id, round(segment.start, 3), round(segment.end, 3), category
    )


def _evidence(segment: TimelineSegment, *, summary: str = "") -> Evidence:
    """Cite everything this segment actually contains.

    Built once per segment and shared by that segment's recommendations,
    because they genuinely rest on the same records.
    """
    return Evidence(
        visual_event_ids=[event.event_id for event in segment.events if not event.error],
        transcript_quotes=[
            entry.text for entry in segment.speech_entries if entry.text
        ][:5],
        audio_event_ids=[event.event_id for event in segment.audio_events],
        audio_types=sorted(segment.audio_types()),
        summary=summary,
    )


def _make(
    segment: TimelineSegment,
    *,
    category: str,
    reason: str,
    layer: str,
    priority: float,
    intensity: str = "low",
    effects: Sequence[str] = (),
    risks: Sequence[str] = (),
    start: Optional[float] = None,
    end: Optional[float] = None,
    notes: str = "",
) -> EditRecommendation:
    return EditRecommendation(
        recommendation_id=_rid(segment, category),
        asset_id=segment.asset_id,
        source_file=segment.source_file,
        start=segment.start if start is None else start,
        end=segment.end if end is None else end,
        category=category,
        priority=max(0.0, min(1.0, priority)),
        reason=reason,
        evidence=_evidence(segment, summary=reason),
        intensity=intensity,
        effects=list(effects),
        risks=list(risks),
        layer=layer,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Layer 1 -- story / structure
# ---------------------------------------------------------------------------

def layer_story(segments: Sequence[TimelineSegment]) -> list[EditRecommendation]:
    """Identify what kind of moment each segment is.

    Produces markers and structure cuts rather than picture changes: this layer
    is about understanding, and its output is mostly what the later layers read
    to decide anything. Markers are cheap and land in Premiere as real timeline
    markers, so a human editor gets the structural read even if every other
    recommendation is rejected.
    """
    out: list[EditRecommendation] = []
    previous: Optional[TimelineSegment] = None

    for segment in segments:
        importance = segment.importance
        weight = IMPORTANCE_WEIGHT.get(importance, 0.3)

        if importance in HIGH_VALUE and not segment.is_dead_air:
            reaction = segment.audio_reaction
            reason = f"{importance.capitalize()} moment"
            if reaction is not None:
                reason += f", with audible {_sound_name(reaction.type)}"
            if segment.alignment == "contrast":
                reason += "; narration contrasts the picture"
            out.append(_make(
                segment, category="marker", layer="story",
                reason=reason,
                priority=min(1.0, weight + 0.1),
                effects=[_effect_for(importance)],
                notes=segment.alignment_reason,
            ))

        # A death or a death screen is always worth marking, whatever the
        # model scored the window at -- it is a hard structural beat.
        if any(event.ui.death_screen for event in segment.events) or (
            "dying" in {event.primary_action for event in segment.events}
        ):
            out.append(_make(
                segment, category="marker", layer="story",
                reason="Death / failure beat",
                priority=0.9,
                effects=["payoff"],
                notes="Deaths anchor an episode's structure.",
            ))

        # A cut belongs where the story actually turns, not on a window edge.
        if previous is not None and _is_story_turn(previous, segment):
            out.append(_make(
                segment, category="structure_cut", layer="story",
                reason=(
                    f"Scene change: {_describe(previous)} -> {_describe(segment)}"
                ),
                priority=0.6,
                effects=["clarity", "pacing"],
                start=segment.start, end=segment.start,
            ))

        previous = segment
    return out


def _is_story_turn(before: TimelineSegment, after: TimelineSegment) -> bool:
    """Whether the story genuinely moves between two segments."""
    if not before.events or not after.events:
        return False
    environment_changed = (
        before.events[-1].environment != after.events[0].environment
        and "unknown" not in (
            before.events[-1].environment, after.events[0].environment
        )
    )
    stakes_changed = (
        IMPORTANCE_WEIGHT.get(after.importance, 0.0)
        - IMPORTANCE_WEIGHT.get(before.importance, 0.0)
    ) >= 0.4
    return environment_changed or stakes_changed


def _sound_name(audio_type: str) -> str:
    """A readable name for an audio event type, for prose in a reason."""
    return audio_type.replace("possible_", "").replace("_", " ")


def _describe(segment: TimelineSegment) -> str:
    if not segment.events:
        return "no analysis"
    event = segment.events[0]
    return f"{event.environment}/{event.primary_action}"


def _effect_for(importance: str) -> str:
    return {
        "payoff": "payoff", "reveal": "impact", "danger": "tension",
        "funny": "comedy", "tension": "tension", "setup": "explanation",
    }.get(importance, "pacing")


# ---------------------------------------------------------------------------
# Layer 2 -- pacing
# ---------------------------------------------------------------------------

def layer_pacing(segments: Sequence[TimelineSegment]) -> list[EditRecommendation]:
    """Decide what happens to each segment's *time*.

    The load-bearing decision here is ``hold``. A payoff that a viewer is
    already invested in should be left raw; cutting into it to look busy is the
    single most common way an edit gets worse. So high-value segments with
    strong evidence get an explicit hold, which later layers respect.
    """
    out: list[EditRecommendation] = []

    for index, segment in enumerate(segments):
        importance = segment.importance

        if segment.is_dead_air:
            out.append(_make(
                segment, category="trim_dead_air", layer="pacing",
                reason=f"{segment.duration:.1f}s of silence with no narration",
                priority=0.8,
                intensity="medium",
                effects=["pacing"],
                notes="Removing this tightens the episode without losing content.",
            ))
            continue

        if importance == "boring" and not segment.has_speech:
            out.append(_make(
                segment, category="speed_ramp", layer="pacing",
                reason="Uneventful and silent -- a speed-up keeps momentum",
                priority=0.55,
                intensity="medium",
                effects=["pacing"],
                risks=["over_editing"],
                notes="Suggested 2x. Travel and tunnelling read fine sped up.",
            ))
            continue

        if importance in HIGH_VALUE and segment.usable:
            out.append(_make(
                segment, category="hold", layer="pacing",
                reason=(
                    f"Strong raw {importance} moment -- leave it alone"
                ),
                priority=0.75,
                effects=[_effect_for(importance)],
                notes="Cutting into a moment the viewer is invested in weakens it.",
            ))

        # Anticipation: the beat *before* a payoff is what makes the payoff
        # land, so it is protected rather than trimmed.
        following = segments[index + 1] if index + 1 < len(segments) else None
        if (
            following is not None
            and following.importance in ("payoff", "reveal")
            and importance in TENSION_LEVELS
            and not segment.is_dead_air
        ):
            out.append(_make(
                segment, category="hold", layer="pacing",
                reason="Anticipation before a payoff -- preserve the build-up",
                priority=0.7,
                effects=["anticipation"],
                notes="Trimming this flattens the moment that follows it.",
            ))

    return out


# ---------------------------------------------------------------------------
# Layer 3 -- visual emphasis
# ---------------------------------------------------------------------------

def layer_visual(
    segments: Sequence[TimelineSegment],
    existing: Sequence[EditRecommendation] = (),
) -> list[EditRecommendation]:
    """Decide where the picture should be emphasised.

    Emphasis is earned, not distributed. A punch-in needs a subject worth
    punching in on -- a reveal, a threat, a readable UI beat -- and enough time
    to breathe. Segments already marked ``hold`` by pacing are skipped: the
    previous layer said this moment works as-is, and overruling it here is how
    a planner argues with itself.
    """
    held = {
        entry.recommendation_id.rsplit("_", 1)[0]
        for entry in existing if entry.category == "hold"
    }
    out: list[EditRecommendation] = []

    for segment in segments:
        if segment.is_dead_air or not segment.events or segment.duration < MIN_EMPHASIS_SECONDS:
            continue

        importance = segment.importance
        covered = all(event.ui.any_screen_open for event in segment.events)

        # A reveal wants a slow push: the viewer needs time to read it.
        if importance == "reveal" and not covered:
            out.append(_make(
                segment, category="slow_push_in", layer="visual",
                reason="Reveal -- a slow push draws the eye without hiding it",
                priority=0.75,
                intensity="low",
                effects=["impact", "anticipation"],
                notes="Suggested ~105-112% across the segment.",
            ))

        # A threat wants a hard punch: it should feel abrupt.
        threats = {threat for event in segment.events for threat in event.threats}
        if threats and importance in ("danger", "tension") and not covered:
            out.append(_make(
                segment, category="punch_in", layer="visual",
                reason="Threat on screen (" + ", ".join(sorted(threats)[:3]) + ")",
                priority=0.7,
                intensity="medium",
                effects=["tension", "impact"],
                risks=["hides_gameplay"],
                notes="Suggested ~115%, held for the length of the threat.",
            ))

        # A death screen is the one place a freeze reliably works.
        if any(event.ui.death_screen for event in segment.events):
            out.append(_make(
                segment, category="freeze_frame", layer="visual",
                reason="Death screen -- a brief freeze lands the failure",
                priority=0.65,
                intensity="low",
                effects=["impact", "comedy"],
                notes="Suggested ~0.4s at the moment of death.",
            ))

        # Narration contradicting the picture is the joke; text makes it land.
        if segment.alignment == "contrast" and segment.has_speech:
            out.append(_make(
                segment, category="text_overlay", layer="visual",
                reason="Narration contrasts the picture -- text sharpens the joke",
                priority=0.6,
                intensity="low",
                effects=["comedy"],
                risks=["text_unreadable", "hides_gameplay"],
                notes=f'Quote: "{segment.said[:60]}"',
            ))

        # An achievement is a real beat and already renders its own text.
        if any(event.ui.achievement_toast for event in segment.events):
            out.append(_make(
                segment, category="caption_emphasis", layer="visual",
                reason="Advancement earned -- worth calling out",
                priority=0.55,
                intensity="low",
                effects=["payoff", "clarity"],
                notes="The toast is small; a caption makes it readable.",
            ))

    return out


# ---------------------------------------------------------------------------
# Layer 4 -- audio suggestions
# ---------------------------------------------------------------------------

def layer_audio(segments: Sequence[TimelineSegment]) -> list[EditRecommendation]:
    """Suggest where sound should do work.

    Placeholders only -- no library is wired up yet, and the brief says that is
    fine. What matters is that the *timing* is derived from real evidence, so
    the placeholders land somewhere defensible.
    """
    out: list[EditRecommendation] = []

    for index, segment in enumerate(segments):
        types = segment.audio_types()

        if segment.importance in ("payoff", "reveal") and not segment.is_dead_air:
            out.append(_make(
                segment, category="music_cue", layer="audio",
                reason=f"{segment.importance.capitalize()} -- music should arrive here",
                priority=0.65,
                intensity="medium",
                effects=["payoff", "impact"],
                notes="Placeholder: no track selected yet.",
            ))

        reaction = segment.audio_reaction
        if reaction is not None and reaction.type in (
            "sudden_reaction", "possible_scream"
        ):
            out.append(_make(
                segment, category="sound_effect", layer="audio",
                reason=(
                    f"Audible {_sound_name(reaction.type)} ({reaction.detection})"
                ),
                priority=0.6 + 0.2 * reaction.confidence,
                intensity="low",
                effects=["impact", "comedy"],
                start=reaction.start, end=reaction.end,
                notes="Placeholder impact sound on the reaction.",
            ))

        if "possible_laughter" in types:
            laughter = next(
                event for event in segment.audio_events
                if event.type == "possible_laughter"
            )
            out.append(_make(
                segment, category="sound_effect", layer="audio",
                reason=f"Laughter detected ({laughter.detection})",
                priority=0.45 + 0.25 * laughter.confidence,
                intensity="low",
                effects=["comedy"],
                start=laughter.start, end=laughter.end,
                notes="Placeholder comedic sting.",
            ))

        if segment.has_speech and "music_region" in types:
            out.append(_make(
                segment, category="ducking", layer="audio",
                reason="Speech over a music-like bed -- duck it under the voice",
                priority=0.6,
                intensity="medium",
                effects=["clarity"],
                notes="Placeholder: -8 dB under the speech range.",
            ))

        # A tension beat that leads into a payoff wants a rise under it.
        following = segments[index + 1] if index + 1 < len(segments) else None
        if (
            segment.importance in TENSION_LEVELS
            and following is not None
            and following.importance in ("payoff", "reveal")
        ):
            out.append(_make(
                segment, category="music_cue", layer="audio",
                reason="Tension leading into a payoff -- rise under this",
                priority=0.6,
                intensity="medium",
                effects=["anticipation", "tension"],
                notes="Placeholder riser.",
            ))

        if segment.is_dead_air and segment.duration >= 3.0:
            out.append(_make(
                segment, category="audio_fade", layer="audio",
                reason="Dead air -- fade rather than cut hard if it is kept",
                priority=0.4,
                intensity="low",
                effects=["clarity"],
                notes="Only relevant if the trim is rejected.",
            ))

    return out


# ---------------------------------------------------------------------------
# Layer 5 -- visual polish
# ---------------------------------------------------------------------------

def layer_polish(segments: Sequence[TimelineSegment]) -> list[EditRecommendation]:
    """Flag things that will read badly, and where text must not go.

    Mostly advisory. This layer's most useful output is arguably the *warnings*
    it attaches to other layers' ideas -- a text overlay proposed over an open
    inventory is a readability problem, and saying so is more valuable than a
    colour tweak.
    """
    out: list[EditRecommendation] = []

    for segment in segments:
        if not segment.events:
            continue

        # Caves are genuinely dark; a lift helps a viewer follow the action.
        dark = [
            event for event in segment.events
            if event.environment in ("cave", "mineshaft", "stronghold", "underwater")
            and not event.error
        ]
        if dark and segment.duration >= 3.0:
            out.append(_make(
                segment, category="color_adjust", layer="polish",
                reason=f"Dark environment ({dark[0].environment}) -- lift shadows",
                priority=0.35,
                intensity="low",
                effects=["clarity"],
                notes="Suggested: small shadow lift, no saturation change.",
            ))

        if any(event.ui.any_screen_open for event in segment.events):
            out.append(_make(
                segment, category="marker", layer="polish",
                reason="Full-screen UI open -- keep overlays and zooms clear of it",
                priority=0.3,
                effects=["clarity"],
                risks=["hides_gameplay"],
                notes="Inventory/chest/crafting covers the gameplay here.",
            ))

        if any(event.type == "clipping" for event in segment.audio_events):
            out.append(_make(
                segment, category="audio_fade", layer="polish",
                reason="Audio is clipping -- needs a level fix before anything else",
                priority=0.5,
                intensity="low",
                effects=["clarity"],
                notes="A limiter or a clip-gain reduction on this range.",
            ))

        # Hotbar and health sit at the bottom of frame; a punch-in can crop
        # them off, and that is information the viewer is reading.
        if any(event.ui.low_health for event in segment.events):
            out.append(_make(
                segment, category="marker", layer="polish",
                reason="Low health visible -- do not crop the HUD out",
                priority=0.45,
                effects=["clarity", "tension"],
                risks=["hides_gameplay"],
                notes="The hearts are the reason this moment is tense.",
            ))

    return out


# ---------------------------------------------------------------------------
# Layer 6 -- safety / anti-trash
# ---------------------------------------------------------------------------

def layer_safety(
    recommendations: Sequence[EditRecommendation],
    segments: Sequence[TimelineSegment],
    *,
    budget_seconds: float = ACTIVE_EDIT_BUDGET_SECONDS,
    min_repeat_gap: float = MIN_REPEAT_GAP,
) -> list[EditRecommendation]:
    """Remove or soften the bad ideas. Nothing is deleted, only marked.

    Runs six checks, cheapest and most certain first, so a recommendation that
    fails hard (no evidence) is not also charged against the density budget:

    1. **No evidence** -> rejected. Nothing to defend it with.
    2. **Transcript-only, contradicted** -> rejected. If the words say
       "terrifying" and both the picture and the audio are calm, the words are
       the weakest of the three.
    3. **Covers gameplay** -> rejected. A zoom or overlay over an open
       inventory or visible low health hides what the viewer is watching.
    4. **Weak single-channel** -> downgraded. One channel at low priority is a
       hunch, not a case.
    5. **Repetition** -> downgraded. Same category too close to its neighbour.
    6. **Density budget** -> downgraded, lowest priority first, until active
       edits fit one per ``budget_seconds`` of covered footage.

    ``hold`` and ``marker`` are exempt from 4-6: they change nothing, and an
    editor is well served by plenty of both.
    """
    by_segment = {
        (segment.asset_id, round(segment.start, 3)): segment for segment in segments
    }
    ordered = sorted(recommendations, key=lambda r: (r.asset_id, r.start, r.category))
    survivors: list[EditRecommendation] = []

    for entry in ordered:
        segment = by_segment.get((entry.asset_id, round(entry.start, 3)))

        # 1. no evidence
        if not entry.has_evidence:
            survivors.append(entry.reject(
                "No visual, transcript or audio evidence supports this."
            ))
            continue

        # 2. transcript-only and contradicted by the other two channels
        if _is_contradicted_transcript_only(entry, segment):
            survivors.append(entry.reject(
                "Based only on narration, while the picture and audio disagree."
            ))
            continue

        # 3. would cover gameplay the viewer needs
        hiding = _hides_gameplay(entry, segment)
        if hiding:
            survivors.append(entry.reject(hiding))
            continue

        # 4. one weak channel
        if (
            entry.category in ACTIVE_CATEGORIES
            and len(entry.evidence.channels) < 2
            and entry.priority < SINGLE_CHANNEL_MIN_PRIORITY
        ):
            survivors.append(entry.downgrade(
                f"Only {entry.evidence.channels[0] if entry.evidence.channels else 'one'} "
                "evidence, at low priority."
            ))
            continue

        survivors.append(entry)

    survivors = _space_out_repeats(survivors, min_repeat_gap)
    survivors = _apply_budget(survivors, segments, budget_seconds)
    return survivors


def _is_contradicted_transcript_only(
    entry: EditRecommendation, segment: Optional[TimelineSegment]
) -> bool:
    """Narration alone, with the picture and audio pointing elsewhere."""
    if segment is None or entry.category not in ACTIVE_CATEGORIES:
        return False
    if entry.evidence.channels != ["transcript"]:
        return False
    calm_picture = segment.importance in ("boring", "setup")
    calm_audio = not segment.audio_reaction
    return calm_picture and calm_audio


def _hides_gameplay(
    entry: EditRecommendation, segment: Optional[TimelineSegment]
) -> str:
    """Reason this edit would obscure something the viewer needs, or ""."""
    if segment is None or entry.category not in (
        "punch_in", "slow_push_in", "text_overlay", "caption_emphasis"
    ):
        return ""

    if any(event.ui.any_screen_open for event in segment.events):
        return (
            "A full-screen UI (inventory/chest/crafting) is open here; zooming "
            "or overlaying would hide what the viewer is reading."
        )
    if entry.category in ("punch_in", "slow_push_in") and any(
        event.ui.low_health for event in segment.events
    ):
        return (
            "Low health is visible and is the reason this moment is tense; a "
            "zoom risks cropping the HUD out of frame."
        )
    return ""


def _space_out_repeats(
    entries: list[EditRecommendation], min_gap: float
) -> list[EditRecommendation]:
    """Downgrade an active edit repeating its category too soon."""
    last_seen: dict = {}
    for entry in entries:
        if entry.status != "accepted" or entry.category not in ACTIVE_CATEGORIES:
            continue
        key = (entry.asset_id, entry.category)
        previous = last_seen.get(key)
        if previous is not None and entry.start - previous < min_gap:
            entry.downgrade(
                f"Another {entry.category} {entry.start - previous:.1f}s earlier; "
                f"repeats closer than {min_gap:.0f}s read as a tic."
            )
            if "repetitive" not in entry.risks:
                entry.risks.append("repetitive")
            continue
        last_seen[key] = entry.start
    return entries


def _apply_budget(
    entries: list[EditRecommendation],
    segments: Sequence[TimelineSegment],
    budget_seconds: float,
) -> list[EditRecommendation]:
    """Hold active edits down to one per ``budget_seconds`` of footage.

    Weakest first, so the budget removes the least defensible ideas rather than
    whichever happened to come last. This is the check that stops an
    enthusiastic set of layers turning a calm episode into a music video.
    """
    covered = sum(segment.duration for segment in segments)
    if covered <= 0 or budget_seconds <= 0:
        return entries

    allowed = max(1, int(covered // budget_seconds))
    active = [
        entry for entry in entries
        if entry.status == "accepted" and entry.category in ACTIVE_CATEGORIES
    ]
    if len(active) <= allowed:
        return entries

    for entry in sorted(active, key=lambda r: r.priority)[: len(active) - allowed]:
        entry.downgrade(
            f"Over the editing budget: {len(active)} active edits across "
            f"{covered:.0f}s, which allows about {allowed}."
        )
        if "over_editing" not in entry.risks:
            entry.risks.append("over_editing")
    return entries


#: The five proposing layers, in order. Layer 6 runs separately because it
#: takes the accumulated set rather than the segments.
PROPOSING_LAYERS = (
    ("story", layer_story),
    ("pacing", layer_pacing),
    ("visual", layer_visual),
    ("audio", layer_audio),
    ("polish", layer_polish),
)
