"""The treatment library: which effects suit which moment, and how strongly.

Thirty-six effects, and a table saying which of them a moment kind can earn.
Kept as data rather than as branches for the same reason the stage table is:
the whole policy is readable in one screen, and a style that forbids an effect
forbids it once, in one place.

## Two things this module deliberately does not do

**It does not decide.** It *offers*. Everything here produces candidates; the
safety pass refuses most of them and the density pass refuses more. A function
in this file returning three candidates for one moment is normal and correct --
the point is that a report can then say "the library offered a zoom punch and a
freeze frame here, and the safety pass took the freeze frame".

**It does not invent content.** A card's text is the objective somebody stated,
the entity the vision pass named, or the caption line that was already
approved. When there is nothing to say, the card becomes a marker asking the
editor to name it -- exactly the rule Session 5's cards follow.

## Style is a filter, not a generator

Each style names the effects it reaches for and the ones it never uses. A style
cannot make this layer produce *more* than the evidence justifies; it can only
narrow what the evidence is allowed to become. That asymmetry is what stops
"styled" turning into "randomly over-edited", and it is the same rule the
Session 5 presets are built on.
"""
from __future__ import annotations

from typing import Optional

from editing.visuals.schema import (
    EFFECT_TYPES, MARKER_EFFECTS, VisualConfig, VisualEffectCandidate,
    VisualMoment, family_of, weaker,
)

#: Moment kind -> the effects that suit it, strongest-fitting first.
#:
#: Read this as "what would a person do here", not as a ranking of the effects
#: themselves. A death earns a freeze frame because a death is the one moment a
#: viewer wants held; a grind earns a montage marker because the answer to a
#: grind is to get through it, not to decorate it.
MOMENT_EFFECTS = {
    "panic": ("quick_punch_in", "impact_flash", "screen_shake"),
    "death_or_fail": ("freeze_frame", "freeze_frame_label", "zoom_punch",
                      "impact_flash", "replay_marker"),
    "danger": ("zoom_punch", "danger_warning_label", "circle_highlight",
               "arrow_callout"),
    "near_death": ("health_emphasis", "quick_punch_in", "totem_reminder",
                   "danger_warning_label"),
    "reveal": ("slow_zoom_hold", "circle_highlight", "label_tag",
               "dramatic_pause"),
    "discovery": ("slow_zoom_hold", "arrow_callout", "entity_callout",
                  "label_tag"),
    "important_find": ("zoom_punch", "circle_highlight", "label_tag",
                       "progression_counter"),
    "payoff": ("slow_zoom_hold", "setup_payoff_card", "impact_flash",
               "label_tag"),
    "callback": ("setup_payoff_card", "label_tag", "instant_replay"),
    "funny_reaction": ("freeze_frame_label", "zoom_punch", "impact_flash",
                       "replay_marker"),
    "banter_spike": ("quick_punch_in", "label_tag"),
    "objective_start": ("objective_card", "objective_label", "chapter_card"),
    "objective_complete": ("progress_card", "objective_label",
                           "progression_counter"),
    "confusing_transition": ("chapter_card", "later_card", "coordinates_card"),
    "grind_montage": ("montage_marker", "speed_ramp", "build_progress_card",
                      "day_counter"),
    "boring_compression": ("montage_marker", "later_card", "speed_ramp"),
    "villager_chaos": ("villager_danger_meter", "box_highlight",
                       "entity_callout"),
    "cliffhanger": ("later_card", "dramatic_pause", "letterbox"),
    "opening_hook": ("title_card", "zoom_punch", "letterbox", "hardcore_warning"),
    "recap": ("recap_card", "instant_replay", "montage_marker"),
}

