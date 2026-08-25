"""What a creative visual layer is, as data.

Everything before this decided *what footage is in the edit*. This layer
decides where the edit should point at something — a zoom onto the creeper, a
card naming the objective, an arrow at the thing nobody would otherwise
notice. It is the difference between a cut and a video.

It is also the layer most able to make something unwatchable, so the whole
design is written against one failure mode: **effects everywhere**. A system
that can put an arrow on screen will put an arrow on every frame that has an
entity in it, and the result is the thing viewers call try-hard.

## Four rules

**Every treatment names the moment it is for.** There is no path that produces
an effect from a clock, a beat grid, or "every N seconds". A candidate exists
because the director accepted a decision there, or the retention pass moved
that footage to the front, or the caption pass found a payoff line, or the
audio spiked. A moment with no evidence produces nothing.

**Every refusal is kept.** A rejected treatment stays in the plan with the
named rule that refused it and what that rule measured. "Four effects" and
"forty candidates, thirty-six refused, here is why" are different reports, and
only the second one distinguishes taste from a bug.

**Nothing is drawn, rendered or executed here.** A treatment is a plan. The
Premiere operations are *proposed* and validated offline; the FFmpeg preview is
a capability statement plus a sidecar marker file, and no effect in this system
is ever burned into a video — see :data:`NOT_RENDERED`.

**The HUD is protected before anything else.** Minecraft's health, hunger,
hotbar and coordinates are information the viewer is reading. An effect that
covers or crops them off the frame ruins the exact moment it was selling, so
that check runs before any other and cannot be overridden by a style.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Optional, Sequence

from editing.schema import (
    _slug, as_float, as_str_list, as_text_list, clamp01, short_hash,
)

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

#: How much visual treatment a run may plan.
#:
#: ``off``       nothing. The default.
#: ``minimal``   cards and the occasional slow zoom. Almost nothing else.
#: ``balanced``  the intended setting: emphasis where the episode earns it.
#: ``high``      more of everything, still inside every safety rule. Not a
#:               default, and every plan produced at this level says so.
VISUAL_LAYERS = ("off", "minimal", "balanced", "high")

#: What the composer does with the result.
#:
#: ``off``            compose nothing.
#: ``plan_only``      a ``FinalEditPlan`` and nothing else. The safe default.
#: ``proxy_preview``  also a preview plan and a sidecar marker file beside the
#:                    proxy, so a person watching it can see where the effects
#:                    would land. Still burns nothing in.
#: ``premiere_plan``  also a validated Premiere operation plan, ready to be
#:                    inspected and — separately, explicitly — executed.
#: ``hybrid``         both of the above.
COMPOSER_MODES = ("off", "plan_only", "proxy_preview", "premiere_plan",
                  "hybrid")

#: The moments that can earn visual emphasis. Closed, because a downstream
#: report that pattern-matches free text breaks the day a detector invents a
#: twenty-first kind.
VISUAL_MOMENT_TYPES = (
    "panic",
    "death_or_fail",
    "danger",
    "reveal",
    "discovery",
    "payoff",
    "callback",
    "funny_reaction",
    "banter_spike",
    "objective_start",
    "objective_complete",
    "confusing_transition",
    "grind_montage",
    "boring_compression",
    "important_find",
    "villager_chaos",
    "near_death",
    "cliffhanger",
    "opening_hook",
    "recap",
)

#: Where a moment's evidence came from. Every moment names one, so a report can
#: say which layer is responsible for a treatment being there at all.
SOURCE_TYPES = ("director", "retention", "polish", "transcript", "audio",
                "visual", "episode", "manual")

#: What a treatment can do to the picture. Grouped by family below; the flat
#: tuple is what serialisation and the CLI validate against.
EFFECT_TYPES = (
    # -- basic emphasis ----------------------------------------------------
    "zoom_punch",
    "quick_punch_in",
    "slow_zoom_hold",
    "crop_pan",
    "freeze_frame",
    "freeze_frame_label",
    "replay_marker",
    "instant_replay",
    # -- callouts ----------------------------------------------------------
    "arrow_callout",
    "circle_highlight",
    "box_highlight",
    "label_tag",
    "danger_warning_label",
    "objective_label",
    "entity_callout",
    # -- cards and overlays ------------------------------------------------
    "title_card",
    "objective_card",
    "progress_card",
    "recap_card",
    "chapter_card",
    "setup_payoff_card",
    "later_card",
    # -- motion and impact -------------------------------------------------
    "screen_shake",
    "impact_flash",
    "speed_ramp",
    "montage_marker",
    "letterbox",
    "dramatic_pause",
    # -- Minecraft-specific ------------------------------------------------
    "hardcore_warning",
    "totem_reminder",
    "health_emphasis",
    "villager_danger_meter",
    "progression_counter",
    "build_progress_card",
    "day_counter",
    "coordinates_card",
)

#: Effect -> family. Families are what the density ceilings and the style rules
#: are written in terms of, because "how many callouts a minute" is a question
#: a person has and "how many arrow_callouts a minute" is not.
EFFECT_FAMILY = {
    "zoom_punch": "emphasis",
    "quick_punch_in": "emphasis",
    "slow_zoom_hold": "emphasis",
    "crop_pan": "emphasis",
    "freeze_frame": "emphasis",
    "freeze_frame_label": "emphasis",
    "replay_marker": "replay",
    "instant_replay": "replay",
    "arrow_callout": "callout",
    "circle_highlight": "callout",
    "box_highlight": "callout",
    "label_tag": "callout",
    "danger_warning_label": "callout",
    "objective_label": "callout",
    "entity_callout": "callout",
    "title_card": "card",
    "objective_card": "card",
    "progress_card": "card",
    "recap_card": "card",
    "chapter_card": "card",
    "setup_payoff_card": "card",
    "later_card": "card",
    "screen_shake": "motion",
    "impact_flash": "motion",
    "speed_ramp": "motion",
    "montage_marker": "motion",
    "letterbox": "motion",
    "dramatic_pause": "motion",
    "hardcore_warning": "minecraft",
    "totem_reminder": "minecraft",
    "health_emphasis": "minecraft",
    "villager_danger_meter": "minecraft",
    "progression_counter": "minecraft",
    "build_progress_card": "minecraft",
    "day_counter": "minecraft",
    "coordinates_card": "minecraft",
}

EFFECT_FAMILIES = ("emphasis", "callout", "card", "motion", "replay",
                   "minecraft")

#: Effects that change the *picture* rather than adding something on top of it.
#: These are the ones that can crop the HUD off the frame, so they carry the
#: strictest checks.
PICTURE_EFFECTS = frozenset({
    "zoom_punch", "quick_punch_in", "slow_zoom_hold", "crop_pan",
    "screen_shake", "letterbox",
})

#: Effects that stop the footage. A viewer forgives one of these an episode.
TIME_EFFECTS = frozenset({
    "freeze_frame", "freeze_frame_label", "speed_ramp", "instant_replay",
    "dramatic_pause",
})

#: Effects that put words on screen. Counted against the caption pass's own
#: text so a moment does not end up with a caption and a label on top of it.
TEXT_EFFECTS = frozenset({
    "freeze_frame_label", "label_tag", "danger_warning_label",
    "objective_label", "entity_callout", "title_card", "objective_card",
    "progress_card", "recap_card", "chapter_card", "setup_payoff_card",
    "later_card", "hardcore_warning", "totem_reminder",
    "villager_danger_meter", "progression_counter", "build_progress_card",
    "day_counter", "coordinates_card",
})

#: Effects that need something on screen to point *at*. A callout with an
#: unknown target is the most obviously wrong thing this layer could emit.
NEEDS_TARGET = frozenset({
    "arrow_callout", "circle_highlight", "box_highlight", "entity_callout",
})

#: Effects a viewer reads as a joke. Turned off by every serious style, and by
#: ``--no-meme-effects``.
MEME_EFFECTS = frozenset({
    "freeze_frame_label", "impact_flash", "screen_shake", "zoom_punch",
})

#: Effects that are markers rather than pictures: they change nothing and cost
#: a viewer nothing, so the density ceilings do not count them.
MARKER_EFFECTS = frozenset({
    "replay_marker", "montage_marker", "dramatic_pause",
})

#: How strong a treatment is. Descriptive, and used by the safety pass to lower
#: something rather than refuse it outright.
INTENSITIES = ("subtle", "low", "medium", "high")

_INTENSITY_ORDER = {name: index for index, name in enumerate(INTENSITIES)}

#: Where a treatment can end up. Set by the capability map, never by taste.
TARGET_OUTPUTS = ("ffmpeg_preview", "premiere_plan", "placeholder_only")

#: Why a treatment was refused. Closed, so a report can group thirty of them.
REJECT_REASONS = (
    "too_close_to_another",
    "density_limit",
    "caption_overlap",
    "hides_hud",
    "low_confidence",
    "interrupts_action",
    "shake_during_combat",
    "unknown_target",
    "too_long",
    "repeated_effect",
    "hook_already_polished",
    "clip_too_short",
    "low_transcript_confidence",
    "weak_visual_label",
    "style_forbids",
    "layer_forbids",
    "no_evidence",
    "disabled",
    "unknown",
)

#: Said on every plan and every report this package writes.
NOT_RENDERED = (
    "No effect in this plan has been drawn, rendered or executed. The Premiere "
    "operations are proposals validated offline; the FFmpeg preview is a "
    "capability statement and a sidecar marker file. Nothing here is in any "
    "video."
)

#: Said wherever a report mentions the proxy and the visuals in one breath.
PREVIEW_NOTE = (
    "The proxy render assembles V1 and its original audio. It carries no "
    "visual treatment from this plan, and this layer never claims otherwise: "
    "the sidecar marker file is how to see where the effects would land while "
    "watching it."
)

#: Said on every plan. This layer counts what it planned; it measures nothing.
NOT_MEASURED = (
    "Nothing here has watched a video, measured attention, or established that "
    "any of these treatments improves an edit. Every number below is a count "
    "of what was planned."
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def coerce_one(value: Any, allowed: Sequence[str], default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


def _dicts(value: Any) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def family_of(effect: str) -> str:
    return EFFECT_FAMILY.get(effect, "emphasis")


def stronger(first: str, second: str) -> str:
    """The stronger of two intensities."""
    return first if _INTENSITY_ORDER.get(first, 0) >= _INTENSITY_ORDER.get(
        second, 0) else second


def weaker(intensity: str) -> str:
    """One step down, or ``subtle`` at the bottom.

    The safety pass reaches for this before it reaches for a refusal: a punch
    that is too strong for the moment is usually a *softer* punch rather than
    no punch, and refusing outright throws away a real observation.
    """
    index = _INTENSITY_ORDER.get(intensity, 1)
    return INTENSITIES[max(0, index - 1)]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VisualConfig:
    """Everything that decides how much visual treatment reaches the plan.

    Frozen and serialised whole onto the plan, so a plan always says what
    settings produced it. Every limit is a **ceiling** in the sense the style
    presets mean it: the planner removes candidates to fit inside one and never
    adds an effect to reach a quota.
    """

    layer: str = "off"
    mode: str = "plan_only"

    # -- density -----------------------------------------------------------
    #: Ceiling on picture-changing effects in any 60 seconds of the cut.
    max_effects_per_minute: float = 1.5
    #: Ceiling on callouts specifically. Lower than the general one on purpose:
    #: an arrow is the effect that most quickly reads as try-hard.
    max_callouts_per_minute: float = 0.8
    #: Seconds between two treatments of any kind.
    min_spacing: float = 6.0
    #: Longest a single treatment may run.
    max_effect_seconds: float = 6.0
    #: Hard ceiling on treatments in the whole episode.
    max_total: int = 40
    #: The same effect type may not appear more than this many times.
    max_per_effect: int = 6

    # -- what is allowed at all -------------------------------------------
    allow_freeze_frames: bool = True
    allow_screen_shake: bool = False
    allow_title_cards: bool = True
    allow_callouts: bool = True
    allow_replays: bool = True
    allow_meme_effects: bool = False

    # -- confidence --------------------------------------------------------
    #: A moment below this confidence is recorded and never treated.
    min_confidence: float = 0.5
    #: ASR confidence a transcript-sourced moment needs, when there is one.
    min_transcript_confidence: float = 0.6

    #: The style preset in force, recorded so the plan says where its numbers
    #: came from.
    style: str = ""

    def validated(self) -> "VisualConfig":
        """Clamp to something the planner can honour. Never raises."""
        return replace(
            self,
            layer=coerce_one(self.layer, VISUAL_LAYERS, "off"),
            mode=coerce_one(self.mode, COMPOSER_MODES, "plan_only"),
            max_effects_per_minute=max(
                0.0, min(30.0, as_float(self.max_effects_per_minute, 1.5))),
            max_callouts_per_minute=max(
                0.0, min(30.0, as_float(self.max_callouts_per_minute, 0.8))),
            min_spacing=max(0.0, as_float(self.min_spacing, 6.0)),
            max_effect_seconds=max(
                0.2, min(30.0, as_float(self.max_effect_seconds, 6.0))),
            max_total=max(0, min(500, int(as_float(self.max_total, 40)))),
            max_per_effect=max(1, min(100, int(as_float(self.max_per_effect, 6)))),
            min_confidence=clamp01(self.min_confidence, 0.5),
            min_transcript_confidence=clamp01(
                self.min_transcript_confidence, 0.6),
            style=_slug(self.style),
        )

    @property
    def enabled(self) -> bool:
        return self.layer != "off"

    @property
    def composes(self) -> bool:
        return self.mode != "off"

    @property
    def wants_preview(self) -> bool:
        return self.mode in ("proxy_preview", "hybrid")

    @property
    def wants_premiere(self) -> bool:
        return self.mode in ("premiere_plan", "hybrid")

    def allows(self, effect: str) -> tuple:
        """Whether this configuration permits ``effect``, and why not.

        Returns ``(allowed, reason)``. The switches are checked here rather
        than at the point of use so that every "no" in the system has the same
        shape and lands in the same closed vocabulary.
        """
        family = family_of(effect)
        if not self.enabled:
            return False, "the visual layer is off for this run"
        if effect in ("freeze_frame", "freeze_frame_label") \
                and not self.allow_freeze_frames:
            return False, "freeze frames are off for this run"
        if effect == "screen_shake" and not self.allow_screen_shake:
            return False, "screen shake is off for this run"
        if family == "card" and not self.allow_title_cards:
            return False, "cards are off for this run"
        if family == "callout" and not self.allow_callouts:
            return False, "callouts are off for this run"
        if family == "replay" and not self.allow_replays:
            return False, "replay markers are off for this run"
        if effect in MEME_EFFECTS and not self.allow_meme_effects:
            return False, (
                f"{effect} reads as a joke, and meme effects are off for this "
                "run")
        return True, ""

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.layer == "off":
            out.append(
                "the visual layer is off, so no treatment was planned. "
                "--visual-layer balanced is the intended setting."
            )
        if self.layer == "high":
            out.append(
                "the visual layer is at 'high': more of everything, still "
                "inside every safety rule. This is not a default, and it is "
                "the setting most likely to read as over-edited."
            )
        if self.max_effects_per_minute > 6:
            out.append(
                f"the effect ceiling is {self.max_effects_per_minute:.1f} a "
                "minute, which is one every ten seconds. Past about four a "
                "minute the effects stop marking moments and start being the "
                "edit."
            )
        if self.allow_screen_shake:
            out.append(
                "screen shake is on. It is the effect most likely to be "
                "refused by the safety pass and the most annoying one when it "
                "is not."
            )
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        data["enabled"] = self.enabled
        data["wants_preview"] = self.wants_preview
        data["wants_premiere"] = self.wants_premiere
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "VisualConfig":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


# ---------------------------------------------------------------------------
# One detected moment
# ---------------------------------------------------------------------------

def moment_id_for(kind: str, at: float, source: str) -> str:
    return f"vm_{_slug(kind)[:18]}_{short_hash(round(at, 2), source, length=8)}"


def treatment_id_for(effect: str, at: float, moment_id: str) -> str:
    return f"vt_{_slug(effect)[:18]}_{short_hash(round(at, 2), moment_id, length=8)}"


@dataclass
class VisualMoment:
    """A moment the earlier passes recorded, resolved onto the cut.

    A moment is an *observation*, not a decision. It says "something happened
    here and here is what said so"; whether anything is drawn about it is the
    treatment's question, and the safety pass's after that.
    """

    moment_id: str = ""
    kind: str = "danger"
    #: Which layer's evidence produced it.
    source_type: str = "visual"
    #: The record in that layer: a decision id, a hook id, a caption id.
    source_id: str = ""

    #: Position on the cut. Always sequence time; a moment that could not be
    #: resolved onto the cut is never created.
    start: float = 0.0
    end: float = 0.0
    placement_id: str = ""
    segment_ids: list[str] = field(default_factory=list)
    asset_id: str = ""

    #: 0..1. How sure the evidence is, not how much it matters.
    confidence: float = 0.5
    #: 0..1. How much this matters relative to the episode's other moments.
    importance: float = 0.5
    #: What the moment is about, in plain English.
    label: str = ""
    #: Quotes, event ids, decision ids -- whatever a person would want to check.
    evidence: list[str] = field(default_factory=list)

    #: Named things on screen at that moment, when the vision pass saw any.
    #: This is what a callout can point at, and its emptiness is what makes
    #: ``unknown_target`` a refusal rather than a guess.
    entities: list[str] = field(default_factory=list)
    #: HUD state at the moment, from the vision pass. Drives the HUD checks.
    hud: dict = field(default_factory=dict)
    #: ASR confidence, when a transcript line is part of the evidence.
    transcript_confidence: float = -1.0
    schema_version: int = 1

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def has_target(self) -> bool:
        return bool(self.entities)

    def line(self) -> str:
        return (f"  [{self.start:7.2f}-{self.end:7.2f}] {self.kind:<20} "
                f"c={self.confidence:.2f} i={self.importance:.2f} "
                f"{self.source_type:<9} {self.label[:50]}")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        data["duration"] = round(self.duration, 3)
        data["has_target"] = self.has_target
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "VisualMoment":
        data = data or {}
        start = as_float(data.get("start"))
        return cls(
            moment_id=_text(data.get("moment_id"), 80),
            kind=coerce_one(data.get("kind"), VISUAL_MOMENT_TYPES, "danger"),
            source_type=coerce_one(
                data.get("source_type"), SOURCE_TYPES, "visual"),
            source_id=_text(data.get("source_id"), 120),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            placement_id=_text(data.get("placement_id"), 120),
            segment_ids=as_str_list(data.get("segment_ids"), limit=40),
            asset_id=_text(data.get("asset_id"), 120),
            confidence=clamp01(data.get("confidence"), 0.5),
            importance=clamp01(data.get("importance"), 0.5),
            label=_text(data.get("label"), 300),
            evidence=as_text_list(data.get("evidence"), limit=20),
            entities=as_str_list(data.get("entities"), limit=20),
            hud=dict(data.get("hud") or {}),
            transcript_confidence=as_float(
                data.get("transcript_confidence"), -1.0),
        )


# ---------------------------------------------------------------------------
# One candidate, and one safety check
# ---------------------------------------------------------------------------

@dataclass
class VisualEffectCandidate:
    """An effect proposed for a moment, before any rule has looked at it.

    Separate from :class:`VisualTreatment` on purpose. A candidate is what the
    treatment library *offered*; a treatment is what survived. Keeping the two
    apart is what lets a report say "the library offered a zoom punch and a
    freeze frame here, and the safety pass took the freeze frame".
    """

    candidate_id: str = ""
    moment_id: str = ""
    effect: str = "zoom_punch"
    intensity: str = "medium"
    #: 0..1. Ranks candidates against each other when a ceiling bites.
    priority: float = 0.5
    #: Why this effect suits this moment, in plain English.
    reason: str = ""
    #: Suggested duration, before the style and the clip trim it.
    duration: float = 1.0
    #: Where in the moment it starts, as an offset in seconds.
    offset: float = 0.0
    #: Kind-specific detail: card text, callout target, zoom scale.
    payload: dict = field(default_factory=dict)

    @property
    def family(self) -> str:
        return family_of(self.effect)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["family"] = self.family
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "VisualEffectCandidate":
        data = data or {}
        return cls(
            candidate_id=_text(data.get("candidate_id"), 80),
            moment_id=_text(data.get("moment_id"), 80),
            effect=coerce_one(data.get("effect"), EFFECT_TYPES, "zoom_punch"),
            intensity=coerce_one(
                data.get("intensity"), INTENSITIES, "medium"),
            priority=clamp01(data.get("priority"), 0.5),
            reason=_text(data.get("reason"), 400),
            duration=max(0.0, as_float(data.get("duration"), 1.0)),
            offset=as_float(data.get("offset")),
            payload=dict(data.get("payload") or {}),
        )


@dataclass
class VisualSafetyCheck:
    """One rule, applied to one treatment, with what it saw.

    Every check that runs is recorded, including the ones that passed. A plan
    where the HUD check never ran and a plan where it ran and passed look
    identical from the outside otherwise, and only one of them is safe.
    """

    name: str = ""
    #: ``pass`` / ``lowered`` / ``reject``
    outcome: str = "pass"
    reason: str = ""
    #: What the rule measured. Numbers, not adjectives.
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "VisualSafetyCheck":
        data = data or {}
        return cls(
            name=_text(data.get("name"), 60),
            outcome=coerce_one(
                data.get("outcome"), ("pass", "lowered", "reject"), "pass"),
            reason=_text(data.get("reason"), 400),
            evidence=dict(data.get("evidence") or {}),
        )


# ---------------------------------------------------------------------------
# One treatment
# ---------------------------------------------------------------------------

@dataclass
class VisualTreatment:
    """One effect, at one moment, and everything decided about it.

    Accepted and rejected treatments are the same record. A refused effect
    keeps its reason, its evidence and every check that looked at it, because
    that is the only way to tell restraint from a bug.
    """

    treatment_id: str = ""
    moment_id: str = ""
    #: The record in the layer that produced the moment.
    source_id: str = ""
    source_type: str = "visual"
    moment_kind: str = "danger"

    effect: str = "zoom_punch"
    intensity: str = "medium"
    #: Position on the cut, in sequence time.
    start: float = 0.0
    end: float = 0.0
    placement_id: str = ""
    #: How the effect moves, when it moves. A name from the Premiere catalog's
    #: easing list, so a plan maps onto an operation without translation.
    easing: str = "ease_out"

    priority: float = 0.5
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    safety_notes: list[str] = field(default_factory=list)
    checks: list[VisualSafetyCheck] = field(default_factory=list)

    accepted: bool = False
    reject_reason: str = ""
    reject_detail: str = ""
    #: True when the safety pass softened this rather than refusing it.
    lowered: bool = False

    #: Where this can actually end up, from the capability map.
    target_output: str = "placeholder_only"
    #: Kind-specific detail: card text, callout target, zoom scale, position.
    payload: dict = field(default_factory=dict)
    schema_version: int = 1

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def family(self) -> str:
        return family_of(self.effect)

    @property
    def changes_the_picture(self) -> bool:
        return self.effect in PICTURE_EFFECTS

    @property
    def counts_against_density(self) -> bool:
        """Whether this costs the viewer anything.

        Markers do not: they change no frame and an editor is well served by
        plenty of them. Charging them against the ceiling would make a
        restrained plan look busy and starve the next real effect -- the same
        asymmetry the style layer draws between an edit and an annotation.
        """
        return self.effect not in MARKER_EFFECTS

    def line(self) -> str:
        mark = "+" if self.accepted else "-"
        tail = self.reason if self.accepted else (
            f"{self.reject_reason}: {self.reject_detail or self.reason}")
        return (f"{mark} {self.start:7.2f}  {self.effect:<22} "
                f"{self.intensity:<7} {tail[:64]}")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        data["duration"] = round(self.duration, 3)
        data["family"] = self.family
        data["changes_the_picture"] = self.changes_the_picture
        data["checks"] = [check.to_dict() for check in self.checks]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "VisualTreatment":
        data = data or {}
        start = as_float(data.get("start"))
        return cls(
            treatment_id=_text(data.get("treatment_id"), 80),
            moment_id=_text(data.get("moment_id"), 80),
            source_id=_text(data.get("source_id"), 120),
            source_type=coerce_one(
                data.get("source_type"), SOURCE_TYPES, "visual"),
            moment_kind=coerce_one(
                data.get("moment_kind"), VISUAL_MOMENT_TYPES, "danger"),
            effect=coerce_one(data.get("effect"), EFFECT_TYPES, "zoom_punch"),
            intensity=coerce_one(data.get("intensity"), INTENSITIES, "medium"),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            placement_id=_text(data.get("placement_id"), 120),
            easing=_text(data.get("easing"), 40) or "ease_out",
            priority=clamp01(data.get("priority"), 0.5),
            reason=_text(data.get("reason"), 400),
            evidence=as_text_list(data.get("evidence"), limit=20),
            safety_notes=as_text_list(data.get("safety_notes"), limit=20),
            checks=[VisualSafetyCheck.from_dict(item)
                    for item in _dicts(data.get("checks"))],
            accepted=bool(data.get("accepted")),
            reject_reason=coerce_one(
                data.get("reject_reason"), REJECT_REASONS, ""),
            reject_detail=_text(data.get("reject_detail"), 400),
            lowered=bool(data.get("lowered")),
            target_output=coerce_one(
                data.get("target_output"), TARGET_OUTPUTS, "placeholder_only"),
            payload=dict(data.get("payload") or {}),
        )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass
class VisualLayerPlan:
    """Every moment found and every treatment considered, in one object."""

    name: str = "structure"
    layer: str = "off"
    config: VisualConfig = field(default_factory=VisualConfig)
    style: str = ""
    sequence_name: str = ""
    cut_duration: float = 0.0
    #: Which cut this was planned against: ``retention`` or ``roughcut``.
    base: str = "roughcut"

    moments: list[VisualMoment] = field(default_factory=list)
    treatments: list[VisualTreatment] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)
    safety_notes: list[str] = field(default_factory=list)
    generated_at: str = ""
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.treatments)

    @property
    def accepted(self) -> list[VisualTreatment]:
        return [t for t in self.treatments if t.accepted]

    @property
    def rejected(self) -> list[VisualTreatment]:
        return [t for t in self.treatments if not t.accepted]

    @property
    def lowered(self) -> list[VisualTreatment]:
        return [t for t in self.accepted if t.lowered]

    def moment(self, moment_id: str) -> Optional[VisualMoment]:
        for entry in self.moments:
            if entry.moment_id == moment_id:
                return entry
        return None

    def treated_moment_ids(self) -> set:
        return {t.moment_id for t in self.accepted}

    def untreated_moments(self) -> list[VisualMoment]:
        """Moments that earned nothing. The other half of the report."""
        treated = self.treated_moment_ids()
        return [m for m in self.moments if m.moment_id not in treated]

    @property
    def effects_per_minute(self) -> float:
        if self.cut_duration <= 0:
            return 0.0
        count = sum(1 for t in self.accepted if t.counts_against_density)
        return round(count / (self.cut_duration / 60.0), 3)

    @property
    def callouts_per_minute(self) -> float:
        if self.cut_duration <= 0:
            return 0.0
        count = sum(1 for t in self.accepted if t.family == "callout")
        return round(count / (self.cut_duration / 60.0), 3)

    def by_family(self) -> dict:
        out: dict = {}
        for treatment in self.accepted:
            out[treatment.family] = out.get(treatment.family, 0) + 1
        return out

    def by_effect(self) -> dict:
        out: dict = {}
        for treatment in self.accepted:
            out[treatment.effect] = out.get(treatment.effect, 0) + 1
        return out

    def by_moment_kind(self) -> dict:
        out: dict = {}
        for treatment in self.accepted:
            out[treatment.moment_kind] = out.get(treatment.moment_kind, 0) + 1
        return out

    def by_source(self) -> dict:
        out: dict = {}
        for moment in self.moments:
            out[moment.source_type] = out.get(moment.source_type, 0) + 1
        return out

    def by_reject_reason(self) -> dict:
        out: dict = {}
        for treatment in self.rejected:
            key = treatment.reject_reason or "unknown"
            out[key] = out.get(key, 0) + 1
        return out

    def by_target(self) -> dict:
        out: dict = {}
        for treatment in self.accepted:
            out[treatment.target_output] = out.get(
                treatment.target_output, 0) + 1
        return out

    def stats(self) -> dict:
        return {
            "moments": len(self.moments),
            "considered": len(self.treatments),
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "lowered": len(self.lowered),
            "untreated_moments": len(self.untreated_moments()),
            "effects_per_minute": self.effects_per_minute,
            "callouts_per_minute": self.callouts_per_minute,
            "picture_changing": sum(
                1 for t in self.accepted if t.changes_the_picture),
            "placeholder_only": sum(
                1 for t in self.accepted
                if t.target_output == "placeholder_only"),
            "by_family": self.by_family(),
            "by_effect": self.by_effect(),
            "by_moment_kind": self.by_moment_kind(),
            "by_source": self.by_source(),
            "by_reject_reason": self.by_reject_reason(),
            "by_target": self.by_target(),
            "cut_duration": round(self.cut_duration, 2),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "layer": self.layer,
            "style": self.style,
            "base": self.base,
            "sequence_name": self.sequence_name,
            "generated_at": self.generated_at,
            "config": self.config.to_dict(),
            "stats": self.stats(),
            "not_rendered": NOT_RENDERED,
            "not_measured": NOT_MEASURED,
            "warnings": list(self.warnings),
            "safety_notes": list(self.safety_notes),
            "moments": [m.to_dict() for m in self.moments],
            "accepted": [t.to_dict() for t in self.accepted],
            "rejected": [t.to_dict() for t in self.rejected],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VisualLayerPlan":
        data = data or {}
        raw = _dicts(data.get("accepted")) + _dicts(data.get("rejected"))
        treatments = [VisualTreatment.from_dict(item) for item in raw]
        # A round trip must not silently promote a rejection.
        for treatment, source in zip(treatments, raw):
            treatment.accepted = bool(source.get("accepted"))
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            layer=coerce_one(data.get("layer"), VISUAL_LAYERS, "off"),
            config=VisualConfig.from_dict(data.get("config")),
            style=_text(data.get("style"), 80),
            base=_text(data.get("base"), 40) or "roughcut",
            sequence_name=_text(data.get("sequence_name"), 200),
            cut_duration=as_float((data.get("stats") or {}).get("cut_duration")),
            moments=[VisualMoment.from_dict(item)
                     for item in _dicts(data.get("moments"))],
            treatments=sorted(
                treatments, key=lambda t: (t.start, t.treatment_id)),
            warnings=as_text_list(data.get("warnings"), limit=60),
            safety_notes=as_text_list(data.get("safety_notes"), limit=60),
            generated_at=_text(data.get("generated_at"), 40),
        )
