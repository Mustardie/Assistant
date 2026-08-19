"""Putting narration and visuals on one timeline.

Two independent descriptions of the same recording arrive here -- what the
model saw (``VisualEvent``) and what was said (``Transcript``) -- and this
module merges them into ``TimelineSegment`` records that answer three questions
per stretch of footage:

* what is happening on screen,
* what is being said over it,
* and whether those two agree, disagree, or are simply unrelated.

That third question is the point. A segment where narration and visuals **match**
is straightforward material. A segment where they **contrast** ("this is
completely safe" over a creeper explosion) is usually the funniest thing in the
recording. A **neutral** segment is someone talking about their upload schedule
while mining. An editor wants those three treated very differently, so the
distinction is computed explicitly rather than left implicit in the text.

Everything here is pure: events and transcripts in, segments out. No model, no
files, no clock. The classification is a documented keyword heuristic, not a
second model call -- it is meant to be cheap, deterministic and arguable, and
every verdict carries the evidence that produced it in ``alignment_reason``.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from editing.config import SCHEMA_VERSION, SamplingConfig
from editing.schema import (
    IMPORTANCE_WEIGHT, MediaAsset, StructureTimeline, TimelineSegment,
    Transcript, TranscriptEntry, VisualEvent, short_hash,
)

# ---------------------------------------------------------------------------
# Vocabulary for the alignment heuristic
# ---------------------------------------------------------------------------

#: Words in narration that indicate each environment. Only reasonably
#: unambiguous ones: "end" is excluded because "in the end" is a figure of
#: speech, and mislabelling that as a dimension reference would be worse than
#: missing it.
_ENV_WORDS = {
    "cave": ("cave", "caves", "cavern", "underground", "ravine", "deepslate",
             "tunnel", "spelunk"),
    "mineshaft": ("mineshaft", "mine shaft", "abandoned mine", "rails", "minecart"),
    "stronghold": ("stronghold", "end portal", "silverfish", "portal room"),
    "nether": ("nether", "netherrack", "lava sea", "ghast", "piglin", "blaze",
               "soul sand", "crimson", "warped"),
    "nether_fortress": ("fortress", "nether fortress", "blaze spawner"),
    "end": ("the end", "ender dragon", "end city", "endermen", "chorus"),
    "village": ("village", "villager", "villagers", "iron golem", "trading hall"),
    "forest": ("forest", "woods", "trees", "taiga", "birch", "jungle"),
    "jungle": ("jungle", "bamboo", "cocoa", "parrot"),
    "swamp": ("swamp", "witch hut", "slime", "mangrove"),
    "desert": ("desert", "sand", "cactus", "badlands", "mesa", "temple"),
    "plains": ("plains", "field", "grassland", "savanna", "meadow"),
    "mountains": ("mountain", "mountains", "peak", "cliff", "hills"),
    "snow": ("snow", "snowy", "ice", "frozen", "glacier", "tundra"),
    "ocean": ("ocean", "sea", "beach", "shore", "boat", "island"),
    "underwater": ("underwater", "drowned", "monument", "guardian", "shipwreck"),
    "base": ("base", "house", "home", "my build", "storage", "our base"),
    "farm": ("farm", "crops", "wheat", "harvest", "breeding"),
    "structure": ("temple", "outpost", "dungeon", "trial chamber", "ancient city",
                  "bastion", "ruins"),
}

#: Words that indicate each action.
_ACTION_WORDS = {
    "mining": ("mining", "mine", "dig", "digging", "ore", "diamond", "diamonds",
               "iron", "gold", "coal", "emerald", "netherite", "ancient debris",
               "pickaxe", "vein"),
    "building": ("build", "building", "place", "placing", "construct", "design",
                 "blocks down"),
    "fighting": ("fight", "fighting", "kill", "killing", "attack", "attacking",
                 "hit", "sword", "shield", "combat", "hostile"),
    "escaping": ("run", "running", "escape", "escaping", "flee", "get out",
                 "get away", "retreat"),
    "looting": ("loot", "looting", "chest", "chests", "treasure", "grab", "took"),
    "crafting": ("craft", "crafting", "recipe", "smelt", "furnace", "anvil",
                 "smithing"),
    "dying": ("died", "die", "death", "dead", "respawn", "lost my stuff",
              "lost everything"),
    "travelling": ("walk", "walking", "travel", "heading", "going to", "way to",
                   "ride", "riding", "sprint", "fly", "flying", "elytra"),
    "farming": ("farm", "farming", "plant", "planting", "harvest", "breed"),
    "searching": ("looking for", "search", "searching", "find", "finding",
                  "trying to find", "where is"),
    "trading": ("trade", "trading", "villager", "emeralds", "barter"),
    "enchanting": ("enchant", "enchanting", "enchantment", "lapis", "bookshelf"),
    "brewing": ("brew", "brewing", "potion", "potions"),
    "redstone": ("redstone", "circuit", "piston", "observer", "repeater"),
}

#: Mobs and hazards worth matching against the model's entity list.
_ENTITY_WORDS = (
    "creeper", "zombie", "skeleton", "spider", "enderman", "witch", "slime",
    "silverfish", "blaze", "ghast", "piglin", "hoglin", "wither", "warden",
    "guardian", "drowned", "phantom", "pillager", "ravager", "vex", "shulker",
    "dragon", "cow", "pig", "sheep", "chicken", "wolf", "cat", "villager",
    "lava", "tnt", "fall", "void",
)

#: Narration that plays a moment down. Over a dangerous or high-motion visual
#: this is the classic contrast.
_CALM_WORDS = (
    "safe", "fine", "no problem", "easy", "relax", "relaxing", "chill",
    "nothing to worry", "no danger", "totally fine", "under control",
    "peaceful", "calm", "simple", "should be okay", "should be fine",
    "what could go wrong", "nothing can go wrong",
)

#: Narration that plays a moment up. Over a boring visual, likewise a contrast.
_HYPE_WORDS = (
    "insane", "crazy", "amazing", "incredible", "oh my god", "no way",
    "what the", "holy", "unbelievable", "terrifying", "so scared", "help",
    "i'm dead", "we're dead", "run", "worst", "best ever", "never seen",
)

#: Narration that acknowledges danger, which agrees with a dangerous visual.
_ALARM_WORDS = (
    "careful", "watch out", "danger", "dangerous", "scared", "scary", "help",
    "run", "oh no", "get out", "low health", "almost died", "nearly died",
    "that was close", "i'm dying", "hurt",
)

#: Alignment kinds an editor should still be shown. Contrast scores as well as
#: match because a mismatch between narration and picture is usually the joke.
_ALIGNMENT_BONUS = {"match": 1.0, "contrast": 1.0, "neutral": 0.4, "unknown": 0.2}


def _words(text: str) -> str:
    """Lowercased text padded with spaces, so ``" run "`` matches whole words."""
    return " " + re.sub(r"[^a-z0-9' ]+", " ", str(text or "").lower()) + " "


def _mentions(haystack: str, needles: Iterable[str]) -> list[str]:
    """Which of ``needles`` appear in ``haystack`` as whole words/phrases."""
    found = []
    for needle in needles:
        token = needle.lower().strip()
        if not token:
            continue
        if f" {token} " in haystack or f" {token}" in haystack[-len(token) - 2:]:
            found.append(token)
        elif " " in token and token in haystack:
            found.append(token)
    return found


# ---------------------------------------------------------------------------
# Alignment classification
# ---------------------------------------------------------------------------

@dataclass
class AlignmentVerdict:
    kind: str
    reason: str
    evidence: list[str]


def classify_alignment(
    events: Sequence[VisualEvent], said: str
) -> AlignmentVerdict:
    """Decide whether narration and visuals agree, clash, or are unrelated.

    Order matters here. Direct agreement is checked first, because a segment
    where the player says "diamonds!" while mining diamonds is a match even
    though the narration is also excited. Only then are the contrast patterns
    tested, and anything left over with speech in it is neutral.
    """
    if not said.strip():
        return AlignmentVerdict("unknown", "No speech in this segment.", [])
    if not events:
        return AlignmentVerdict("unknown", "No visual analysis for this segment.", [])
    if all(event.error for event in events):
        return AlignmentVerdict(
            "unknown", "Visual analysis failed for this segment.", []
        )

    text = _words(said)
    environments = {event.environment for event in events} - {"unknown"}
    actions = {action for event in events for action in event.actions} - {"unknown"}
    entities = {
        entity.lower() for event in events for entity in (event.entities + event.threats)
    }
    dangerous = any(
        event.importance in ("danger", "tension") or event.threats
        or event.ui.low_health or event.ui.death_screen
        for event in events
    )
    boring = all(event.importance == "boring" for event in events)

    # -- agreement -----------------------------------------------------
    hits: list[str] = []
    for environment in environments:
        hits += [f"env:{word}" for word in _mentions(text, _ENV_WORDS.get(environment, ()))]
    for action in actions:
        hits += [f"action:{word}" for word in _mentions(text, _ACTION_WORDS.get(action, ()))]
    for entity in entities:
        # Entity names come from the model as free text ("2 creepers"), so
        # match on the known mob words inside them rather than the whole string.
        for word in _ENTITY_WORDS:
            if word in entity and f" {word}" in text:
                hits.append(f"entity:{word}")
    if dangerous:
        hits += [f"alarm:{word}" for word in _mentions(text, _ALARM_WORDS)]

    if hits:
        unique = sorted(set(hits))[:8]
        return AlignmentVerdict(
            "match",
            "Narration refers to what is on screen: " + ", ".join(unique),
            unique,
        )

    # -- contrast ------------------------------------------------------
    calm = _mentions(text, _CALM_WORDS)
    if dangerous and calm:
        return AlignmentVerdict(
            "contrast",
            "Narration plays down a dangerous moment: " + ", ".join(calm[:5]),
            [f"calm:{word}" for word in calm[:5]],
        )

    hype = _mentions(text, _HYPE_WORDS)
    if boring and hype:
        return AlignmentVerdict(
            "contrast",
            "Narration is excited over an uneventful shot: " + ", ".join(hype[:5]),
            [f"hype:{word}" for word in hype[:5]],
        )

    # Narration naming a *different* place than the one on screen. Checked last
    # because it is the weakest signal -- the player may be describing where
    # they are heading rather than where they are.
    if environments:
        mentioned = {
            name for name, words in _ENV_WORDS.items() if _mentions(text, words)
        }
        elsewhere = mentioned - environments
        if elsewhere and not (mentioned & environments):
            return AlignmentVerdict(
                "contrast",
                "Narration talks about "
                + "/".join(sorted(elsewhere))
                + " while the footage shows "
                + "/".join(sorted(environments)) + ".",
                [f"elsewhere:{name}" for name in sorted(elsewhere)][:5],
            )

    return AlignmentVerdict(
        "neutral", "Speech present but unrelated to what is on screen.", []
    )


# ---------------------------------------------------------------------------
# Usefulness
# ---------------------------------------------------------------------------

#: Segments at or above this score are flagged ``usable``.
DEFAULT_USABLE_THRESHOLD = 0.45


def score_segment(
    segment: TimelineSegment, *, threshold: float = DEFAULT_USABLE_THRESHOLD
) -> tuple[float, bool, list[str]]:
    """Rate how likely a segment is to be worth cutting into a video.

    A weighted sum, not a model call: it must be deterministic, explainable and
    free. The weights say what this layer believes -- that what is happening
    matters most, that speech and a narration/visual relationship matter next,
    and that motion and visible threats are supporting evidence.

    Returns ``(score, usable, reasons)``; ``reasons`` is what makes a ranking
    arguable instead of magic.
    """
    reasons: list[str] = []

    if not segment.events or all(event.error for event in segment.events):
        return 0.0, False, ["No usable visual analysis."]

    best = max(segment.events, key=lambda event: event.weight)
    importance = IMPORTANCE_WEIGHT.get(best.importance, 0.3)
    confidence = max(0.2, best.confidence)
    score = 0.45 * importance * confidence
    reasons.append(
        f"importance={best.importance} ({importance:.2f}) at confidence "
        f"{best.confidence:.2f}"
    )

    if segment.has_speech:
        # Speech density, capped: a wall of words is not four times better than
        # a good line, and long transcripts would otherwise dominate the score.
        words = len(segment.said.split())
        speech = min(1.0, words / 25.0)
        score += 0.15 * speech
        reasons.append(f"{words} words of narration")
    else:
        reasons.append("no narration")

    bonus = _ALIGNMENT_BONUS.get(segment.alignment, 0.2)
    score += 0.15 * bonus
    reasons.append(f"alignment={segment.alignment}")

    intensity = max((event.camera.intensity for event in segment.events), default=0.0)
    motion = max((event.motion_score for event in segment.events), default=0.0)
    score += 0.10 * max(intensity, motion)
    if max(intensity, motion) > 0.4:
        reasons.append(f"camera/motion {max(intensity, motion):.2f}")

    threats = {threat for event in segment.events for threat in event.threats}
    if threats:
        score += 0.10
        reasons.append("threats: " + ", ".join(sorted(threats)[:4]))

    if any(event.ui.death_screen for event in segment.events):
        score += 0.10
        reasons.append("death screen visible")
    if any(event.ui.achievement_toast for event in segment.events):
        score += 0.05
        reasons.append("achievement toast")

    # Penalties: a full-screen menu is unusable footage however interesting the
    # model found it, and a sub-second segment cannot be cut to.
    if all(event.ui.any_screen_open for event in segment.events):
        score -= 0.15
        reasons.append("a full-screen UI covers the whole segment")
    if segment.duration < 1.0:
        score -= 0.10
        reasons.append("shorter than a second")

    score = max(0.0, min(1.0, score))
    usable = (
        score >= threshold
        and segment.duration >= 1.0
        and not all(event.error for event in segment.events)
    )
    return score, usable, reasons


# ---------------------------------------------------------------------------
# Segment construction
# ---------------------------------------------------------------------------

def _mergeable(left: VisualEvent, right: VisualEvent) -> bool:
    """Whether two adjacent events describe the same continuing moment."""
    return (
        left.environment == right.environment
        and left.primary_action == right.primary_action
        and left.importance == right.importance
        and not left.error and not right.error
    )


def group_events(
    events: Sequence[VisualEvent],
    *,
    merge_similar: bool = True,
    max_segment_seconds: float = 30.0,
) -> list[list[VisualEvent]]:
    """Group consecutive events into segments.

    Merging matters for usability: with 8-second windows, a two-minute cave
    exploration arrives as fifteen near-identical events, and an editor wants
    one range to trim rather than fifteen to reconcile. The merge is
    conservative -- same place, same action, same importance, no errors -- and
    capped by ``max_segment_seconds`` so a long uniform stretch still breaks
    into cuttable pieces.
    """
    ordered = sorted(events, key=lambda event: (event.start, event.end))
    if not ordered:
        return []
    if not merge_similar:
        return [[event] for event in ordered]

    groups: list[list[VisualEvent]] = [[ordered[0]]]
    for event in ordered[1:]:
        current = groups[-1]
        span = event.end - current[0].start
        # A gap means something was not analysed between them; merging across
        # it would claim coverage the analysis does not have.
        contiguous = event.start <= current[-1].end + 0.25
        if _mergeable(current[-1], event) and contiguous and span <= max_segment_seconds:
            current.append(event)
        else:
            groups.append([event])
    return groups


def build_segments(
    asset: MediaAsset,
    events: Sequence[VisualEvent],
    transcript: Optional[Transcript] = None,
    *,
    merge_similar: bool = True,
    max_segment_seconds: float = 30.0,
    usable_threshold: float = DEFAULT_USABLE_THRESHOLD,
) -> list[TimelineSegment]:
    """Build one asset's segments from its events and transcript."""
    groups = group_events(
        events, merge_similar=merge_similar, max_segment_seconds=max_segment_seconds
    )
    if not groups and transcript is not None and len(transcript):
        return _segments_from_transcript(asset, transcript, usable_threshold)

    segments: list[TimelineSegment] = []
    for group in groups:
        start = min(event.start for event in group)
        end = max(event.end for event in group)
        entries = (
            transcript.entries_between(start, end) if transcript is not None else []
        )
        said = " ".join(entry.text for entry in entries if entry.text).strip()

        segment = TimelineSegment(
            segment_id="s_" + short_hash(asset.asset_id, round(start, 3), round(end, 3)),
            asset_id=asset.asset_id,
            source_file=asset.path,
            start=start,
            end=end,
            said=said,
            speech_entries=list(entries),
            events=list(group),
        )
        verdict = classify_alignment(group, said)
        segment.alignment = verdict.kind
        segment.alignment_reason = verdict.reason
        segment.usefulness, segment.usable, segment.reasons = score_segment(
            segment, threshold=usable_threshold
        )
        segments.append(segment)

    return segments


