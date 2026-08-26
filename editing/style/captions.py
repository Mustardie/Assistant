"""The caption and text layer.

The failure mode this module is written against is **text spam**. A system that
can put a transcript on screen will put the whole transcript on screen, and the
result is unwatchable in a way that is obvious to a viewer and invisible to the
code that produced it. So the design is subtractive throughout:

* Captions come **only** from transcript lines that already exist and are
  already aligned to the timeline. Nothing is written, paraphrased or invented.
* A line has to *earn* a caption. It is scored against what the picture, the
  audio and the words are all doing at that moment, and lines below the style's
  ``caption_min_priority`` never become candidates at all.
* A long line is **condensed to its strongest phrase**, not truncated at a word
  count. "okay so anyway I think that was probably a creeper" becomes "that was
  a creeper", because the window around the strongest keyword is the part worth
  reading.
* Placement is refused rather than guessed. When a full-screen menu is open, or
  the critic flagged text at that moment, or the style has no safe zone left,
  the item becomes a **marker** saying what should go there — never text placed
  hopefully over the game.

Density and spacing are **not** enforced here. This module proposes; the
compiler decides how many survive, using the same ceilings for every layer.
That split is deliberate: "which lines are worth captioning" and "how many
captions this style tolerates" are different questions, and answering them in
one pass makes both untestable.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

from editing.recommend.schema import RecommendationSet
from editing.roughcut.schema import ClipPlacement, RoughCutPlan
from editing.roughcut.select import map_to_sequence
from editing.schema import StructureTimeline, TimelineSegment, TranscriptEntry
from editing.style.presets import ZONE_POSITION, StylePreset
from editing.style.schema import LayerEvidence, LayerItem, item_id_for
from editing.tracks import DEFAULT_LAYOUT

#: The video track styled overlays land on. V1 is the rough cut's assembly, so
#: everything this layer draws goes above it and can be deleted as a unit.
OVERLAY_TRACK = DEFAULT_LAYOUT.captions

#: Words that mark a line as a reaction. Deliberately short and literal: this
#: is a keyword list, not sentiment analysis, and pretending otherwise would
#: make its mistakes harder to predict.
#: The three lists below are kept **disjoint**. A word in two of them would
#: score twice for one utterance, which is how "creeper creeper run run run"
#: reached a perfect 1.0 and outranked a chapter card. Words that could belong
#: to either list live in the more specific one: "run" and "help" are danger,
#: not generic reaction.
REACTION_WORDS = (
    "oh my god", "oh my", "what the", "no way", "holy", "oh no", "wait what",
    "let's go", "lets go", "yes yes", "i can't", "i cant", "are you kidding",
    "that was close", "nearly died", "almost died", "aaah",
    "what", "wow", "whoa", "woah", "oh",
)

DANGER_WORDS = (
    "creeper", "watch out", "behind you", "low health", "half a heart",
    "one heart", "i'm dying", "im dying", "we're dead", "were dead", "careful",
    "run", "help", "it's going to blow", "its going to blow", "warden", "lava",
)

#: Explanatory connectives. What a documentary caption is usually pulling out.
EXPLANATORY_WORDS = (
    "because", "so that", "which means", "the plan is", "the plan was",
    "we need", "the point", "first", "next", "finally", "the idea",
    "turns out", "the problem", "the reason",
)

#: Transcript annotations. A line that is only an annotation is not a caption.
_ANNOTATION = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*$")
_BRACKETED = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_WORD = re.compile(r"[\w']+")

#: Audio event types that make a line worth putting on screen.
REACTION_AUDIO = frozenset({
    "sudden_reaction", "possible_laughter", "possible_scream", "loudness_spike",
})

#: HUD/menu states that make any overlay a bad idea at that moment.
BLOCKING_UI = frozenset({
    "inventory_open", "crafting_open", "chest_open", "map_open", "death_screen",
})

#: Critic issues that mean "do not put text here". Read from a Session 4
#: revision set when one exists.
CRITIC_TEXT_BLOCKERS = frozenset({
    "text_placed_badly", "caption_covers_gameplay", "text_unreadable",
    "hud_hidden", "bad_crop",
})

#: Lines shorter than this are a fragment, not a line.
MIN_CAPTION_CHARS = 3


def build_captions(
    timeline: StructureTimeline,
    roughcut: RoughCutPlan,
    style: StylePreset,
    *,
    recommendations: Optional[RecommendationSet] = None,
    blocked_ranges: Sequence[tuple] = (),
) -> list[LayerItem]:
    """Caption candidates for this cut, unfiltered by density.

    ``blocked_ranges`` is ``[(start, end, reason), ...]`` in **sequence** time,
    normally derived from critic findings — moments where the picture already
    has a text problem and adding more would compound it.
    """
    if not style.text_allowed:
        return []

    recommendations = recommendations or RecommendationSet()
    boosts = _recommendation_boosts(recommendations)
    items: list[LayerItem] = []

    for segment in timeline.segments:
        for entry in segment.speech_entries:
            item = _consider(
                entry, segment, roughcut, style, boosts, blocked_ranges
            )
            if item is not None:
                items.append(item)
        label = _callout_label(segment, roughcut, style)
        if label is not None:
            items.append(label)

    items.sort(key=lambda item: item.start)
    return items


def _callout_label(
    segment: TimelineSegment, roughcut: RoughCutPlan, style: StylePreset
):
    """A label naming the thing on screen, when a moment turns on one thing.

    Always a marker. No callout graphic has been designed, so drawing a label
    would mean inventing a look for it -- and a label in the wrong typeface is
    worse than a note saying which mob to point at. The marker carries the
    entity name, which is the part a person cannot easily recover later.
    """
    if not style.allows("callout_label"):
        return None
    if segment.importance not in ("payoff", "reveal", "danger"):
        return None

    named = sorted({
        name for event in segment.events
        for name in (list(event.threats) + list(event.entities))
    })
    if not named:
        return None

    at = map_to_sequence(roughcut.placements, segment.asset_id, segment.start)
    if at is None:
        return None
    placement = roughcut.placement_at(at)

    item = LayerItem(
        item_id=item_id_for("callout_label", at, named[0]),
        layer="caption",
        kind="callout_label",
        placement_id=placement.placement_id if placement else "",
        start=at,
        end=at,
        source_start=segment.start,
        source_end=segment.end,
        asset_id=segment.asset_id,
        style=style.name,
        reason=f"the {segment.importance} here turns on {named[0]}, which a "
               "viewer may not spot unaided",
        effect="clarity",
        priority=round(min(1.0, 0.45 + segment.usefulness * 0.3), 3),
        evidence=LayerEvidence(
            visual_event_ids=[e.event_id for e in segment.events][:5],
            segment_ids=[segment.segment_id],
            summary=segment.summary()[:200],
        ),
        payload={"placeholder": named[0], "label": named[0],
                 "entities": named[:5]},
    )
    _as_marker(
        item, style,
        "no callout graphic has been designed, so this records what to point "
        "at rather than drawing a label",
    )
    return item


# ---------------------------------------------------------------------------
# One transcript line
# ---------------------------------------------------------------------------

def _consider(
    entry: TranscriptEntry,
    segment: TimelineSegment,
    roughcut: RoughCutPlan,
    style: StylePreset,
    boosts: Sequence[tuple],
    blocked_ranges: Sequence[tuple],
) -> Optional[LayerItem]:
    text = _clean(entry.text)
    if len(text) < MIN_CAPTION_CHARS or _ANNOTATION.match(entry.text or ""):
        return None

    boost = _boost_for(boosts, segment.asset_id, entry.start, entry.end)
    kind, effect, score, why = _classify(entry, segment, text, boost)
    if not style.allows(kind):
        return None
    if score < style.caption_min_priority:
        return None

    start = map_to_sequence(roughcut.placements, segment.asset_id, entry.start)
    if start is None:
        # The line was cut out of the rough cut. Dropping it is the honest
        # answer -- nudging it to the nearest surviving frame would caption
        # footage in which nobody said it.
        return None

    placement = roughcut.placement_at(start)
    end = map_to_sequence(roughcut.placements, segment.asset_id, entry.end)
    duration = _duration_for(text, style, start, end, placement)

    condensed, was_condensed = condense(text, style.max_caption_words)
    blocked, block_reason = _blocked(
        segment, start, start + duration, blocked_ranges
    )
    zone = None if blocked else style.zone_for(kind)

    item = LayerItem(
        item_id=item_id_for(kind, start, condensed),
        layer="caption",
        kind=kind,
        placement_id=placement.placement_id if placement else "",
        start=start,
        end=start + duration,
        source_start=entry.start,
        source_end=entry.end,
        asset_id=segment.asset_id,
        style=style.name,
        reason=why,
        effect=effect,
        priority=round(score, 3),
        intensity="medium" if kind == "danger_text" else "low",
        evidence=LayerEvidence(
            transcript_quotes=[text[:300]],
            visual_event_ids=[e.event_id for e in segment.events][:5],
            audio_event_ids=[a.event_id for a in segment.audio_events][:5],
            audio_types=sorted(segment.audio_types()),
            segment_ids=[segment.segment_id],
            summary=f"said at {entry.start:.2f}s of the source",
        ),
        payload={
            "text": condensed,
            "full_line": text[:300],
            "condensed": was_condensed,
            "zone": zone or "",
            "words": len(_WORD.findall(condensed)),
        },
    )
    if was_condensed:
        item.notes = f'condensed from "{text[:120]}"'

    if zone is None:
        _as_marker(item, style, block_reason or (
            "no safe zone is left in this style for text at this moment"
        ))
    elif not style.allow_real_text:
        _as_marker(
            item, style,
            f"the {style.name} style leaves text as a note for the editor "
            "rather than drawing it",
        )
    else:
        item.premiere_ops = [_text_op(item, style, zone, duration)]
    return item


def _classify(
    entry: TranscriptEntry,
    segment: TimelineSegment,
    text: str,
    boost: float,
) -> tuple:
    """What kind of caption this line is, and how much it deserves one.

    Scored rather than matched, because the same words mean different things
    over different pictures: "oh no" over a creeper is a danger caption, over a
    crafting table it is nothing. Every contribution is additive and small, so
    a line needs agreement from more than one channel to clear a style's bar.
    """
    lowered = text.lower()
    reasons: list[str] = []
    score = 0.25

    reaction_hit = _first_hit(lowered, REACTION_WORDS)
    danger_hit = _first_hit(lowered, DANGER_WORDS)
    explain_hit = _first_hit(lowered, EXPLANATORY_WORDS)

    audio = segment.audio_reaction
    laughter = "possible_laughter" in segment.audio_types()
    threats = any(event.threats for event in segment.events)
    importance = segment.importance

    if audio is not None:
        score += 0.22
        reasons.append(f"the audio spikes here ({audio.type})")
    if laughter:
        score += 0.10
        reasons.append("laughter around this line")
    if importance in ("payoff", "reveal", "funny"):
        score += 0.20
        reasons.append(f"the picture is a {importance}")
    elif importance == "danger":
        score += 0.18
        reasons.append("the player is in danger here")
    elif importance == "boring":
        score -= 0.18
        reasons.append("nothing is happening on screen")

    if reaction_hit and reaction_hit != danger_hit:
        score += 0.16
        reasons.append(f'the line reacts ("{reaction_hit}")')
    if danger_hit and (threats or importance == "danger"):
        score += 0.22
        reasons.append(f'the line names the threat ("{danger_hit}")')
    if explain_hit:
        score += 0.12
        reasons.append(f'the line explains ("{explain_hit}")')
    if segment.alignment == "match":
        score += 0.10
        reasons.append("the words and the picture agree")
    elif segment.alignment == "contrast":
        # A deadpan line over chaos is exactly what a caption is for.
        score += 0.14
        reasons.append("the words and the picture disagree, which reads as a joke")

    if boost:
        score += min(0.2, boost)
        reasons.append("a recommendation already asked for text here")

    if danger_hit and (threats or importance == "danger"):
        kind, effect = "danger_text", "tension"
    elif reaction_hit or audio is not None or laughter:
        kind, effect = "reaction_caption", "comedy" if laughter else "impact"
    else:
        kind, effect = "key_phrase", "explanation"

    why = "; ".join(reasons[:3]) or "a spoken line worth reading"
    return kind, effect, max(0.0, min(1.0, score)), why


def _first_hit(lowered: str, words: Sequence[str]) -> str:
    for word in words:
        if word in lowered:
            return word
    return ""


def _recommendation_boosts(recommendations: RecommendationSet) -> list[tuple]:
    """Source ranges a text recommendation already argued for.

    Session 2 proposed ``text_overlay`` and ``caption_emphasis`` without being
    able to place them. When one of those covers a line, that is independent
    agreement from a different pass, and the line scores higher for it.

    Kept as ranges rather than IDs because a recommendation and a transcript
    line are related by *time*, not by any shared key.
    """
    return [
        (entry.asset_id, entry.start, entry.end, entry.priority)
        for entry in recommendations.recommendations
        if entry.category in ("text_overlay", "caption_emphasis")
        and entry.status in ("accepted", "downgraded")
    ]


def _boost_for(
    boosts: Sequence[tuple], asset_id: str, start: float, end: float
) -> float:
    best = 0.0
    for other_asset, low, high, priority in boosts:
        if other_asset != asset_id:
            continue
        if end > low and start < high:
            best = max(best, priority * 0.2)
    return best


def _duration_for(
    text: str,
    style: StylePreset,
    start: float,
    end: Optional[float],
    placement: Optional[ClipPlacement],
) -> float:
    """How long the caption stays up.

    Long enough to read, never longer than the clip it sits on. Reading time
    dominates: a five-word caption held for four seconds reads as a mistake,
    and a twelve-word one flashed for one second cannot be read at all.
    """
    words = len(_WORD.findall(text)) or 1
    # ~2.6 words per second is a comfortable read for on-screen text.
    reading = 0.6 + words / 2.6
    duration = max(style.caption_duration, reading)
    if end is not None and end > start:
        duration = max(duration, min(duration, end - start))
    if placement is not None:
        room = max(0.4, placement.sequence_end - start)
        duration = min(duration, room)
    return round(max(0.4, duration), 3)


#: Splits a line into sentences, keeping the punctuation on the sentence it
#: belongs to. Whisper punctuates, so this is reliable on real transcripts.
_SENTENCE = re.compile(r"[^.!?]+[.!?]*")


def sentences_in(text: str) -> list[str]:
    """The line's sentences, punctuation intact, empties dropped."""
    return [
        piece.strip() for piece in _SENTENCE.findall(str(text or ""))
        if piece.strip()
    ]