#: Effect -> (default intensity, default seconds, easing).
#:
#: The durations are the thing most worth arguing with, and they are here in
#: one table so arguing with them is a one-line change rather than a hunt.
EFFECT_DEFAULTS = {
    "zoom_punch": ("high", 0.9, "expo_out"),
    "quick_punch_in": ("medium", 0.7, "quart_out"),
    "slow_zoom_hold": ("subtle", 3.5, "sine_in_out"),
    "crop_pan": ("low", 3.0, "sine_in_out"),
    "freeze_frame": ("medium", 1.0, "linear"),
    "freeze_frame_label": ("high", 1.4, "linear"),
    "replay_marker": ("low", 0.5, "linear"),
    "instant_replay": ("high", 2.5, "linear"),
    "arrow_callout": ("medium", 1.8, "ease_out"),
    "circle_highlight": ("low", 1.8, "ease_out"),
    "box_highlight": ("low", 2.0, "ease_out"),
    "label_tag": ("low", 2.0, "ease_out"),
    "danger_warning_label": ("high", 1.6, "ease_out"),
    "objective_label": ("low", 2.4, "ease_out"),
    "entity_callout": ("medium", 1.8, "ease_out"),
    "title_card": ("high", 3.0, "ease_in_out"),
    "objective_card": ("medium", 2.8, "ease_in_out"),
    "progress_card": ("low", 2.4, "ease_in_out"),
    "recap_card": ("medium", 3.0, "ease_in_out"),
    "chapter_card": ("medium", 2.4, "ease_in_out"),
    "setup_payoff_card": ("medium", 2.6, "ease_in_out"),
    "later_card": ("low", 1.8, "ease_in_out"),
    "screen_shake": ("high", 0.5, "linear"),
    "impact_flash": ("high", 0.25, "expo_out"),
    "speed_ramp": ("medium", 2.5, "sine_in_out"),
    "montage_marker": ("low", 0.5, "linear"),
    "letterbox": ("subtle", 4.0, "sine_in_out"),
    "dramatic_pause": ("low", 0.8, "linear"),
    "hardcore_warning": ("high", 2.0, "ease_out"),
    "totem_reminder": ("medium", 1.6, "ease_out"),
    "health_emphasis": ("high", 1.4, "ease_out"),
    "villager_danger_meter": ("medium", 2.4, "ease_out"),
    "progression_counter": ("low", 2.2, "ease_out"),
    "build_progress_card": ("low", 2.6, "ease_in_out"),
    "day_counter": ("subtle", 2.0, "ease_out"),
    "coordinates_card": ("subtle", 2.2, "ease_out"),
}

#: Zoom scale per intensity, as a percentage. Ceilings, not targets: the style
#: preset's ``max_zoom_scale`` is applied on top and always wins.
ZOOM_SCALE = {"subtle": 104.0, "low": 106.0, "medium": 110.0, "high": 116.0}

#: What each style reaches for, and what it never uses.
#:
#: ``prefers``  effects this style is *for*. They start half a step stronger.
#: ``forbids``  effects this style never emits, whatever the evidence says.
#: ``ceiling``  the strongest intensity this style will ever plan.
STYLE_RULES = {
    "cinematic_minecraft": {
        "prefers": frozenset({
            "slow_zoom_hold", "letterbox", "title_card", "objective_card",
            "chapter_card", "montage_marker", "dramatic_pause",
            "setup_payoff_card", "circle_highlight",
        }),
        "forbids": frozenset({
            "screen_shake", "impact_flash", "freeze_frame_label",
            "zoom_punch", "instant_replay", "villager_danger_meter",
        }),
        "ceiling": "medium",
    },
    "fast_funny": {
        "prefers": frozenset({
            "zoom_punch", "quick_punch_in", "freeze_frame_label",
            "impact_flash", "arrow_callout", "circle_highlight",
            "replay_marker", "instant_replay", "entity_callout",
        }),
        "forbids": frozenset({"letterbox", "recap_card", "coordinates_card"}),
        "ceiling": "high",
    },
    "documentary_story": {
        "prefers": frozenset({
            "objective_card", "recap_card", "chapter_card",
            "setup_payoff_card", "progress_card", "objective_label",
            "coordinates_card", "day_counter", "build_progress_card",
            "slow_zoom_hold",
        }),
        "forbids": frozenset({
            "screen_shake", "impact_flash", "zoom_punch",
            "freeze_frame_label", "instant_replay",
        }),
        "ceiling": "medium",
    },
    "minimal_clean": {
        "prefers": frozenset({
            "title_card", "objective_card", "slow_zoom_hold",
            "chapter_card", "montage_marker",
        }),
        # Everything not preferred is refused for this style. The set below is
        # the explicit half; ``allowed_effects`` does the rest.
        "forbids": frozenset(),
        "ceiling": "low",
        "only_prefers": True,
    },
}

