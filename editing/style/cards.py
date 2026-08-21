"""Title and chapter cards.

A card is the most intrusive thing this system can put on a timeline: it covers
the frame, it costs the viewer time, and a card in the wrong place actively
damages the pacing it was meant to clarify. So cards are the layer with the
fewest, strictest triggers, and only two styles turn them on at all.

A card is planned where the *timeline itself* says a section changed — never on
a clock, never every N minutes:

* **the opening**, which is a title rather than a chapter;
* **after a death, failure or restart**, because that is where a viewer's model
  of what is happening resets;
* **on entering a new environment** — overworld to nether, surface to
  stronghold — which is the clearest section boundary Minecraft footage has;
* **before a stated objective**, when the narration announces the plan;
* **at a long gap in importance**, where a stretch of setup follows a payoff.

Two rules keep them rare. A section must be at least ``min_section_seconds``
long to earn a card, so a flurry of biome changes while running does not
produce a flurry of chapters. And consecutive triggers collapse: a death
*followed by* a biome change is one boundary, not two.

The card's text comes from what is actually known — the environment, the
objective the narration stated, the number of the section. Nothing is invented;
when there is nothing to say, the card becomes a marker asking the editor to
name it.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

from editing.roughcut.schema import RoughCutPlan
from editing.roughcut.select import map_to_sequence
from editing.schema import StructureTimeline, TimelineSegment
from editing.style.captions import OVERLAY_TRACK, _blocked
from editing.style.presets import ZONE_POSITION, StylePreset
from editing.style.schema import LayerEvidence, LayerItem, item_id_for

#: Environments that read as a genuine change of place rather than a few blocks
#: of different terrain. Walking from forest to plains is not a chapter.
MAJOR_ENVIRONMENTS = frozenset({
    "nether", "end", "stronghold", "mineshaft", "ocean_monument", "village",
    "base", "cave", "ocean",
})

#: Phrases that announce an objective. A card before a stated plan is the one
#: place a viewer genuinely wants to be told what is coming.
OBJECTIVE_PHRASES = (
    "the plan is", "the plan was", "we need to", "i need to", "today we",
    "the goal is", "we're going to", "were going to", "i'm going to",
    "im going to", "next up", "time to", "let's build", "lets build",
    "step one", "first we",
)

#: Phrases and states that mark a failure. A restart is a section boundary
#: whether or not anything else changed.
FAILURE_PHRASES = (
    "i died", "we died", "that's a death", "thats a death", "start again",
    "starting over", "back to spawn", "lost everything", "all my stuff",
)

_WORD = re.compile(r"[\w']+")

#: A card is never allowed to be longer than this, whatever the style says --
#: past four seconds a card stops reading as punctuation and starts reading as
#: a stall.
MAX_CARD_SECONDS = 4.0

#: Cards closer together than this are the same boundary seen twice.
MIN_CARD_GAP = 20.0


def build_cards(
    timeline: StructureTimeline,
    roughcut: RoughCutPlan,
    style: StylePreset,
    *,
    blocked_ranges: Sequence[tuple] = (),
) -> list[LayerItem]:
    """Title and chapter cards, where the timeline says a section changed."""
    if not (style.title_cards or style.chapter_cards):
        return []
    if not roughcut.placements:
        return []

    boundaries = _boundaries(timeline, roughcut, style)
    items: list[LayerItem] = []
    last_at = -MIN_CARD_GAP
    index = 0

    for at, kind, title, why, segment in boundaries:
        if kind == "title_card" and not style.title_cards:
            continue
        if kind == "chapter_card" and not style.chapter_cards:
            continue
        if not style.allows(kind):
            continue
        if at - last_at < MIN_CARD_GAP:
            continue
        last_at = at
        index += 1
        items.append(
            _card(at, kind, title, why, segment, roughcut, style, index,
                  blocked_ranges)
        )
    return items


# ---------------------------------------------------------------------------
# Where the sections are
# ---------------------------------------------------------------------------

def _boundaries(
    timeline: StructureTimeline, roughcut: RoughCutPlan, style: StylePreset
) -> list[tuple]:
    """Section starts on the cut, as ``(at, kind, title, why, segment)``."""
    found: list[tuple] = []
    segments = list(timeline.segments)
    if not segments:
        return found

    opening = map_to_sequence(
        roughcut.placements, segments[0].asset_id, segments[0].start
    )
    if opening is not None:
        found.append((
            opening, "title_card", _opening_title(segments),
            "the opening of the cut", segments[0],
        ))

    previous: Optional[TimelineSegment] = None
    section_started = opening if opening is not None else 0.0

    for segment in segments:
        if previous is None:
            previous = segment
            continue

        trigger, title = _trigger(previous, segment)
        if trigger is None:
            previous = segment
            continue

        at = map_to_sequence(
            roughcut.placements, segment.asset_id, segment.start
        )
        if at is None:
            previous = segment
            continue
        if at - section_started < style.min_section_seconds:
            # Too soon to be a new section. A run of biome changes while
            # sprinting is one journey, not six chapters.
            previous = segment
            continue

        found.append((at, "chapter_card", title, trigger, segment))
        section_started = at
        previous = segment

    found.sort(key=lambda entry: entry[0])
    return found


def _trigger(
    previous: TimelineSegment, segment: TimelineSegment
) -> tuple:
    """Whether this segment starts a new section, and what to call it."""
    said = (segment.said or "").lower()

    for phrase in FAILURE_PHRASES:
        if phrase in said:
            return (
                f'the narration reports a failure ("{phrase}")',
                "Starting Over",
            )
    if any(event.ui.death_screen for event in segment.events):
        return "a death screen is visible here", "After the Death"

    was = previous.events[0].environment if previous.events else ""
    now = segment.events[0].environment if segment.events else ""
    if now and now != was and now in MAJOR_ENVIRONMENTS:
        return (
            f"the environment changes from {was or 'unknown'} to {now}",
            _environment_title(now),
        )

    for phrase in OBJECTIVE_PHRASES:
        if phrase in said:
            objective = _objective_from(said, phrase)
            if objective:
                return (
                    f'the narration states an objective ("{phrase}")',
                    objective,
                )
    return None, ""


def _environment_title(environment: str) -> str:
    return {
        "nether": "Into the Nether",
        "end": "The End",
        "stronghold": "The Stronghold",
        "mineshaft": "The Mineshaft",
        "ocean_monument": "The Monument",
        "village": "The Village",
        "base": "Back at Base",
        "cave": "Underground",
        "ocean": "Out to Sea",
    }.get(environment, environment.replace("_", " ").title())


def _objective_from(said: str, phrase: str) -> str:
    """The words just after an objective phrase, title-cased.

    Taken verbatim from the narration rather than summarised. A card that says
    something the person did not say is worse than no card.
    """
    index = said.find(phrase)
    if index < 0:
        return ""
    tail = said[index + len(phrase):]
    words = _WORD.findall(tail)[:5]
    if len(words) < 2:
        return ""
    return " ".join(words).strip().title()


#: How far into the cut the opening narration is still "the opening".
_OPENING_SECONDS = 20.0


def _opening_title(segments: Sequence[TimelineSegment]) -> str:
    """A title for the top of the cut, from the first thing that happens.

    A stated objective wins, and is looked for across the opening narration --
    "the plan is to find diamonds" is the video's subject wherever in the first
    few seconds it lands.

    The fallback names the place **the video opens in**, from the first segment
    only. Scanning further ahead produced titles like "Into the Nether" over an
    opening shot in a forest: a real place, named a minute early, which is the
    inventing-a-title failure this module exists to avoid.
    """
    start = segments[0].start if segments else 0.0
    for segment in segments:
        if segment.start - start > _OPENING_SECONDS:
            break
        for phrase in OBJECTIVE_PHRASES:
            if phrase in (segment.said or "").lower():
                objective = _objective_from(segment.said.lower(), phrase)
                if objective:
                    return objective

    first = segments[0] if segments else None
    if first is not None and first.events:
        environment = first.events[0].environment
        if environment and environment != "unknown":
            return _environment_title(environment)
    return ""


# ---------------------------------------------------------------------------
# Building one card
# ---------------------------------------------------------------------------

def _card(
    at: float,
    kind: str,
    title: str,
    why: str,
    segment: TimelineSegment,
    roughcut: RoughCutPlan,
    style: StylePreset,
    index: int,
    blocked_ranges: Sequence[tuple],
) -> LayerItem:
    duration = min(MAX_CARD_SECONDS, max(0.8, style.card_duration))
    placement = roughcut.placement_at(at)
    if placement is not None:
        duration = min(duration, max(0.8, placement.sequence_end - at))

    item = LayerItem(
        item_id=item_id_for(kind, at, title or str(index)),
        layer="title",
        kind=kind,
        placement_id=placement.placement_id if placement else "",
        start=at,
        end=at + duration,
        source_start=segment.start,
        source_end=segment.end,
        asset_id=segment.asset_id,
        style=style.name,
        reason=why,
        effect="structure",
        intensity="medium",
        # Cards are structural rather than decorative, so they rank above
        # every caption a line can score: when a density ceiling bites, losing
        # the chapter marker that gives the video its shape costs more than
        # losing one caption or one punch-in, and a caption dropped here is
        # still readable in the transcript.
        priority=0.92 if kind == "title_card" else 0.86,
        evidence=LayerEvidence(
            visual_event_ids=[e.event_id for e in segment.events][:5],
            transcript_quotes=[segment.said[:200]] if segment.said else [],
            segment_ids=[segment.segment_id],
            summary=segment.summary()[:200],
        ),
        payload={
            "text": title,
            "index": index,
            "zone": "center",
            "seconds": round(duration, 3),
        },
    )

    blocked, block_reason = _blocked(segment, at, at + duration, blocked_ranges)
    if not title:
        _as_marker(item, style,
                   "the timeline says a section starts here but nothing in it "
                   "names the section, so the editor should title it")
    elif blocked:
        _as_marker(item, style, block_reason)
    elif not style.allow_real_text:
        _as_marker(item, style,
                   f"the {style.name} style leaves text as a note for the "
                   "editor rather than drawing it")
    else:
        item.premiere_ops = [{
            "op": "text.create",
            "text": title,
            "track": OVERLAY_TRACK,
            "time": round(at, 3),
            "duration": round(duration, 3),
            "position": list(ZONE_POSITION["center"]),
            "engine": "render",
            "note": f"{kind} '{title}' -- {why[:60]} [{item.item_id}]",
        }]
    return item


def _as_marker(item: LayerItem, style: StylePreset, reason: str) -> None:
    item.payload["placeholder"] = item.payload.get("text") or item.kind
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
            f"{item.kind.replace('_', ' ').upper()}"
            + (f': "{item.payload.get("text")}"' if item.payload.get("text")
               else " (untitled)")
            + f" | {item.reason} | not drawn: {reason} [{item.item_id}]"
        )[:500],
        "note": f"{item.kind} placeholder [{style.name}]",
    }
    if item.duration >= 0.25:
        op["duration"] = round(item.duration, 3)
    item.premiere_ops = [op]
