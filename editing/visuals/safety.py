"""The rules that stop this layer being embarrassing.

Fourteen deterministic checks. They run in a fixed order, they record what they
saw whether or not they acted, and the first one is about the HUD because that
is the one a style may not override.

## Lower before refusing

A check reaches for :func:`weaker` before it reaches for a rejection wherever
that is honest. A punch that is too strong for the moment is usually a *softer*
punch rather than no punch, and refusing outright throws away a real
observation that the earlier passes paid for. Only the checks where a softer
version would still be wrong — a callout with nothing to point at, an effect
over an open inventory — refuse outright.

## What each check is protecting

* **The HUD.** Minecraft's health, hunger and hotbar are information the viewer
  is reading. Scaling the picture pushes them off the edge and covering them
  hides the thing the moment is about. No style may override this.
* **The footage.** A freeze frame in the middle of a fight stops the thing the
  viewer came for. A shake while somebody is aiming makes the aim unreadable.
* **The viewer's patience.** Two effects on top of each other, the same effect
  six times, a label over a caption, an opening that already has a caption and
  a sting and now wants a title card too.
* **The evidence.** A moment nobody was sure about, a transcript nobody could
  hear, a vision label with nothing named in it.

Every one of them records a :class:`VisualSafetyCheck` on the treatment. A plan
where the HUD check never ran and a plan where it ran and passed look identical
from the outside otherwise, and only one of them is safe.
"""
from __future__ import annotations

from typing import Optional, Sequence

from editing.visuals.schema import (
    MARKER_EFFECTS, NEEDS_TARGET, PICTURE_EFFECTS, TEXT_EFFECTS, TIME_EFFECTS,
    VisualConfig, VisualMoment, VisualSafetyCheck, VisualTreatment, weaker,
)

#: HUD states that make any overlay or scale a bad idea at that moment. The
#: same set the caption pass refuses text over, for the same reason.
BLOCKING_HUD = ("inventory_open", "crafting_open", "chest_open", "map_open",
                "death_screen")

#: A zoom past this scale starts pushing the hotbar and health bar off a 16:9
#: frame. Below it the HUD survives; above it the thing the viewer is reading
#: is the first thing to go.
HUD_SAFE_SCALE = 112.0

#: And the tighter ceiling for a moment where the health bar is the point.
LOW_HEALTH_SAFE_SCALE = 104.0

#: A clip shorter than the effect plus this margin cannot carry it.
CLIP_MARGIN = 0.4

#: Importance levels where stopping the footage is stopping the thing the
#: viewer came for.
CRITICAL_ACTION = frozenset({"danger", "tension"})

#: An opening carrying this many things already does not need another.
HOOK_POLISH_LIMIT = 2

#: Vision confidence below which a label is a guess rather than an observation.
WEAK_LABEL_CONFIDENCE = 0.55


def check_all(
    treatment: VisualTreatment,
    moment: VisualMoment,
    config: VisualConfig,
    *,
    style=None,
    placement=None,
    captions: Sequence[tuple] = (),
    kept: Sequence[VisualTreatment] = (),
    effect_counts: Optional[dict] = None,
    hook_polish: int = 0,
    segment_importance: str = "",
) -> VisualTreatment:
    """Run every check over one treatment, in order. Mutates and returns it.

    Stops at the first refusal: once a treatment is rejected the later checks
    are describing something that will not happen, and recording their opinion
    of it would pad the report without informing it.
    """
    for check in (
        _hud,
        _unknown_target,
        _low_confidence,
        _transcript_confidence,
        _weak_label,
        _clip_too_short,
        _too_long,
        _interrupts_action,
        _shake_during_combat,
        _caption_overlap,
        _repeated_effect,
        _hook_already_polished,
        _too_close,
    ):
        result = check(
            treatment, moment, config,
            style=style, placement=placement, captions=captions, kept=kept,
            effect_counts=effect_counts or {}, hook_polish=hook_polish,
            segment_importance=segment_importance,
        )
        if result is None:
            continue
        treatment.checks.append(result)
        if result.outcome == "reject":
            treatment.accepted = False
            treatment.reject_reason = result.name
            treatment.reject_detail = result.reason
            return treatment
        if result.outcome == "lowered":
            treatment.lowered = True
            treatment.safety_notes.append(result.reason)

    treatment.accepted = True
    return treatment