#: What a style not in the table gets. The quiet end, like everywhere else.
DEFAULT_STYLE_RULES = {
    "prefers": frozenset({"title_card", "objective_card", "slow_zoom_hold"}),
    "forbids": frozenset({"screen_shake", "impact_flash"}),
    "ceiling": "medium",
}

#: Per visual layer: the ceilings, and how many effects a moment may earn.
#:
#: ``candidates`` is the interesting number. At ``minimal`` a moment gets one
#: shot at one effect; at ``high`` it can carry three, which is exactly how a
#: single death ends up with a freeze frame, a zoom and a label -- so ``high``
#: exists, is not a default, and says so on every plan it produces.
LAYER_RULES = {
    "off": {"candidates": 0, "effects": 0.0, "callouts": 0.0,
            "ceiling": "subtle"},
    "minimal": {"candidates": 1, "effects": 0.5, "callouts": 0.2,
                "ceiling": "low"},
    "balanced": {"candidates": 2, "effects": 1.5, "callouts": 0.8,
                 "ceiling": "medium"},
    "high": {"candidates": 3, "effects": 3.0, "callouts": 1.6,
             "ceiling": "high"},
}


#: Per-style taste, as configuration rather than as effect names.
#:
#: This lives beside ``STYLE_RULES`` rather than in ``schema.py`` on purpose:
#: the two tables are one policy read from two angles -- which effects a style
#: reaches for, and how much of anything it tolerates -- and splitting them
#: across files is how they drift apart.
STYLE_VISUALS = {
    "cinematic_minecraft": {
        "effects": 0.8, "callouts": 0.3, "spacing": 12.0,
        "freeze": True, "shake": False, "cards": True, "callout_switch": True,
        "replays": False, "meme": False,
    },
    "fast_funny": {
        "effects": 2.5, "callouts": 1.2, "spacing": 4.0,
        "freeze": True, "shake": False, "cards": True, "callout_switch": True,
        # The one style that reads a freeze-frame label and a punch zoom as
        # the point rather than as noise.
        "replays": True, "meme": True,
    },
    "documentary_story": {
        "effects": 0.7, "callouts": 0.5, "spacing": 14.0,
        "freeze": False, "shake": False, "cards": True, "callout_switch": True,
        "replays": False, "meme": False,
    },
    "minimal_clean": {
        "effects": 0.3, "callouts": 0.0, "spacing": 30.0,
        "freeze": False, "shake": False, "cards": True,
        "callout_switch": False, "replays": False, "meme": False,
    },
}

#: What a style not in the table gets. The quiet end.
DEFAULT_STYLE_VISUALS = {
    "effects": 1.0, "callouts": 0.4, "spacing": 10.0,
    "freeze": True, "shake": False, "cards": True, "callout_switch": True,
    "replays": False, "meme": False,
}

#: How the visual layer scales a style's own density. A multiplier rather than
#: a replacement, so a quiet style stays quieter than a loud one at every
#: setting -- ``high`` on ``minimal_clean`` is still quieter than ``balanced``
#: on ``fast_funny``, which is what picking a style is for.
LAYER_SCALE = {"off": 0.0, "minimal": 0.4, "balanced": 1.0, "high": 2.0}


def style_rules(style_name: str) -> dict:
    return STYLE_RULES.get(str(style_name or "").strip(), DEFAULT_STYLE_RULES)


def visual_defaults(style, layer: str = "balanced",
                    mode: str = "plan_only") -> VisualConfig:
    """Visual settings for one style at one layer, before any override.

    The style says what it is for; the layer says how much of it. Both only
    ever narrow: there is no combination of the two that plans more effects
    than the evidence produced candidates for.
    """
    from editing.visuals.schema import coerce_one

    layer = coerce_one(layer, ("off", "minimal", "balanced", "high"), "off")
    name = str(getattr(style, "name", "") or "")
    taste = dict(DEFAULT_STYLE_VISUALS)
    taste.update(STYLE_VISUALS.get(name, {}))

    scale = LAYER_SCALE.get(layer, 1.0)
    ceilings = LAYER_RULES.get(layer, LAYER_RULES["balanced"])

    return VisualConfig(
        layer=layer,
        mode=mode,
        # The style's own number scaled by the layer, and never above what the
        # layer itself permits.
        max_effects_per_minute=min(
            taste["effects"] * scale, float(ceilings["effects"])),
        max_callouts_per_minute=min(
            taste["callouts"] * scale, float(ceilings["callouts"])),
        min_spacing=max(2.0, taste["spacing"] / max(scale, 0.4)),
        allow_freeze_frames=bool(taste["freeze"]),
        allow_screen_shake=bool(taste["shake"]),
        allow_title_cards=bool(taste["cards"]),
        allow_callouts=bool(taste["callout_switch"]),
        allow_replays=bool(taste["replays"]),
        allow_meme_effects=bool(taste["meme"]),
        style=name,
    ).validated()