def condense(text: str, max_words: int) -> tuple:
    """Cut a line down to its strongest phrase.

    Returns ``(text, was_condensed)``.

    **A whole sentence beats a window of words**, and that is the first thing
    tried. The word-window fallback below reads straight across a sentence
    boundary and drops the punctuation on the way -- on the first real episode
    it turned "I fell off. What do you mean?" into "I fell off What do...",
    which is on screen, ungrammatical, and cut in the middle of a thought. If
    any sentence in the line fits the budget, it is a better caption than any
    window can be: it is a complete thing somebody said.

    Only when no sentence fits does the window logic run. Then the window with
    the most keyword hits wins, ties going to the earliest, because a viewer
    reads the start of a line and speakers front-load the point. Falling back
    to the first ``max_words`` would keep "okay so anyway I think that was" and
    throw away "a creeper", which is the only part worth reading.
    """
    cleaned = str(text or "").strip()
    words = _WORD.findall(cleaned)
    if len(words) <= max_words:
        return cleaned, False

    keywords = REACTION_WORDS + DANGER_WORDS + EXPLANATORY_WORDS

    def score_of(phrase: str) -> int:
        lowered = phrase.lower()
        return sum(1 for word in keywords if word in lowered)

    # 1. A whole sentence that fits, most keyword hits first.
    candidates = [
        sentence for sentence in sentences_in(cleaned)
        if len(_WORD.findall(sentence)) <= max_words
    ]
    if candidates:
        best = max(
            range(len(candidates)),
            # Negative index breaks ties towards the earlier sentence.
            key=lambda i: (score_of(candidates[i]), -i),
        )
        return candidates[best], True

    # 2. No sentence fits: the strongest window of words.
    best_index, best_score = 0, -1
    for index in range(0, len(words) - max_words + 1):
        window = " ".join(words[index:index + max_words])
        score = score_of(window)
        if score > best_score:
            best_index, best_score = index, score

    chosen = " ".join(words[best_index:best_index + max_words])
    prefix = "..." if best_index > 0 else ""
    suffix = "..." if best_index + max_words < len(words) else ""
    return f"{prefix}{chosen}{suffix}", True