def _pass(name: str, reason: str = "", **evidence) -> VisualSafetyCheck:
    return VisualSafetyCheck(
        name=name, outcome="pass", reason=reason, evidence=evidence)


def _reject(name: str, reason: str, **evidence) -> VisualSafetyCheck:
    return VisualSafetyCheck(
        name=name, outcome="reject", reason=reason, evidence=evidence)


def _lower(name: str, reason: str, **evidence) -> VisualSafetyCheck:
    return VisualSafetyCheck(
        name=name, outcome="lowered", reason=reason, evidence=evidence)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def _hud(treatment, moment, config, **_kwargs):
    """The HUD comes first, and no style may override it.

    Two ways an effect damages it: covering it with something, and scaling the
    picture until it leaves the frame. Both are checked, and the second is
    checked against a tighter ceiling when the health bar is the point.
    """
    open_ui = [name for name in BLOCKING_HUD if moment.hud.get(name)]
    covers = treatment.effect in TEXT_EFFECTS or treatment.effect in NEEDS_TARGET
    scales = treatment.effect in PICTURE_EFFECTS

    if open_ui and (covers or scales):
        # A death screen is the exception a freeze frame is *for*: the frame a
        # viewer wants held is the one with the death message on it, and
        # holding it covers nothing that was not already full-screen.
        if open_ui == ["death_screen"] and treatment.effect == "freeze_frame":
            return _pass("hides_hud",
                         "a freeze on the death screen holds what the viewer "
                         "is already reading",
                         open_ui=open_ui)
        return _reject(
            "hides_hud",
            f"a full-screen {open_ui[0].replace('_open', '')} is open here, "
            f"and a {treatment.effect} would cover what the viewer is reading",
            open_ui=open_ui, effect=treatment.effect)

    if scales:
        scale = float(treatment.payload.get("scale") or 100.0)
        ceiling = (LOW_HEALTH_SAFE_SCALE if moment.hud.get("low_health")
                   else HUD_SAFE_SCALE)
        if scale > ceiling:
            lowered = weaker(treatment.intensity)
            treatment.intensity = lowered
            treatment.payload["scale"] = ceiling
            why = ("the health bar is the point of this moment"
                   if moment.hud.get("low_health")
                   else "the hotbar and health bar start leaving a 16:9 frame")
            return _lower(
                "hides_hud",
                f"scaled back to {ceiling:.0f}% because {why}",
                scale=scale, ceiling=ceiling, intensity=lowered)
        return _pass("hides_hud", "the HUD survives this scale",
                     scale=scale, ceiling=ceiling)

    return _pass("hides_hud", "this effect neither covers nor scales the frame")


def _unknown_target(treatment, moment, config, **_kwargs):
    """A callout with nothing to point at is the most obviously wrong thing.

    The vision pass names what it sees. When it named nothing, an arrow is
    pointing at the middle of the frame and hoping, which a viewer reads
    immediately.
    """
    if treatment.effect not in NEEDS_TARGET:
        return None
    target = str(treatment.payload.get("target") or "")
    if not target:
        return _reject(
            "unknown_target",
            f"a {treatment.effect} needs something to point at and the vision "
            "pass named nothing on screen here",
            entities=len(moment.entities))
    # Position is genuinely unknown even when the entity is named: this system
    # knows *what* is on screen, never *where*. A person has to place it.
    treatment.safety_notes.append(
        f"the target is '{target}' and its position on screen is not known -- "
        "this points at a thing, not at a place")
    return _pass("unknown_target", f"pointing at {target}", target=target)


def _low_confidence(treatment, moment, config, **_kwargs):
    if moment.confidence >= config.min_confidence:
        return _pass("low_confidence", "the evidence is strong enough",
                     confidence=moment.confidence)
    return _reject(
        "low_confidence",
        f"the moment scored {moment.confidence:.2f}, below the "
        f"{config.min_confidence:.2f} an effect needs",
        confidence=moment.confidence, floor=config.min_confidence)