def allowed_effects(style, config: VisualConfig) -> set:
    """Every effect this style and this configuration together permit.

    The style narrows and the configuration narrows again; neither can widen.
    Computed once per pass so the reason a particular effect is missing is one
    lookup rather than a trace through the planner.
    """
    rules = style_rules(getattr(style, "name", "") or "")
    prefers = set(rules.get("prefers") or ())
    forbids = set(rules.get("forbids") or ())

    if rules.get("only_prefers"):
        out = set(prefers)
    else:
        out = set(EFFECT_TYPES) - forbids

    # A style with no zooms at all must not plan one. The preset already says
    # this in a number, and reading it here is what keeps the two agreeing.
    if not getattr(style, "zooms_allowed", True):
        out -= {"zoom_punch", "quick_punch_in", "slow_zoom_hold", "crop_pan"}
    if not getattr(style, "text_allowed", True):
        out -= {"label_tag", "danger_warning_label", "objective_label",
                "entity_callout", "freeze_frame_label"}

    return {effect for effect in out if config.allows(effect)[0]}


def propose(
    moment: VisualMoment,
    style,
    config: VisualConfig,
    *,
    allowed: Optional[set] = None,
    context: Optional[dict] = None,
) -> list[VisualEffectCandidate]:
    """Candidates for one moment, best-fitting first.

    ``context`` carries whatever the caller knows that the moment does not:
    the episode's stated objective, the chapter number, the previous find.
    Every one of them is optional, and a card whose text is missing becomes a
    marker rather than a card with invented words on it.
    """
    if not config.enabled:
        return []

    allowed = allowed if allowed is not None else allowed_effects(style, config)
    limit = int(LAYER_RULES.get(config.layer, LAYER_RULES["balanced"])
                ["candidates"])
    if limit <= 0:
        return []

    rules = style_rules(getattr(style, "name", "") or "")
    prefers = set(rules.get("prefers") or ())
    ceiling = _lowest(rules.get("ceiling", "high"),
                      LAYER_RULES.get(config.layer, {}).get("ceiling", "high"))

    out: list[VisualEffectCandidate] = []
    for rank, effect in enumerate(MOMENT_EFFECTS.get(moment.kind, ())):
        if effect not in allowed:
            continue
        candidate = _build(moment, effect, rank, ceiling, prefers, style,
                           context or {})
        if candidate is not None:
            out.append(candidate)
        if len(out) >= limit:
            break
    return out


def _lowest(first: str, second: str) -> str:
    from editing.visuals.schema import _INTENSITY_ORDER

    return first if _INTENSITY_ORDER.get(first, 3) <= _INTENSITY_ORDER.get(
        second, 3) else second


def _build(
    moment: VisualMoment,
    effect: str,
    rank: int,
    ceiling: str,
    prefers: set,
    style,
    context: dict,
) -> Optional[VisualEffectCandidate]:
    """One candidate, with its payload filled from what is actually known."""
    from editing.visuals.schema import _INTENSITY_ORDER, treatment_id_for

    intensity, duration, easing = EFFECT_DEFAULTS.get(
        effect, ("medium", 1.5, "ease_out"))

    # A style that reaches for this effect keeps it at full strength; one that
    # merely tolerates it gets it a step down. Then the ceiling, which wins.
    if effect not in prefers:
        intensity = weaker(intensity)
    if _INTENSITY_ORDER.get(intensity, 1) > _INTENSITY_ORDER.get(ceiling, 3):
        intensity = ceiling

    payload = _payload(moment, effect, intensity, style, context)
    if payload is None:
        return None

    reason = _reason(moment, effect)
    priority = max(0.0, min(
        1.0,
        moment.importance * 0.6
        + moment.confidence * 0.3
        + (0.1 if effect in prefers else 0.0)
        - rank * 0.05,
    ))

    return VisualEffectCandidate(
        candidate_id=treatment_id_for(effect, moment.start, moment.moment_id),
        moment_id=moment.moment_id,
        effect=effect,
        intensity=intensity,
        priority=round(priority, 3),
        reason=reason,
        duration=duration,
        offset=0.0,
        payload={**payload, "easing": easing},
    )