def _clean(text: str) -> str:
    """Strip transcript annotations and tidy whitespace."""
    stripped = _BRACKETED.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", stripped).strip()


def _blocked(
    segment: TimelineSegment,
    start: float,
    end: float,
    blocked_ranges: Sequence[tuple],
) -> tuple:
    """Whether text must not be drawn here, and why."""
    open_ui = sorted({
        name for event in segment.events
        for name in BLOCKING_UI
        if getattr(event.ui, name, False)
    })
    if open_ui:
        return True, (
            f"a full-screen {open_ui[0].replace('_open', '')} is open here, so "
            "an overlay would cover what the viewer is reading"
        )
    for entry in blocked_ranges:
        low, high = float(entry[0]), float(entry[1])
        reason = entry[2] if len(entry) > 2 else "the critic flagged this moment"
        if end > low and start < high:
            return True, reason
    return False, ""


def _as_marker(item: LayerItem, style: StylePreset, reason: str) -> None:
    """Turn a text item into an honest note instead of drawing it.

    The marker has to carry whatever the item was going to say -- the caption
    line, or the entity a callout would have pointed at. That text is the part
    a person cannot reconstruct from the timeline later, so losing it would
    make the marker useless.
    """
    label = item.payload.get("text") or item.payload.get("label") or ""
    if not item.payload.get("placeholder"):
        item.payload["placeholder"] = label or item.kind
    item.payload["zone"] = ""
    item.notes = (item.notes + " | " if item.notes else "") + reason
    if "placeholder_only" not in item.risks:
        item.risks.append("placeholder_only")
    op = {
        "op": "marker.add",
        "time": round(item.start, 3),
        "name": style.marker_name(item.kind),
        "type": "comment",
        "comment": (
            (f'{item.kind.replace("_", " ").upper()}: "{label}" '
             if label else f"{item.kind.replace('_', ' ').upper()}: ")
            + f"| not placed: {reason} | {item.reason} [{item.item_id}]"
        )[:500],
        "note": f"{item.kind} placeholder [{item.item_id}]",
    }
    if item.duration >= 0.25:
        op["duration"] = round(item.duration, 3)
    item.premiere_ops = [op]


def _text_op(
    item: LayerItem, style: StylePreset, zone: str, duration: float
) -> dict:
    """A real ``text.create`` overlay, positioned in a safe zone.

    ``engine="render"`` rather than ``"auto"``: the rasterised path is
    available on every install, while the MOGRT path needs a registered
    template. Asking for the one that always works means a dry run that passes
    here corresponds to an execution that can actually happen.
    """
    return {
        "op": "text.create",
        "text": item.payload["text"],
        "track": OVERLAY_TRACK,
        "time": round(item.start, 3),
        "duration": round(duration, 3),
        "position": list(ZONE_POSITION[zone]),
        "engine": "render",
        "note": f"{item.kind} ({zone}) -- {item.reason[:60]} [{item.item_id}]",
    }