def _transcript_confidence(treatment, moment, config, **_kwargs):
    """A label built on speech nobody could hear will be wrong on screen."""
    if moment.source_type != "transcript":
        return None
    if moment.transcript_confidence < 0:
        return _pass("low_transcript_confidence",
                     "this transcript carries no confidence figures")
    if moment.transcript_confidence >= config.min_transcript_confidence:
        return _pass("low_transcript_confidence", "the speech was clear",
                     confidence=moment.transcript_confidence)
    return _reject(
        "low_transcript_confidence",
        f"speech confidence {moment.transcript_confidence:.2f} is below the "
        f"{config.min_transcript_confidence:.2f} a transcript-driven effect "
        "needs",
        confidence=moment.transcript_confidence)


def _weak_label(treatment, moment, config, **_kwargs):
    """A vision finding with nothing named in it cannot drive a picture change.

    A card or a marker is fine on a weak label -- it says something happened.
    Scaling the picture on one is acting on a guess.
    """
    if moment.source_type != "visual":
        return None
    if treatment.effect not in PICTURE_EFFECTS:
        return None
    if moment.entities or moment.confidence >= WEAK_LABEL_CONFIDENCE:
        return _pass("weak_visual_label", "the vision pass named what it saw",
                     entities=len(moment.entities),
                     confidence=moment.confidence)
    return _reject(
        "weak_visual_label",
        f"the vision pass named nothing here and scored {moment.confidence:.2f}"
        f", which is not enough to change the picture on",
        confidence=moment.confidence)


def _clip_too_short(treatment, moment, config, placement=None, **_kwargs):
    if placement is None:
        return None
    room = float(getattr(placement, "sequence_end", 0.0)) - treatment.start
    needed = treatment.duration + CLIP_MARGIN
    if room >= needed:
        return _pass("clip_too_short", "the clip can carry it",
                     room=round(room, 2), needed=round(needed, 2))
    if room >= CLIP_MARGIN + 0.3 and treatment.effect not in MARKER_EFFECTS:
        # Shorten rather than refuse: a two-second card on a two-and-a-half
        # second clip is a shorter card, not no card.
        treatment.end = round(treatment.start + max(0.3, room - CLIP_MARGIN), 3)
        return _lower(
            "clip_too_short",
            f"shortened to {treatment.duration:.1f}s: the clip only has "
            f"{room:.1f}s left after this point",
            room=round(room, 2))
    return _reject(
        "clip_too_short",
        f"the clip has {room:.1f}s left after this point and the effect needs "
        f"{needed:.1f}s",
        room=round(room, 2), needed=round(needed, 2))


def _too_long(treatment, moment, config, **_kwargs):
    if treatment.duration <= config.max_effect_seconds:
        return _pass("too_long", "inside the duration ceiling",
                     duration=round(treatment.duration, 2))
    if treatment.effect in MARKER_EFFECTS:
        return None
    treatment.end = round(treatment.start + config.max_effect_seconds, 3)
    return _lower(
        "too_long",
        f"trimmed to the {config.max_effect_seconds:.1f}s ceiling",
        ceiling=config.max_effect_seconds)


def _interrupts_action(treatment, moment, config, segment_importance="",
                       **_kwargs):
    """Stopping time during the thing the viewer came for.

    A freeze on a death is the moment; a freeze *mid-fight* is an interruption.
    The two are told apart by whether the death already happened, which the
    moment kind answers.
    """
    if treatment.effect not in TIME_EFFECTS:
        return None
    if moment.kind in ("death_or_fail", "funny_reaction", "reveal", "payoff",
                       "grind_montage", "boring_compression", "cliffhanger",
                       "recap", "callback"):
        return _pass("interrupts_action",
                     f"a {moment.kind.replace('_', ' ')} is a moment to hold")
    if moment.kind in ("panic", "near_death", "danger", "villager_chaos") or \
            segment_importance in CRITICAL_ACTION:
        return _reject(
            "interrupts_action",
            f"a {treatment.effect} in the middle of a "
            f"{moment.kind.replace('_', ' ')} stops the thing the viewer came "
            "for",
            moment=moment.kind, importance=segment_importance)
    return _pass("interrupts_action", "nothing critical is happening here")


