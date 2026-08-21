"""Style presets: what "styled" means, as numbers.

A rough cut assembled from recommendations is *correct* and characterless. The
difference between a cut and an edit is not more effects — it is a consistent
set of choices about density, emphasis and restraint, applied everywhere. This
module is that set of choices, written down.

Four presets ship. Each one answers the same questions with different numbers:

``cinematic_minecraft``  let moments breathe; few edits, strong audio marking
``fast_funny``           keep it moving; captions and reactions carry the pace
``documentary_story``    explain clearly; structure, cards, almost no zooms
``minimal_clean``        get out of the way; cuts and markers, nothing else

**Every limit here is a ceiling, not a target.** The compiler never adds an
edit to reach a quota; it only ever *removes* edits that would exceed one. A
style cannot make the system busier than the evidence justifies — it can only
make it quieter. That asymmetry is what stops "styled" turning into "randomly
over-edited", and it is why the density fields are all maxima.

**A preset is a document, not code.** It has no behaviour beyond validation,
so a user can print one, read it, change a number, and know exactly what
changed. `style show <preset>` exists for that.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Optional, Sequence

from editing.schema import _slug, as_str_list, clamp01

#: Where on screen text may be placed. Minecraft puts health, hunger and the
#: hotbar across the bottom centre and the crosshair dead centre, so those are
#: the two places text must not go. ``center`` exists only for title cards,
#: which are *meant* to cover the frame.
TEXT_ZONES = (
    "upper_left", "upper_center", "upper_right",
    "lower_left", "lower_right",
    "center",
)

#: Zone -> ``[x, y]`` as a fraction of the frame, for ``text.create``.
ZONE_POSITION = {
    "upper_left": [0.26, 0.18],
    "upper_center": [0.50, 0.15],
    "upper_right": [0.74, 0.18],
    "lower_left": [0.26, 0.80],
    "lower_right": [0.74, 0.80],
    "center": [0.50, 0.45],
}

#: Never a safe zone for a caption over gameplay, whatever the style says.
#: The crosshair lives here and the hotbar sits just below it.
UNSAFE_FOR_CAPTIONS = frozenset({"center"})

#: How fast the cut feels. Descriptive only -- the numbers below are what act.
PACING = ("slow", "measured", "clean", "fast")

#: The kinds of thing a layer can produce, named once so presets, the compiler
#: and the report all agree.
LAYER_KINDS = (
    # caption layer
    "reaction_caption", "key_phrase", "danger_text", "callout_label",
    # title layer
    "title_card", "chapter_card",
    # emphasis layer
    "punch_in", "slow_push_in", "freeze_frame", "visual_callout",
    "reveal_marker", "danger_marker", "funny_marker",
    # audio layer
    "music_start", "music_rise", "tension_bed", "impact_sfx", "comedic_sfx",
    "whoosh", "silence_hold", "duck_narration", "audio_fade_in",
    "audio_fade_out", "beat_marker", "ambience",
    # marker / polish
    "structure_marker", "pacing_marker", "polish_marker",
)

#: Kinds that change the picture or the sound. These are what the
#: edits-per-minute ceiling counts; markers are annotations and are exempt,
#: because ten notes in a row cost a viewer nothing.
ACTIVE_KINDS = frozenset({
    "reaction_caption", "key_phrase", "danger_text", "title_card",
    "chapter_card", "punch_in", "slow_push_in", "freeze_frame",
    "audio_fade_in", "audio_fade_out",
})

#: A callout label names a thing on screen. No callout graphic has been
#: designed, so it is always a marker -- never drawn, never counted as an edit.
CALLOUT_KINDS = frozenset({"callout_label", "visual_callout"})

#: Kinds that put text on screen at all. Used for reporting.
TEXT_KINDS = frozenset({
    "reaction_caption", "key_phrase", "danger_text", "title_card",
    "chapter_card",
})

#: Kinds counted against the caption ceiling. Cards are deliberately excluded:
#: a chapter card is structural punctuation, not a caption, and it is already
#: limited by its own rules (a real section boundary, a minimum section length,
#: and a minimum gap between cards). Counting it twice meant a reaction caption
#: could starve the chapter marker that gives a documentary its shape -- which
#: is the wrong trade in every style that turns cards on.
CAPTION_KINDS = frozenset({"reaction_caption", "key_phrase", "danger_text"})

#: Cards. Structural, rare, and limited by ``cards.py`` rather than by a rate.
CARD_KINDS = frozenset({"title_card", "chapter_card"})

#: Kinds that scale the picture. Counted against the zoom ceiling.
ZOOM_KINDS = frozenset({"punch_in", "slow_push_in"})

#: Default marker names per kind. A preset overrides only what it wants to
#: rename, so a new kind never silently loses its marker name.
DEFAULT_MARKER_NAMES = {
    "reaction_caption": "CAPTION",
    "key_phrase": "TEXT",
    "danger_text": "WARN",
    "callout_label": "CALLOUT",
    "title_card": "TITLE",
    "chapter_card": "CHAPTER",
    "punch_in": "PUNCH",
    "slow_push_in": "PUSH",
    "freeze_frame": "FREEZE",
    "visual_callout": "CALLOUT",
    "reveal_marker": "REVEAL",
    "danger_marker": "DANGER",
    "funny_marker": "FUNNY",
    "music_start": "MUSIC",
    "music_rise": "RISE",
    "tension_bed": "BED",
    "impact_sfx": "SFX",
    "comedic_sfx": "SFX",
    "whoosh": "WHOOSH",
    "silence_hold": "SILENCE",
    "duck_narration": "DUCK",
    "audio_fade_in": "FADE",
    "audio_fade_out": "FADE",
    "beat_marker": "BEAT",
    "ambience": "AMBI",
    "structure_marker": "STRUCT",
    "pacing_marker": "PACE",
    "polish_marker": "POLISH",
}


@dataclass
class StylePreset:
    """One coherent set of editing choices.

    Treat instances as immutable: use :func:`get` with overrides, or
    ``dataclasses.replace``, rather than mutating a shared preset.
    """

    name: str
    label: str = ""
    description: str = ""

    # -- pacing ----------------------------------------------------------
    #: Descriptive band. Reported, never acted on directly.
    pacing: str = "measured"
    #: Ceiling on picture/sound-changing edits in any 60-second window.
    max_edits_per_minute: float = 3.0
    #: Seconds between two active edits of the same kind.
    min_edit_spacing: float = 6.0
    #: Silence shorter than this is not worth trimming in this style.
    dead_air_tolerance: float = 1.5
    #: 0..1. How willing the style is to cut tight. Reported to the rough cut
    #: rather than acted on here -- this layer does not retime.
    trim_aggression: float = 0.4

    # -- text ------------------------------------------------------------
    max_captions_per_minute: float = 1.5
    min_caption_spacing: float = 8.0
    max_caption_words: int = 7
    #: A transcript line must score at least this to be worth putting on screen.
    caption_min_priority: float = 0.55
    #: Preferred zones, best first. The first zone that is safe wins.
    text_zones: tuple = ("upper_left", "upper_right")
    #: Convert text into real ``text.create`` clips, or leave markers only.
    allow_real_text: bool = True
    #: Seconds a caption stays up, before the per-caption length adjustment.
    caption_duration: float = 2.2

    # -- visual emphasis --------------------------------------------------
    #: Ceiling on any punch-in. 100.0 disables zooms entirely for this style.
    max_zoom_scale: float = 110.0
    max_push_scale: float = 106.0
    max_zooms_per_minute: float = 1.0
    #: Zoom a clip the pacing layer marked "leave this alone"?
    zoom_protected_clips: bool = False
    #: Zoom a clip that has already been retimed?
    zoom_retimed_clips: bool = False
    #: Confidence a zoom needs on a protected clip, when the style allows one.
    zoom_min_confidence: float = 0.75

    # -- categories -------------------------------------------------------
    #: Kinds this style reaches for. Used to break ties when density bites.
    preferred_kinds: frozenset = field(default_factory=frozenset)
    #: kind -> ceiling per minute. Beats the global ceiling when lower.
    limited_kinds: dict = field(default_factory=dict)
    #: Kinds this style never emits at all.
    forbidden_kinds: frozenset = field(default_factory=frozenset)

    # -- markers ----------------------------------------------------------
    #: Prefixed onto every marker name this style writes, so a styled pass is
    #: identifiable and removable on the timeline.
    marker_prefix: str = ""
    #: Overrides on top of ``DEFAULT_MARKER_NAMES``.
    marker_names: dict = field(default_factory=dict)

    # -- audio ------------------------------------------------------------
    #: Which placeholder kinds the audio layer may emit.
    audio_kinds: frozenset = field(default_factory=frozenset)
    #: Convert the two genuinely safe audio ops (fade in/out), or mark only.
    allow_audio_ops: bool = True

    # -- cards -------------------------------------------------------------
    title_cards: bool = False
    chapter_cards: bool = False
    card_duration: float = 2.5
    #: A section must be at least this long to earn a chapter card.
    min_section_seconds: float = 25.0

    # -- safety -------------------------------------------------------------
    #: Evidence priority an item needs before it may become an operation.
    min_confidence: float = 0.5
    #: Two active edits closer than this are considered stacked.
    min_stack_spacing: float = 2.0

    schema_version: int = 1

    # -- derived -----------------------------------------------------------

    @property
    def zooms_allowed(self) -> bool:
        """Whether this style permits scaling the picture at all."""
        return self.max_zoom_scale > 100.0 and self.max_zooms_per_minute > 0

    @property
    def text_allowed(self) -> bool:
        return self.max_captions_per_minute > 0

    def marker_name(self, kind: str) -> str:
        """The marker name this style uses for ``kind``, prefix included."""
        base = self.marker_names.get(kind) or DEFAULT_MARKER_NAMES.get(
            kind, kind.upper()[:16]
        )
        return f"{self.marker_prefix}{base}" if self.marker_prefix else base

    def allows(self, kind: str) -> bool:
        return kind not in self.forbidden_kinds

    def limit_for(self, kind: str) -> Optional[float]:
        """Per-minute ceiling for one kind, or None when only the global caps apply."""
        if kind in self.forbidden_kinds:
            return 0.0
        return self.limited_kinds.get(kind)

    def zone_for(self, kind: str, *, blocked: Sequence[str] = ()) -> Optional[str]:
        """The first preferred zone that is safe here, or None.

        ``blocked`` is whatever the picture rules out at this moment -- a
        full-screen menu, a HUD element the critic flagged. Returning None is a
        real answer: the caller is expected to fall back to a marker rather
        than place text somewhere it will cover the game.
        """
        if kind in ("title_card", "chapter_card"):
            return "center"
        blocked = set(blocked) | set(UNSAFE_FOR_CAPTIONS)
        for zone in self.text_zones:
            if zone not in blocked:
                return zone
        return None

    # -- validation ---------------------------------------------------------

    def problems(self) -> list[str]:
        """Everything wrong with this preset, in plain English.

        Returned rather than raised, and each entry names the field, so
        ``style show`` can report a hand-edited preset's faults all at once
        instead of one per run.
        """
        out: list[str] = []
        if not _slug(self.name):
            out.append("name is empty.")
        if self.pacing not in PACING:
            out.append(
                f"pacing '{self.pacing}' is not one of: {', '.join(PACING)}."
            )
        for field_name, value in (
            ("max_edits_per_minute", self.max_edits_per_minute),
            ("max_captions_per_minute", self.max_captions_per_minute),
            ("max_zooms_per_minute", self.max_zooms_per_minute),
        ):
            if value < 0:
                out.append(f"{field_name} is negative ({value}).")
            elif value > 60:
                out.append(
                    f"{field_name} is {value}, which is more than one per "
                    "second. That is not a style, it is a strobe."
                )
        for field_name, value in (
            ("min_edit_spacing", self.min_edit_spacing),
            ("min_caption_spacing", self.min_caption_spacing),
            ("min_stack_spacing", self.min_stack_spacing),
            ("dead_air_tolerance", self.dead_air_tolerance),
            ("card_duration", self.card_duration),
            ("caption_duration", self.caption_duration),
            ("min_section_seconds", self.min_section_seconds),
        ):
            if value < 0:
                out.append(f"{field_name} is negative ({value}).")
        if self.max_caption_words < 1:
            out.append("max_caption_words must be at least 1.")
        if self.max_caption_words > 14:
            out.append(
                f"max_caption_words is {self.max_caption_words}; past about a "
                "dozen words a caption stops being read and starts being "
                "skipped."
            )
        if self.max_zoom_scale < 100.0:
            out.append(
                f"max_zoom_scale is {self.max_zoom_scale}, below 100%. A zoom "
                "ceiling under 100% would shrink the picture; use exactly "
                "100.0 to disable zooms."
            )
        if self.max_push_scale > self.max_zoom_scale:
            out.append(
                f"max_push_scale ({self.max_push_scale}) is above "
                f"max_zoom_scale ({self.max_zoom_scale}); a gradual push must "
                "never end up stronger than a hard punch."
            )
        if self.max_zoom_scale > 130.0:
            out.append(
                f"max_zoom_scale is {self.max_zoom_scale}; past about 125% a "
                "1080p source visibly softens and the HUD starts leaving the "
                "frame."
            )
        for zone in self.text_zones:
            if zone not in TEXT_ZONES:
                out.append(
                    f"text zone '{zone}' is not one of: {', '.join(TEXT_ZONES)}."
                )
        if self.text_allowed and not [
            zone for zone in self.text_zones if zone not in UNSAFE_FOR_CAPTIONS
        ]:
            out.append(
                "the style allows captions but every preferred zone is unsafe "
                "for them, so no caption could ever be placed."
            )
        for kind in list(self.preferred_kinds) + list(self.forbidden_kinds) + list(
            self.audio_kinds
        ) + list(self.limited_kinds):
            if kind not in LAYER_KINDS:
                out.append(f"unknown layer kind '{kind}'.")
        overlap = set(self.preferred_kinds) & set(self.forbidden_kinds)
        if overlap:
            out.append(
                "these kinds are both preferred and forbidden: "
                + ", ".join(sorted(overlap))
            )
        if self.title_cards and "title_card" in self.forbidden_kinds:
            out.append("title_cards is on but 'title_card' is forbidden.")
        if self.chapter_cards and "chapter_card" in self.forbidden_kinds:
            out.append("chapter_cards is on but 'chapter_card' is forbidden.")
        if not 0.0 <= self.min_confidence <= 1.0:
            out.append(f"min_confidence {self.min_confidence} is outside 0..1.")
        return out

    @property
    def is_valid(self) -> bool:
        return not self.problems()

    def validated(self) -> "StylePreset":
        """Clamp to something the compiler can honour. Never raises.

        A hand-edited preset with one bad number should degrade to a working
        style rather than stop a run -- the same stance ``SamplingConfig`` and
        ``AudioConfig`` take. Read ``problems()`` to see what was wrong before
        the clamp.
        """
        zones = tuple(
            zone for zone in self.text_zones if zone in TEXT_ZONES
        ) or ("upper_left",)
        max_zoom = max(100.0, min(130.0, float(self.max_zoom_scale)))
        return replace(
            self,
            name=_slug(self.name) or "custom",
            pacing=self.pacing if self.pacing in PACING else "measured",
            max_edits_per_minute=max(0.0, min(60.0, float(self.max_edits_per_minute))),
            min_edit_spacing=max(0.0, float(self.min_edit_spacing)),
            dead_air_tolerance=max(0.0, float(self.dead_air_tolerance)),
            trim_aggression=clamp01(self.trim_aggression, 0.4),
            max_captions_per_minute=max(
                0.0, min(60.0, float(self.max_captions_per_minute))
            ),
            min_caption_spacing=max(0.0, float(self.min_caption_spacing)),
            max_caption_words=max(1, min(14, int(self.max_caption_words))),
            caption_min_priority=clamp01(self.caption_min_priority, 0.55),
            caption_duration=max(0.5, float(self.caption_duration)),
            text_zones=zones,
            max_zoom_scale=max_zoom,
            # A gradual push must never out-scale a hard punch.
            max_push_scale=max(100.0, min(max_zoom, float(self.max_push_scale))),
            max_zooms_per_minute=max(0.0, min(60.0, float(self.max_zooms_per_minute))),
            zoom_min_confidence=clamp01(self.zoom_min_confidence, 0.75),
            preferred_kinds=frozenset(
                k for k in self.preferred_kinds if k in LAYER_KINDS
            ),
            forbidden_kinds=frozenset(
                k for k in self.forbidden_kinds if k in LAYER_KINDS
            ),
            limited_kinds={
                k: max(0.0, float(v)) for k, v in self.limited_kinds.items()
                if k in LAYER_KINDS
            },
            audio_kinds=frozenset(k for k in self.audio_kinds if k in LAYER_KINDS),
            card_duration=max(0.5, float(self.card_duration)),
            min_section_seconds=max(0.0, float(self.min_section_seconds)),
            min_confidence=clamp01(self.min_confidence, 0.5),
            min_stack_spacing=max(0.0, float(self.min_stack_spacing)),
        )

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        data = asdict(self)
        data["preferred_kinds"] = sorted(self.preferred_kinds)
        data["forbidden_kinds"] = sorted(self.forbidden_kinds)
        data["audio_kinds"] = sorted(self.audio_kinds)
        data["text_zones"] = list(self.text_zones)
        data["limited_kinds"] = dict(self.limited_kinds)
        data["marker_names"] = dict(self.marker_names)
        data["zooms_allowed"] = self.zooms_allowed
        data["text_allowed"] = self.text_allowed
        data["problems"] = self.problems()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "StylePreset":
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in (data or {}).items() if k in known}
        clean["name"] = str(clean.get("name") or "custom")
        for key in ("preferred_kinds", "forbidden_kinds", "audio_kinds"):
            if key in clean:
                clean[key] = frozenset(as_str_list(clean[key], limit=80))
        if "text_zones" in clean:
            clean["text_zones"] = tuple(as_str_list(clean["text_zones"], limit=10))
        for key in ("limited_kinds", "marker_names"):
            if key in clean:
                clean[key] = dict(clean[key] or {})
        return cls(**clean)

    def summary(self) -> str:
        """One line, for ``style list``."""
        zoom = f"{self.max_zoom_scale:g}%" if self.zooms_allowed else "none"
        return (
            f"{self.name:<22} {self.pacing:<9} "
            f"{self.max_edits_per_minute:>4.1f} edits/min  "
            f"{self.max_captions_per_minute:>4.1f} captions/min  "
            f"zoom {zoom:<5}  {self.label}"
        )


# ---------------------------------------------------------------------------
# The four presets
# ---------------------------------------------------------------------------

CINEMATIC_MINECRAFT = StylePreset(
    name="cinematic_minecraft",
    label="Let it breathe",
    description=(
        "Slow, atmospheric. Few edits, and the ones there are hold rather than "
        "hurry. Text is rare and short because the picture is doing the work. "
        "Music and sound are marked heavily -- in this style the score carries "
        "the tension, so the audio pass gets the most guidance."
    ),
    pacing="slow",
    max_edits_per_minute=1.8,
    min_edit_spacing=10.0,
    # A pause is atmosphere here, not dead air. Only long silences get trimmed.
    dead_air_tolerance=3.0,
    trim_aggression=0.25,
    max_captions_per_minute=0.8,
    min_caption_spacing=20.0,
    max_caption_words=6,
    caption_min_priority=0.72,
    caption_duration=2.6,
    text_zones=("lower_left", "upper_left"),
    allow_real_text=True,
    # Subtle: visible as emphasis, never as a jump.
    max_zoom_scale=108.0,
    max_push_scale=105.0,
    max_zooms_per_minute=0.6,
    zoom_protected_clips=False,
    zoom_retimed_clips=False,
    preferred_kinds=frozenset({
        "slow_push_in", "tension_bed", "music_start", "music_rise",
        "reveal_marker", "danger_marker", "ambience", "silence_hold",
    }),
    limited_kinds={"punch_in": 0.3, "reaction_caption": 0.4},
    forbidden_kinds=frozenset({"comedic_sfx", "whoosh", "freeze_frame"}),
    marker_names={"music_start": "MUSIC IN", "tension_bed": "TENSION"},
    audio_kinds=frozenset({
        "music_start", "music_rise", "tension_bed", "impact_sfx",
        "silence_hold", "ambience", "beat_marker", "audio_fade_in",
        "audio_fade_out", "duck_narration",
    }),
    allow_audio_ops=True,
    title_cards=True,
    chapter_cards=False,
    card_duration=3.0,
    min_section_seconds=45.0,
    min_confidence=0.6,
    min_stack_spacing=4.0,
)

FAST_FUNNY = StylePreset(
    name="fast_funny",
    label="Keep it moving",
    description=(
        "Quick and loud. Captions carry the jokes, reactions get marked, and "
        "dead air is the enemy -- anything over half a second is a candidate "
        "for a trim. The one thing this style will not do is zoom past the "
        "point where the HUD leaves frame, because a punchline you cannot see "
        "is not a punchline."
    ),
    pacing="fast",
    max_edits_per_minute=7.0,
    min_edit_spacing=3.0,
    dead_air_tolerance=0.6,
    trim_aggression=0.85,
    max_captions_per_minute=4.0,
    min_caption_spacing=3.5,
    max_caption_words=5,
    caption_min_priority=0.45,
    caption_duration=1.6,
    text_zones=("upper_center", "upper_left", "upper_right"),
    allow_real_text=True,
    max_zoom_scale=118.0,
    max_push_scale=110.0,
    max_zooms_per_minute=2.5,
    zoom_protected_clips=False,
    zoom_retimed_clips=False,
    preferred_kinds=frozenset({
        "reaction_caption", "punch_in", "comedic_sfx", "whoosh",
        "funny_marker", "impact_sfx", "freeze_frame",
    }),
    limited_kinds={"slow_push_in": 0.5, "tension_bed": 0.2},
    forbidden_kinds=frozenset({"chapter_card"}),
    marker_names={"comedic_sfx": "LOL SFX", "funny_marker": "BIT"},
    audio_kinds=frozenset({
        "music_start", "impact_sfx", "comedic_sfx", "whoosh", "beat_marker",
        "audio_fade_in", "audio_fade_out", "duck_narration",
    }),
    allow_audio_ops=True,
    title_cards=True,
    chapter_cards=False,
    card_duration=1.8,
    min_section_seconds=30.0,
    min_confidence=0.45,
    min_stack_spacing=1.5,
)

DOCUMENTARY_STORY = StylePreset(
    name="documentary_story",
    label="Explain it clearly",
    description=(
        "Structure first. Chapter and title cards mark the sections, text "
        "explains rather than jokes, and the camera stays still -- a "
        "documentary that zooms every thirty seconds reads as nervous. The "
        "structure markers are the point of this style; the effects are not."
    ),
    pacing="clean",
    max_edits_per_minute=2.5,
    min_edit_spacing=8.0,
    dead_air_tolerance=1.2,
    trim_aggression=0.55,
    max_captions_per_minute=2.0,
    min_caption_spacing=9.0,
    # Explanatory text is longer than a reaction by nature.
    max_caption_words=10,
    caption_min_priority=0.55,
    caption_duration=3.2,
    text_zones=("lower_left", "upper_left"),
    allow_real_text=True,
    max_zoom_scale=104.0,
    max_push_scale=104.0,
    max_zooms_per_minute=0.4,
    zoom_protected_clips=False,
    zoom_retimed_clips=False,
    preferred_kinds=frozenset({
        "chapter_card", "title_card", "key_phrase", "structure_marker",
        "slow_push_in", "music_start", "ambience",
    }),
    limited_kinds={"punch_in": 0.2, "reaction_caption": 0.5},
    forbidden_kinds=frozenset({"comedic_sfx", "whoosh", "freeze_frame"}),
    marker_names={"structure_marker": "SECTION"},
    audio_kinds=frozenset({
        "music_start", "music_rise", "ambience", "duck_narration",
        "audio_fade_in", "audio_fade_out", "silence_hold",
    }),
    allow_audio_ops=True,
    title_cards=True,
    chapter_cards=True,
    card_duration=3.5,
    min_section_seconds=25.0,
    min_confidence=0.55,
    min_stack_spacing=3.0,
)

MINIMAL_CLEAN = StylePreset(
    name="minimal_clean",
    label="Get out of the way",
    description=(
        "Cuts and markers. No zooms at all, almost no text, and every "
        "suggestion the other styles would apply is left as a note for a "
        "human instead. This is the style to pick when the footage is good "
        "and the edit should be invisible -- and the one to pick first when "
        "you are not yet sure you trust the system."
    ),
    pacing="clean",
    max_edits_per_minute=0.8,
    min_edit_spacing=20.0,
    dead_air_tolerance=2.0,
    trim_aggression=0.35,
    max_captions_per_minute=0.4,
    min_caption_spacing=30.0,
    max_caption_words=5,
    caption_min_priority=0.8,
    caption_duration=2.0,
    text_zones=("lower_left",),
    allow_real_text=False,          # markers only: nothing is drawn on screen
    # 100.0 disables zooms outright, which is what "no aggressive zooms" means
    # taken seriously.
    max_zoom_scale=100.0,
    max_push_scale=100.0,
    max_zooms_per_minute=0.0,
    zoom_protected_clips=False,
    zoom_retimed_clips=False,
    preferred_kinds=frozenset({
        "structure_marker", "pacing_marker", "music_start", "silence_hold",
    }),
    limited_kinds={},
    forbidden_kinds=frozenset({
        "punch_in", "slow_push_in", "freeze_frame", "comedic_sfx", "whoosh",
        "chapter_card", "danger_text",
    }),
    marker_names={},
    audio_kinds=frozenset({"music_start", "silence_hold", "ambience"}),
    allow_audio_ops=False,
    title_cards=False,
    chapter_cards=False,
    card_duration=2.0,
    min_section_seconds=60.0,
    min_confidence=0.7,
    min_stack_spacing=6.0,
)

#: Every shipped preset, by name. Ordered loosest-to-tightest is deliberate:
#: ``style list`` reads as a spectrum rather than an arbitrary set.
PRESETS = {
    preset.name: preset for preset in (
        CINEMATIC_MINECRAFT, FAST_FUNNY, DOCUMENTARY_STORY, MINIMAL_CLEAN,
    )
}

#: What a run uses when nobody says otherwise. The safest of the four: it
#: cannot put text on screen or scale the picture at all.
DEFAULT_PRESET = "minimal_clean"


def names() -> list[str]:
    return list(PRESETS)


def get(name: Optional[str] = None, **overrides: Any) -> StylePreset:
    """A validated preset by name, with optional field overrides.

    Raises for an unknown name rather than falling back silently: asking for
    ``fast_funnny`` and quietly getting ``minimal_clean`` would produce an edit
    nobody chose.
    """
    from editing.errors import EditingError

    key = _slug(name or DEFAULT_PRESET)
    preset = PRESETS.get(key)
    if preset is None:
        raise EditingError(
            f"Unknown style preset '{name}'",
            hint="Available: " + ", ".join(names())
                 + ". Run `style list` to see them.",
            detail={"available": names()},
        )
    if overrides:
        clean = {
            k: v for k, v in overrides.items()
            if k in StylePreset.__dataclass_fields__ and v is not None
        }
        if clean:
            preset = replace(preset, **clean)
    return preset.validated()