def _payload(moment: VisualMoment, effect: str, intensity: str, style,
             context: dict) -> Optional[dict]:
    """Effect-specific detail, or None when there is nothing honest to put in.

    Returning None is the mechanism behind "no card with invented words on
    it": a card with no text to show is not a weaker card, it is not a card.
    """
    family = family_of(effect)

    if effect in ("zoom_punch", "quick_punch_in", "slow_zoom_hold"):
        scale = min(
            ZOOM_SCALE.get(intensity, 106.0),
            float(getattr(style, "max_zoom_scale", 110.0) or 110.0),
        )
        if effect == "slow_zoom_hold":
            scale = min(scale, float(
                getattr(style, "max_push_scale", scale) or scale))
        if scale <= 100.0:
            return None
        return {"scale": round(scale, 1),
                "component": "Motion", "property": "Scale"}

    if effect == "crop_pan":
        return {"scale": 108.0, "component": "Motion",
                "property": "Position", "direction": "right"}

    if effect in ("freeze_frame", "freeze_frame_label"):
        payload = {"hold_at": round(moment.start, 3)}
        if effect == "freeze_frame_label":
            text = _label_text(moment, context)
            if not text:
                return None
            payload["text"] = text
        return payload

    if effect in ("arrow_callout", "circle_highlight", "box_highlight",
                  "entity_callout"):
        target = moment.entities[0] if moment.entities else ""
        # The candidate is still built with an empty target: the *safety* pass
        # is what refuses it, so the refusal lands in the report as
        # ``unknown_target`` rather than the candidate never existing.
        return {"target": target, "shape": _shape_for(effect),
                "position": "unknown"}

    if family == "card":
        text, subtitle, source = _card_text(moment, effect, context)
        if not text:
            return None
        return {"text": text, "subtitle": subtitle, "text_source": source}

    if effect in ("label_tag", "danger_warning_label", "objective_label"):
        text = _label_text(moment, context)
        if not text:
            return None
        return {"text": text}

    if effect in ("hardcore_warning", "totem_reminder", "health_emphasis"):
        return {"text": {
            "hardcore_warning": "HARDCORE",
            "totem_reminder": "TOTEM",
            "health_emphasis": "LOW HEALTH",
        }[effect]}

    if effect == "villager_danger_meter":
        named = ", ".join(moment.entities[:3])
        return {"text": named or "villagers", "entities": moment.entities[:6]}

    if effect in ("progression_counter", "build_progress_card"):
        text = context.get("progress") or moment.label
        if not text:
            return None
        return {"text": str(text)[:120]}

    if effect == "day_counter":
        day = context.get("day")
        if not day:
            return None
        return {"text": f"Day {day}"}

    if effect == "coordinates_card":
        coordinates = str(moment.hud.get("coordinates") or "")
        if not coordinates:
            return None
        return {"text": coordinates[:60]}

    if effect == "speed_ramp":
        return {"rate": 2.0}

    if effect == "letterbox":
        return {"bars": 0.11}

    if effect == "impact_flash":
        return {"color": "#FFFFFF", "opacity": 60.0}

    if effect == "screen_shake":
        return {"amplitude": 0.012}

    if effect in MARKER_EFFECTS or effect == "instant_replay":
        return {"note": moment.label[:160] or moment.kind.replace("_", " ")}

    return {}


def _shape_for(effect: str) -> str:
    return {
        "arrow_callout": "arrow",
        "circle_highlight": "circle",
        "box_highlight": "rounded_rect",
        "entity_callout": "rounded_rect",
    }.get(effect, "rounded_rect")