def _shake_during_combat(treatment, moment, config, segment_importance="",
                         **_kwargs):
    """Shake makes aiming unreadable, which is when it is most tempting."""
    if treatment.effect != "screen_shake":
        return None
    if moment.entities or segment_importance in CRITICAL_ACTION or \
            moment.kind in ("danger", "near_death", "panic"):
        return _reject(
            "shake_during_combat",
            "there is something on screen to aim at, and shaking the frame "
            "makes that unreadable at exactly the moment it matters",
            entities=len(moment.entities), importance=segment_importance)
    return _pass("shake_during_combat", "nothing to aim at here")


def _caption_overlap(treatment, moment, config, captions=(), **_kwargs):
    """Text on top of text.

    The caption pass has already put words on screen at moments it chose. A
    label there is a second thing to read at the same time, and a viewer reads
    neither.
    """
    if treatment.effect not in TEXT_EFFECTS:
        return None
    for start, end, text in captions:
        if treatment.end > start and treatment.start < end:
            return _reject(
                "caption_overlap",
                f'a caption ("{str(text)[:40]}") is on screen at '
                f"{start:.1f}s, and a {treatment.effect} there would be a "
                "second thing to read at once",
                caption_at=round(float(start), 2))
    return _pass("caption_overlap", "no caption is on screen here")


def _repeated_effect(treatment, moment, config, effect_counts=None, **_kwargs):
    """The sixth freeze frame is not emphasis, it is a tic."""
    counts = effect_counts or {}
    used = int(counts.get(treatment.effect, 0))
    if used < config.max_per_effect:
        return _pass("repeated_effect", f"used {used} time(s) so far",
                     used=used, ceiling=config.max_per_effect)
    return _reject(
        "repeated_effect",
        f"{treatment.effect} has already been used {used} time(s), and past "
        f"{config.max_per_effect} the same effect stops reading as emphasis",
        used=used, ceiling=config.max_per_effect)


def _hook_already_polished(treatment, moment, config, hook_polish=0, **_kwargs):
    """An opening that already has a caption and a sting does not need more."""
    if moment.kind != "opening_hook":
        return None
    if hook_polish < HOOK_POLISH_LIMIT:
        return _pass("hook_already_polished",
                     f"the opening carries {hook_polish} other thing(s)",
                     polish=hook_polish)
    return _reject(
        "hook_already_polished",
        f"the opening already carries {hook_polish} other treatment(s) -- a "
        "caption, a sting, a cold open -- and a viewer meets all of them in "
        "the first few seconds",
        polish=hook_polish, ceiling=HOOK_POLISH_LIMIT)


def _too_close(treatment, moment, config, kept=(), **_kwargs):
    """Two effects on top of each other are one too many.

    Markers are exempt from each other: they change nothing, and an editor is
    well served by plenty of them.
    """
    if treatment.effect in MARKER_EFFECTS:
        return None
    for other in kept:
        if other.effect in MARKER_EFFECTS:
            continue
        gap = abs(treatment.start - other.start)
        overlaps = treatment.start < other.end and other.start < treatment.end
        if gap < config.min_spacing or overlaps:
            # A gap of nothing means the same moment already earned an
            # effect. Saying "0.0s from" is true and unhelpful; one moment
            # getting one gesture is the actual rule, and a composite gesture
            # -- a freeze frame *with* a label -- is a single effect in the
            # library rather than two stacked here.
            if gap < 0.5:
                return _reject(
                    "too_close_to_another",
                    f"this moment already earned a {other.effect}, and one "
                    "moment gets one gesture",
                    gap=round(gap, 2), other=other.effect, same_moment=True)
            return _reject(
                "too_close_to_another",
                f"{gap:.1f}s from the {other.effect} at {other.start:.1f}s, "
                f"and this style asks for {config.min_spacing:.0f}s between "
                "two effects",
                gap=round(gap, 2), spacing=config.min_spacing,
                other=other.effect)
    return _pass("too_close_to_another", "clear of everything already kept")
