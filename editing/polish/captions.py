"""Key-moment captions: the few lines worth reading.

Session 5's caption layer answers "which spoken lines could carry text". This
one answers a narrower question: **which lines are the episode**. It is the
difference between a styled edit and subtitles, and it is entirely subtractive.

## How a line earns a caption

Three gates, in order, and a line has to pass all three.

1. **Is it legible?** Long lines, filler, ASR uncertainty markers and
   low-confidence speech are refused before anything looks at what they mean.
   A caption that misquotes the audio is worse than no caption, because a
   viewer can hear the difference.
2. **Is it a key moment?** One of the nine kinds in ``KEY_MOMENTS``, argued for
   by the picture, the audio and the words *together* -- never by the words
   alone. "oh my god" over a crafting table is not a reaction caption.
3. **Does it fit the budget?** Candidates are ranked and the ceiling is
   applied. A refusal here is normal and is recorded as ``density_limit``, so
   a report can say "eleven earned one, four fitted".

## What it will not do

It will not caption a line the cut removed, nudge one to a nearby frame, write
text nobody said, or place anything over a full-screen menu. Each of those is
a named rejection rather than a silent skip, because a caption pass that
quietly drops half its work is indistinguishable from one that is broken.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

from editing.polish.schema import (
    FILLER_LINES, STRUCTURAL_MOMENTS, UNCLEAR_MARKERS, CaptionConfig,
    CaptionDecision, CaptionPlan, caption_id_for, now,
)
from editing.roughcut.schema import RoughCutPlan
from editing.roughcut.select import map_to_sequence
from editing.schema import StructureTimeline, TimelineSegment, TranscriptEntry
from editing.style.captions import (
    BLOCKING_UI, DANGER_WORDS, EXPLANATORY_WORDS, REACTION_WORDS, condense,
)
from editing.style.presets import StylePreset

_WORD = re.compile(r"[\w']+")
_BRACKETED = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_ANNOTATION = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*$")

# ``BLOCKING_UI`` -- the HUD and menu states that make any overlay a bad idea
# -- is imported from the style layer rather than restated, so the two passes
# cannot drift apart about what "a menu is open" means.

#: Words that mark a line as admitting a failure. Short and literal, like every
#: other list in this system: a keyword list whose mistakes are predictable
#: beats a classifier whose mistakes are not.
FAIL_WORDS = (
    "i died", "we died", "i'm dead", "im dead", "we're dead", "were dead",
    "that killed me", "lost everything", "lost it all", "dropped my",
    "i lost", "we lost", "that was a disaster", "went wrong", "my stuff",
    "back to spawn", "respawn",
)

#: Lines that state what the episode is trying to do.
OBJECTIVE_WORDS = (
    "the plan is", "the plan was", "we need to", "i need to", "the goal",
    "today we", "in this episode", "we're going to", "were going to",
    "i'm going to", "im going to", "the mission", "trying to find",
    "looking for", "let's find", "lets find", "by the end",
)

#: Lines that mark the hinge between two sections.
TRANSITION_WORDS = (
    "right so", "okay so now", "next up", "moving on", "after that",
    "first things first", "now that", "with that done", "meanwhile",
    "back at", "let's head", "lets head", "time to",
)

#: Lines that land something set up earlier.
PAYOFF_WORDS = (
    "finally", "there it is", "we did it", "that worked", "it worked",
    "got it", "found it", "there we go", "that's it", "thats it",
    "at last", "knew it", "told you",
)

#: Lines that point back at something earlier in the episode.
CALLBACK_WORDS = (
    "like i said", "remember", "earlier", "as i mentioned", "same as before",
    "again", "like last time", "told you so", "back when",
)

#: Lines that read as a reveal.
REVEAL_WORDS = (
    "look at that", "look at this", "check this out", "what is that",
    "is that a", "there's a", "theres a", "no way that's", "it's a",
    "i found", "we found", "diamonds", "ancient debris", "netherite",
)

#: Audio types that agree a moment landed.
REACTION_AUDIO = frozenset({
    "sudden_reaction", "possible_scream", "loudness_spike",
})

#: A line longer than this many seconds is a paragraph, not a caption.
MAX_SOURCE_SECONDS = 8.0

#: A line with more words than this is not condensed down -- it is refused.
#: Condensing a thirty-word sentence to five words does not summarise it, it
#: picks five words out of it and pretends they were the sentence.
MAX_CONDENSE_WORDS = 22

#: Shorter than this is a fragment.
MIN_CAPTION_CHARS = 3


def build_caption_plan(
    timeline: StructureTimeline,
    cut: RoughCutPlan,
    style: StylePreset,
    config: CaptionConfig,
    *,
    memory=None,
    name: str = "structure",
) -> CaptionPlan:
    """Every transcript line, judged. Returns accepted and refused together.

    ``memory`` is an optional ``EpisodeMemory``. When there is one, the
    objective and the callbacks it found are used as evidence -- a line that
    restates the episode's stated objective is a stronger candidate than one
    that merely sounds like a plan.
    """
    config = config.validated()
    plan = CaptionPlan(
        name=name,
        mode=config.mode,
        config=config,
        style=style.name,
        sequence_name=cut.sequence_name,
        cut_duration=round(cut.total_duration, 3),
        generated_at=now(),
        warnings=list(config.warnings),
    )

    if not config.enabled:
        plan.safety_notes.append(
            "No line was considered: captions are off for this run."
        )
        return plan

    if not style.text_allowed:
        plan.warnings.append(
            f"the {style.name} style allows no captions at all "
            f"(max_captions_per_minute is {style.max_captions_per_minute}), "
            "so every line was refused before it was read."
        )
        for entry, segment in _lines(timeline):
            plan.decisions.append(_refused(
                entry, segment, "style_forbids_text",
                f"the {style.name} style puts no text on screen",
            ))
        return plan

    context = _EpisodeContext(memory)
    seen_texts: set = set()
    candidates: list[CaptionDecision] = []

    for entry, segment in _lines(timeline):
        decision = _consider(
            entry, segment, cut, style, config, context, seen_texts)
        plan.decisions.append(decision)
        if decision.accepted:
            candidates.append(decision)

    _apply_budget(candidates, config, plan)

    if not style.allow_real_text:
        plan.safety_notes.append(
            f"the {style.name} style leaves text as a note for the editor "
            "rather than drawing it, so these are a caption plan and not an "
            "overlay."
        )
    plan.safety_notes.append(
        "Nothing here is burned into any video. The proxy render assembles "
        "V1 only; the sidecar subtitle file is how to see these against it."
    )
    plan.decisions.sort(key=lambda d: (d.start if d.start >= 0 else 1e9,
                                       d.source_start))
    return plan


# ---------------------------------------------------------------------------
# One line
# ---------------------------------------------------------------------------

def _consider(
    entry: TranscriptEntry,
    segment: TimelineSegment,
    cut: RoughCutPlan,
    style: StylePreset,
    config: CaptionConfig,
    context: "_EpisodeContext",
    seen_texts: set,
) -> CaptionDecision:
    """Judge one line. Always returns a record, accepted or not."""
    text = _clean(entry.text)
    lowered = text.lower()

    # -- gate 1: is it legible? ------------------------------------------
    if len(text) < MIN_CAPTION_CHARS or _ANNOTATION.match(entry.text or ""):
        return _refused(entry, segment, "repeated_filler",
                        "the line is an annotation or a fragment, not speech")

    if any(marker in (entry.text or "").lower() for marker in UNCLEAR_MARKERS):
        return _refused(
            entry, segment, "unclear_transcript",
            "the transcript marks this line as not clearly heard")

    if lowered.strip(" .,!?") in FILLER_LINES:
        return _refused(entry, segment, "repeated_filler",
                        f'the whole line is filler ("{text[:40]}")')

    if config.require_confidence and not _has_confidence(entry):
        return _refused(
            entry, segment, "low_confidence",
            "this transcript carries no confidence figure and "
            "--require-caption-confidence was set")

    if _has_confidence(entry) and entry.confidence < config.min_confidence:
        return _refused(
            entry, segment, "low_confidence",
            f"speech confidence {entry.confidence:.2f} is below the "
            f"{config.min_confidence:.2f} a caption needs")

    if entry.duration > MAX_SOURCE_SECONDS:
        return _refused(
            entry, segment, "too_long",
            f"the line runs {entry.duration:.1f}s, which is a paragraph "
            f"rather than a caption (limit {MAX_SOURCE_SECONDS:.0f}s)")

    words = _WORD.findall(text)
    if len(words) > MAX_CONDENSE_WORDS:
        return _refused(
            entry, segment, "too_many_words",
            f"{len(words)} words. Condensing it to {config.max_words} would "
            "pick a phrase out of a sentence and present it as the sentence")

    if _is_background(entry, segment):
        return _refused(
            entry, segment, "background_speech",
            "quiet speech over a low-energy stretch: this reads as "
            "background rather than commentary")

    # -- gate 2: is it a key moment? -------------------------------------
    moment, priority, reason, evidence = _classify(
        text, lowered, segment, context)

    if config.key_moments_only and not moment:
        return _refused(entry, segment, "not_a_key_moment",
                        reason or "nothing in the picture or the audio makes "
                                  "this line one of the moments that carry "
                                  "the episode", text=text)

    if not moment and _is_explanation(lowered) and config.key_moments_only:
        return _refused(entry, segment, "boring_explanation",
                        "the line explains rather than lands", text=text)

    if priority < config.min_priority:
        return _refused(
            entry, segment, "not_a_key_moment",
            f"scored {priority:.2f}, below the {config.min_priority:.2f} this "
            f"style asks for", text=text)

    # -- placement --------------------------------------------------------
    start = map_to_sequence(cut.placements, segment.asset_id, entry.start)
    if start is None:
        return _refused(
            entry, segment, "cut_from_the_edit",
            "this line is not in the cut, and captioning footage nobody kept "
            "would put text over a moment that no longer exists", text=text)

    blocked = _blocking_ui(segment)
    if blocked:
        return _refused(
            entry, segment, "blocked_by_ui",
            f"a full-screen {blocked.replace('_open', '')} is open here, so "
            "an overlay would cover what the viewer is reading",
            text=text, start=start)

    zone = style.zone_for("reaction_caption")
    if zone is None:
        return _refused(
            entry, segment, "no_safe_zone",
            f"the {style.name} style has no safe zone left for text over "
            "gameplay", text=text, start=start)

    condensed, was_condensed = condense(text, config.max_words)
    key = condensed.strip().lower()
    if key in seen_texts:
        return _refused(
            entry, segment, "duplicate_line",
            f'"{condensed[:40]}" has already been captioned once',
            text=condensed, start=start)
    seen_texts.add(key)

    placement = cut.placement_at(start)
    duration = _duration_for(condensed, config, start, placement)

    decision = CaptionDecision(
        caption_id=caption_id_for(moment or "key_phrase", start, condensed),
        accepted=True,
        moment=moment,
        text=condensed,
        full_line=text[:300],
        condensed=was_condensed,
        start=round(start, 3),
        end=round(start + duration, 3),
        source_start=entry.start,
        source_end=entry.end,
        asset_id=segment.asset_id,
        segment_id=segment.segment_id,
        placement_id=placement.placement_id if placement else "",
        zone=zone,
        priority=round(priority, 3),
        confidence=entry.confidence,
        reason=reason,
        evidence=evidence[:8],
    )
    return decision


def _refused(
    entry: TranscriptEntry,
    segment: TimelineSegment,
    code: str,
    detail: str,
    *,
    text: str = "",
    start: Optional[float] = None,
) -> CaptionDecision:
    body = text or _clean(entry.text)
    return CaptionDecision(
        caption_id=caption_id_for(code, entry.start, body),
        accepted=False,
        text=body[:300],
        full_line=_clean(entry.text)[:300],
        start=round(start, 3) if start is not None else -1.0,
        end=-1.0,
        source_start=entry.start,
        source_end=entry.end,
        asset_id=segment.asset_id,
        segment_id=segment.segment_id,
        confidence=entry.confidence,
        reason=detail,
        reject_reason=code,
        reject_detail=detail,
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class _EpisodeContext:
    """What the episode memory knows, in the shape this module asks for."""

    def __init__(self, memory=None):
        self.objective = ""
        self.callback_ranges: list[tuple] = []
        self.payoff_ranges: list[tuple] = []
        if memory is None:
            return
        main = getattr(memory, "main_objective", None)
        if main is not None:
            self.objective = str(getattr(main, "text", "") or "").lower()
        for callback in getattr(memory, "callbacks", ()) or ():
            self.callback_ranges.append((
                float(getattr(callback, "start", 0.0) or 0.0),
                float(getattr(callback, "end", 0.0) or 0.0),
            ))
        for payoff in getattr(memory, "payoffs", ()) or ():
            self.payoff_ranges.append((
                float(getattr(payoff, "start", 0.0) or 0.0),
                float(getattr(payoff, "end", 0.0) or 0.0),
            ))

    def restates_objective(self, lowered: str) -> bool:
        """Whether this line names what the memory says the episode is about.

        Word overlap rather than similarity: the objective text came from a
        transcript too, so the words are literally shared when it is the same
        subject, and a fuzzy match here would be a fuzzy match on nothing.
        """
        if not self.objective:
            return False
        theirs = {w for w in _WORD.findall(self.objective) if len(w) > 4}
        mine = {w for w in _WORD.findall(lowered) if len(w) > 4}
        return len(theirs & mine) >= 2


def _classify(
    text: str,
    lowered: str,
    segment: TimelineSegment,
    context: _EpisodeContext,
) -> tuple:
    """Which key moment this line is, how strongly, and why.

    Returns ``(moment, priority, reason, evidence)`` with ``moment`` empty when
    the line is not one of the nine kinds. Every claim needs agreement from
    more than one channel: the words say what was said, the picture says what
    was happening, and neither is trusted alone.
    """
    importance = segment.importance
    audio_types = segment.audio_types()
    laughter = "possible_laughter" in audio_types
    reaction = bool(REACTION_AUDIO & audio_types)
    threats = sorted({
        threat for event in segment.events for threat in event.threats})
    death = any(getattr(event.ui, "death_screen", False)
                for event in segment.events)

    evidence: list[str] = []
    if importance:
        evidence.append(f"the picture here is a {importance}")
    if audio_types:
        evidence.append("audio: " + ", ".join(sorted(audio_types)[:3]))
    if threats:
        evidence.append("threats on screen: " + ", ".join(threats[:3]))
    if death:
        evidence.append("a death screen is on screen")

    candidates: list[tuple] = []

    if death or _hit(lowered, FAIL_WORDS):
        strength = 0.9 if death else 0.62
        candidates.append((
            "death_or_fail", strength,
            "a death screen is on screen and the line reacts to it" if death
            else f'the line admits a failure ("{_hit(lowered, FAIL_WORDS)}")'))

    if laughter and _hit(lowered, REACTION_WORDS):
        candidates.append((
            "funny_reaction", 0.78,
            "laughter around a line that reacts -- the joke is audible"))
    elif laughter and len(_WORD.findall(text)) <= 8:
        candidates.append((
            "funny_reaction", 0.6,
            "laughter around a short line"))

    if threats and _hit(lowered, DANGER_WORDS):
        candidates.append((
            "danger", 0.8,
            f'the line names the threat on screen '
            f'("{_hit(lowered, DANGER_WORDS)}")'))
    elif importance == "danger" and _hit(lowered, DANGER_WORDS):
        candidates.append((
            "danger", 0.66,
            f'the player is in danger and the line says so '
            f'("{_hit(lowered, DANGER_WORDS)}")'))

    if importance == "reveal" and (reaction or _hit(lowered, REVEAL_WORDS)):
        candidates.append((
            "reveal", 0.82,
            "the picture is a reveal and the line points at it"))

    if importance == "payoff" and _hit(lowered, PAYOFF_WORDS):
        candidates.append((
            "payoff_line", 0.85,
            f'the payoff lands and the line names it '
            f'("{_hit(lowered, PAYOFF_WORDS)}")'))
    elif _in_any(segment, context.payoff_ranges) and _hit(lowered, PAYOFF_WORDS):
        candidates.append((
            "payoff_line", 0.7,
            "the episode memory has a payoff here and the line lands it"))

    if _hit(lowered, CALLBACK_WORDS) and _in_any(segment,
                                                 context.callback_ranges):
        candidates.append((
            "callback", 0.72,
            "the episode memory recorded a callback here and the line "
            "refers back"))
    elif _hit(lowered, CALLBACK_WORDS) and importance in ("payoff", "reveal"):
        candidates.append((
            "callback", 0.58,
            f'the line refers back ("{_hit(lowered, CALLBACK_WORDS)}") at a '
            f"moment that lands"))

    if _hit(lowered, OBJECTIVE_WORDS):
        strength = 0.8 if context.restates_objective(lowered) else 0.6
        candidates.append((
            "objective", strength,
            "the line states what the episode is trying to do"
            + (", and it matches the objective the episode memory found"
               if strength > 0.7 else "")))

    if _hit(lowered, TRANSITION_WORDS) and importance in ("setup", "boring"):
        candidates.append((
            "transition_setup", 0.55,
            f'the line hinges between two sections '
            f'("{_hit(lowered, TRANSITION_WORDS)}")'))

    if (reaction and len(_WORD.findall(text)) <= 6
            and importance in ("payoff", "reveal", "danger", "funny")):
        candidates.append((
            "meme_quote", 0.68,
            "short, said with force, over a moment that lands"))

    if not candidates:
        return "", 0.0, _why_not(importance, audio_types), evidence

    # Structural moments beat reactive ones on a tie: losing the objective
    # line costs a viewer the plot, losing one "oh my god" costs them nothing.
    candidates.sort(key=lambda item: (
        -item[1], 0 if item[0] in STRUCTURAL_MOMENTS else 1, item[0]))
    moment, priority, reason = candidates[0]

    if segment.alignment == "contrast":
        priority = min(1.0, priority + 0.06)
        evidence.append("the words and the picture disagree, which reads as a "
                        "joke")
    if segment.usefulness:
        priority = min(1.0, priority + min(0.08, segment.usefulness * 0.08))
    return moment, priority, reason, evidence


def _why_not(importance: str, audio_types: set) -> str:
    if importance == "boring":
        return ("nothing is happening on screen and the audio is ordinary, so "
                "this is talking rather than a moment")
    if not audio_types:
        return (f"the picture is a {importance} but nothing in the audio or "
                "the words marks this line as the moment itself")
    return ("the line is clear, but nothing makes it one of the nine moments "
            "this pass captions")


def _is_explanation(lowered: str) -> bool:
    return bool(_hit(lowered, EXPLANATORY_WORDS))


def _is_background(entry: TranscriptEntry, segment: TimelineSegment) -> bool:
    """Quiet speech over a stretch measured as low energy.

    A heuristic, and named as one. There is no diarisation anywhere in this
    system, so "somebody else in the room" cannot be detected -- what can be
    detected is speech the ASR was unsure of, over audio that was measured
    quiet, which is the same evidence pointing the same way.
    """
    if not _has_confidence(entry) or entry.confidence >= 0.75:
        return False
    quiet = {"low_energy", "silence"} & segment.audio_types()
    return bool(quiet) and segment.audio_reaction is None


def _has_confidence(entry: TranscriptEntry) -> bool:
    """Whether the transcript carried a real confidence figure.

    ``TranscriptEntry`` defaults to 1.0, which means "nobody said", not "very
    sure". Treating the default as a measurement would let a hand-typed SRT
    outrank a Whisper transcript that reported 0.9.
    """
    return entry.confidence < 1.0


def _hit(lowered: str, words: Sequence[str]) -> str:
    for word in words:
        if word in lowered:
            return word
    return ""


def _in_any(segment: TimelineSegment, ranges: Sequence[tuple]) -> bool:
    for low, high in ranges:
        if segment.end > low and segment.start < high:
            return True
    return False


def _blocking_ui(segment: TimelineSegment) -> str:
    for event in segment.events:
        for name in sorted(BLOCKING_UI):
            if getattr(event.ui, name, False):
                return name
    return ""


def _clean(text: str) -> str:
    stripped = _BRACKETED.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", stripped).strip()


def _lines(timeline: StructureTimeline):
    for segment in timeline.segments:
        for entry in segment.speech_entries:
            yield entry, segment


def _duration_for(
    text: str, config: CaptionConfig, start: float, placement
) -> float:
    """How long a caption stays up.

    Long enough to read, never past the clip it sits on, never past the
    configured ceiling. Reading time dominates: ~2.6 words a second is a
    comfortable read, and a five-word caption held for six seconds reads as
    stuck.
    """
    words = len(_WORD.findall(text)) or 1
    reading = 0.8 + words / 2.6
    duration = min(max(1.2, reading), config.max_seconds)
    if placement is not None:
        room = max(0.4, placement.sequence_end - start)
        duration = min(duration, room)
    return round(max(0.4, duration), 3)


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------

def _apply_budget(
    candidates: list[CaptionDecision],
    config: CaptionConfig,
    plan: CaptionPlan,
) -> None:
    """Thin the accepted set until it fits the ceilings.

    Spacing is applied first and the rate second, because spacing is a local
    rule a person can see (two captions on top of each other) while the rate is
    a global one they cannot. Anything removed becomes a rejection with a named
    reason, never a silent drop.
    """
    if not candidates:
        return

    minutes = max(plan.cut_duration, 1.0) / 60.0
    allowed = int(config.max_per_minute * minutes)
    if config.max_per_minute > 0 and allowed < 1:
        # Never let a short cut round down to nothing: one caption on a
        # forty-second clip is what the rate means at that length. It does
        # push the measured rate above the ceiling, so the plan says so rather
        # than printing a number that looks like a broken limit.
        allowed = 1
        plan.safety_notes.append(
            f"the cut is {plan.cut_duration:.0f}s, so "
            f"{config.max_per_minute:.2f} captions a minute rounds down to "
            "none. One was allowed, which reads above the ceiling in the "
            "density figure."
        )
    allowed = min(allowed, config.max_total)

    # Strongest first, so what survives the ceiling is what was argued for
    # best rather than what happened to be early.
    ranked = sorted(
        candidates,
        key=lambda d: (-d.priority,
                       0 if d.moment in STRUCTURAL_MOMENTS else 1,
                       d.start),
    )

    kept: list[CaptionDecision] = []
    for decision in ranked:
        clash = _too_close(decision, kept, config.min_spacing)
        if clash is not None:
            _reject(decision, "too_close_to_another",
                    f"{abs(decision.start - clash.start):.1f}s from the "
                    f'caption "{clash.text[:30]}", and this style asks for '
                    f"{config.min_spacing:.0f}s between two")
            continue
        if len(kept) >= allowed:
            _reject(decision, "density_limit",
                    f"the budget for this cut is {allowed} caption(s) "
                    f"({config.max_per_minute:.1f} a minute over "
                    f"{plan.cut_duration:.0f}s), and stronger moments filled "
                    "it")
            continue
        kept.append(decision)

    if len(kept) < len(candidates):
        plan.warnings.append(
            f"{len(candidates)} line(s) earned a caption and {len(kept)} "
            "fitted the budget. The rest are in the plan with the rule that "
            "refused them."
        )


def _too_close(
    decision: CaptionDecision,
    kept: Sequence[CaptionDecision],
    spacing: float,
) -> Optional[CaptionDecision]:
    for other in kept:
        if abs(decision.start - other.start) < spacing:
            return other
        # Overlap is a clash whatever the spacing says: two captions on screen
        # at once is the one outcome this pass must never produce.
        if decision.start < other.end and other.start < decision.end:
            return other
    return None


def _reject(decision: CaptionDecision, code: str, detail: str) -> None:
    decision.accepted = False
    decision.reject_reason = code
    decision.reject_detail = detail
    decision.zone = ""