def _segments_from_transcript(
    asset: MediaAsset, transcript: Transcript, usable_threshold: float
) -> list[TimelineSegment]:
    """Fall back to a speech-only timeline when there is no visual analysis.

    Worth doing rather than returning nothing: a transcript alone still tells an
    editor where the talking is. Every such segment is marked ``unknown``
    alignment and left un-usable, so it can never be mistaken for an analysed
    one.
    """
    segments: list[TimelineSegment] = []
    for entry in transcript.entries:
        segment = TimelineSegment(
            segment_id="s_" + short_hash(
                asset.asset_id, round(entry.start, 3), round(entry.end, 3), "speech"
            ),
            asset_id=asset.asset_id,
            source_file=asset.path,
            start=entry.start,
            end=entry.end,
            said=entry.text,
            speech_entries=[entry],
            events=[],
            alignment="unknown",
            alignment_reason="Speech only: this file has no visual analysis yet.",
            usefulness=0.0,
            usable=False,
            reasons=["No visual analysis; run `analyze` for this file."],
        )
        segments.append(segment)
    return segments


# ---------------------------------------------------------------------------
# Timeline assembly
# ---------------------------------------------------------------------------

def build_timeline(
    assets: Sequence[MediaAsset],
    events_by_asset: dict,
    transcripts_by_asset: Optional[dict] = None,
    *,
    sampling: Optional[SamplingConfig] = None,
    model: str = "",
    transcript_sources: Optional[dict] = None,
    warnings: Optional[Sequence[str]] = None,
    merge_similar: bool = True,
    max_segment_seconds: float = 30.0,
    usable_threshold: float = DEFAULT_USABLE_THRESHOLD,
) -> StructureTimeline:
    """Assemble the deliverable from every asset's events and transcript.

    Assets are kept in the order given (discovery sorts them by path) and
    segments within an asset in time order, so the output is stable across runs
    -- which is what makes two timelines diffable.
    """
    transcripts_by_asset = transcripts_by_asset or {}
    collected: list[TimelineSegment] = []
    notes = list(warnings or [])

    for asset in assets:
        events = list(events_by_asset.get(asset.asset_id) or [])
        transcript = transcripts_by_asset.get(asset.asset_id)
        segments = build_segments(
            asset, events, transcript,
            merge_similar=merge_similar,
            max_segment_seconds=max_segment_seconds,
            usable_threshold=usable_threshold,
        )
        if not segments:
            notes.append(
                f"{asset.filename}: no visual events and no transcript, so it "
                "contributes nothing to the timeline."
            )
        collected.extend(segments)

    return StructureTimeline(
        segments=collected,
        assets=list(assets),
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        model=model,
        sampling=(sampling.validated().to_dict() if sampling else {}),
        transcript_sources=dict(transcript_sources or {}),
        warnings=notes,
        schema_version=SCHEMA_VERSION,
    )


__all__ = [
    "AlignmentVerdict", "classify_alignment", "score_segment", "group_events",
    "build_segments", "build_timeline", "DEFAULT_USABLE_THRESHOLD",
]