def _label_text(moment: VisualMoment, context: dict) -> str:
    """Words for a label, taken from something that was actually said or seen.

    Never generated. The order is deliberate: what somebody said beats what a
    model named, which beats nothing at all.
    """
    if moment.source_type == "polish" and moment.label:
        return moment.label[:60]
    if moment.entities:
        return moment.entities[0][:60]
    if moment.kind == "objective_start" and context.get("objective"):
        return str(context["objective"])[:60]
    if moment.kind in ("near_death", "danger") and moment.hud.get("low_health"):
        return "LOW HEALTH"
    return ""


def _card_text(moment: VisualMoment, effect: str,
               context: dict) -> tuple:
    """``(title, subtitle, source)`` for a card, or an empty title.

    ``source`` is ``transcript_quote`` when the words were said and
    ``observed`` when they describe something the vision pass saw. There is no
    third option -- this layer does not write copy the footage cannot support,
    which is the rule Session 8's hook text follows.
    """
    objective = str(context.get("objective") or "")
    chapter = context.get("chapter")

    if effect == "title_card":
        text = str(context.get("title") or objective)
        return (text[:80], "", "transcript_quote" if text else "none")

    if effect == "objective_card":
        if not objective:
            return ("", "", "none")
        return (objective[:80], "the objective", "transcript_quote")

    if effect == "objective_label":
        return ((objective or "")[:60], "", "transcript_quote")

    if effect == "chapter_card":
        label = str(context.get("chapter_label") or moment.label or "")
        if not label:
            return ("", "", "none")
        title = f"{chapter}. {label}"[:80] if chapter else label[:80]
        return (title, "", "observed")

    if effect == "recap_card":
        if not context.get("recap"):
            return ("", "", "none")
        return (str(context["recap"])[:80], "previously", "observed")

    if effect == "setup_payoff_card":
        if not moment.label:
            return ("", "", "none")
        return (moment.label[:80],
                "the payoff" if moment.kind == "payoff" else "the callback",
                "observed")

    if effect == "later_card":
        return ("later...", "", "observed")

    if effect == "progress_card":
        text = str(context.get("progress") or moment.label or "")
        return (text[:80], "", "observed" if text else "none")

    if effect == "build_progress_card":
        text = str(context.get("progress") or "")
        return (text[:80], "", "observed" if text else "none")

    return (moment.label[:80], "", "observed" if moment.label else "none")


def _reason(moment: VisualMoment, effect: str) -> str:
    """Why this effect, at this moment, in one sentence."""
    what = moment.kind.replace("_", " ")
    return {
        "zoom_punch": f"punch onto the {what} so it reads as the moment it is",
        "quick_punch_in": f"a quick push into the {what}",
        "slow_zoom_hold": f"hold and drift into the {what} rather than cutting away",
        "crop_pan": f"drift across the {what}",
        "freeze_frame": f"hold the {what} for a beat: it is the frame a viewer wants",
        "freeze_frame_label": f"hold the {what} and name it",
        "replay_marker": f"mark the {what} as worth replaying",
        "instant_replay": f"replay the {what}",
        "arrow_callout": "point at the thing a viewer would otherwise miss",
        "circle_highlight": "ring the thing the moment is about",
        "box_highlight": "box the thing the moment is about",
        "label_tag": "name what is on screen",
        "danger_warning_label": "say what the threat is, while it is on screen",
        "objective_label": "restate what this is for",
        "entity_callout": "name the thing on screen",
        "title_card": "open the episode on what it is about",
        "objective_card": "state the objective where it is stated",
        "progress_card": "show how far along this is",
        "recap_card": "remind the viewer what happened",
        "chapter_card": "mark the section change",
        "setup_payoff_card": "connect this back to what set it up",
        "later_card": "cover the jump rather than leaving it unexplained",
        "screen_shake": f"shake on the {what}",
        "impact_flash": f"flash on the {what}",
        "speed_ramp": "get through this rather than cutting it out",
        "montage_marker": "mark this stretch as a montage",
        "letterbox": "widen the frame for a beat",
        "dramatic_pause": "hold before the moment lands",
        "hardcore_warning": "the stakes, said once, where they matter",
        "totem_reminder": "remind the viewer what is keeping them alive",
        "health_emphasis": "draw the eye to the health bar while it matters",
        "villager_danger_meter": "show how bad the village situation is",
        "progression_counter": "show the count moving",
        "build_progress_card": "show the build moving",
        "day_counter": "say which day this is",
        "coordinates_card": "say where this is",
    }.get(effect, f"emphasise the {what}")
