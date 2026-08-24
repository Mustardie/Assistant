"""What a polish pass is, as data.

The core system decides *what footage is in the edit*. This layer decides the
much smaller question of what to put **on top of it**, and its whole design is
about not doing very much.

Two failure modes it is written against:

* **Full subtitles.** A system that can put a transcript on screen will put the
  whole transcript on screen. So a line does not get a caption for being
  audible -- it has to be a *key moment*, one of the nine kinds in
  :data:`KEY_MOMENTS`, and even then it competes for a per-minute budget it
  will usually lose.
* **Sound-effect spam.** A system that can place a whoosh at a cut will place a
  whoosh at every cut. So a cue has to name the moment it is for, it may not
  land on a spoken word, and the per-minute ceiling is small enough that most
  candidates are refused.

Three rules hold for both halves:

**Every refusal is kept.** A rejected caption and a rejected cue stay in the
plan with the named rule that refused them. "Nothing was added" and "forty
things were considered and all forty were refused, here is why" are very
different reports, and only the second one is useful.

**A missing asset is reported, never invented.** In ``placeholders`` mode
nothing is ever played; in ``assets`` mode an unmatched cue stays a placeholder
and lands on the shopping list.

**No claim is ever made about the result.** This layer may say that four
captions were placed. It may never say the video is better for them -- see
:data:`NOT_MEASURED`.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Optional, Sequence

from editing.schema import (
    _slug, as_float, as_text_list, clamp01, short_hash,
)

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

#: How much text this pass may put on screen.
#:
#: ``off``          nothing. The default.
#: ``key_moments``  only the nine kinds below, inside a small budget.
#: ``dense``        every line the style's own caption rules would allow. This
#:                  is close to subtitles and is never a default; the plan
#:                  carries a warning saying so.
CAPTION_MODES = ("off", "key_moments", "dense")

#: What a caption may be *for*. A line that is none of these is not captioned
#: in ``key_moments`` mode, however clearly it was said.
KEY_MOMENTS = (
    "funny_reaction",     # laughter, or a reaction over something absurd
    "death_or_fail",      # a death screen, or the line that admits one
    "objective",          # what the episode is trying to do, stated
    "reveal",             # the moment something is found or shown
    "payoff_line",        # the line that lands the thing that was set up
    "callback",           # a line that refers back to an earlier one
    "danger",             # a named threat, at the moment it matters
    "meme_quote",         # short, quotable, and said with force
    "transition_setup",   # "right, now we..." -- the hinge between sections
)

#: Moment kinds that carry the episode's structure. These win ties against the
#: purely reactive ones when the budget bites, because a viewer who loses the
#: objective line loses the plot and a viewer who loses one "oh my god" does
#: not.
STRUCTURAL_MOMENTS = frozenset({
    "objective", "reveal", "payoff_line", "callback", "transition_setup",
})

#: Why a caption was refused. Closed, so a report can group thirty of them.
CAPTION_REJECT_REASONS = (
    "not_a_key_moment",
    "boring_explanation",
    "too_long",
    "too_many_words",
    "unclear_transcript",
    "low_confidence",
    "background_speech",
    "repeated_filler",
    "duplicate_line",
    "cut_from_the_edit",
    "blocked_by_ui",
    "style_forbids_text",
    "no_safe_zone",
    "density_limit",
    "too_close_to_another",
    "disabled",
    "unknown",
)

#: What the audio polish may place.
#:
#: ``off``           nothing.
#: ``placeholders``  every cue is a note naming the sound that should go there.
#:                   Nothing is ever played, and no library is read.
#: ``assets``        cues are matched against the local library. Anything that
#:                   does not match stays a placeholder and is reported missing.
AUDIO_POLISH_MODES = ("off", "placeholders", "assets")

#: The five things this pass will place, and the one absence it will ask for.
AUDIO_CUE_KINDS = (
    "riser",         # a build into a big moment
    "hit",           # a sting on a fail or a reveal
    "whoosh",        # a transition between two sections
    "ambience",      # atmosphere under a stretch, when the style allows it
    "music_bed",     # a bed under a stretch, when the style allows it
    "silence_drop",  # take everything out for a beat, before the moment lands
)

#: Cue kind -> the placeholder kind the Session 6 matcher already knows how to
#: satisfy. ``silence_drop`` is deliberately absent: the absence of sound is
#: the point, and there is no file that could be it.
CUE_REQUIREMENT = {
    "riser": "music_rise",
    "hit": "impact_sfx",
    "whoosh": "whoosh",
    "ambience": "ambience",
    "music_bed": "tension_bed",
}

#: Cue kinds that count against the sound-effect ceiling. A bed and a silence
#: are not effects: one runs underneath for a minute and the other is nothing
#: at all, so counting them would starve the ceiling that matters.
SFX_KINDS = frozenset({"riser", "hit", "whoosh"})

#: Why an audio cue was refused.
AUDIO_REJECT_REASONS = (
    "no_moment",
    "would_cover_speech",
    "too_close_to_another",
    "density_limit",
    "style_forbids",
    "bed_not_allowed",
    "no_asset",
    "clip_too_short",
    "duplicate_cue",
    "disabled",
    "unknown",
)

#: Filler that is never worth reading, whatever else is happening. Matched on
#: the whole cleaned line, not on substrings, so "okay" is filler and "okay
#: that is a warden" is not.
FILLER_LINES = frozenset({
    "um", "uh", "er", "hmm", "mm", "mhm", "yeah", "yep", "yes", "no", "nope",
    "okay", "ok", "right", "so", "and", "but", "well", "like", "anyway",
    "alright", "sure", "wait", "huh", "what", "oh", "ah", "hey", "here we go",
    "okay so", "so yeah", "i mean", "you know", "let me see", "there we go",
})

#: Markers an ASR leaves when it could not hear. A line carrying one of these
#: is not captioned, because the caption would be wrong in a way a viewer can
#: see against the audio.
UNCLEAR_MARKERS = ("[inaudible]", "(inaudible)", "[unintelligible]",
                   "[indistinct]", "[?]", "???", "[silence]", "[noise]")

#: Said on every plan and every report this package writes.
NOT_MEASURED = (
    "Captions and cues here are chosen from what the earlier passes recorded: "
    "where a reaction was heard, where a death screen was seen, where a line "
    "names a threat. Nothing in this layer has watched the video, measured "
    "attention, or established that any of it improves an edit. Every number "
    "below is a count of what was planned."
)

#: Said on every audio plan whose mode is ``placeholders``.
PLACEHOLDER_ONLY = (
    "Nothing here plays. Every cue is a note naming the sound that belongs at "
    "that moment, for a person or a later pass to satisfy."
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptionConfig:
    """Everything that decides how much text reaches the screen.

    Frozen and serialised whole onto the plan, so a caption plan always says
    what settings produced it.

    Every limit is a **ceiling**, in the same sense the style presets mean it:
    the selector removes candidates to fit inside one and never adds a caption
    to reach a quota.
    """

    mode: str = "off"
    #: Ceiling on captions in any 60 seconds of the cut.
    max_per_minute: float = 1.2
    #: Seconds between two captions.
    min_spacing: float = 8.0
    #: Longest a single caption may stay up.
    max_seconds: float = 3.5
    #: Words a caption may carry. Longer lines are condensed to their strongest
    #: phrase; lines that cannot be condensed that far are refused.
    max_words: int = 7
    #: A line must score at least this to be a candidate at all.
    min_priority: float = 0.55
    #: ASR confidence a line needs, when the transcript carries one.
    min_confidence: float = 0.6
    #: Refuse a line whose transcript has no confidence figure at all. Off:
    #: a hand-written SRT has no confidence and is usually more trustworthy
    #: than a machine one.
    require_confidence: bool = False
    #: Hard ceiling on captions in the whole episode, whatever the rate allows.
    max_total: int = 40
    #: The style preset in force, recorded so the plan says where its numbers
    #: came from.
    style: str = ""

    def validated(self) -> "CaptionConfig":
        """Clamp to something the selector can honour. Never raises."""
        return replace(
            self,
            mode=coerce_one(self.mode, CAPTION_MODES, "off"),
            max_per_minute=max(0.0, min(30.0, as_float(self.max_per_minute, 1.2))),
            min_spacing=max(0.0, as_float(self.min_spacing, 8.0)),
            max_seconds=max(0.5, min(12.0, as_float(self.max_seconds, 3.5))),
            max_words=max(1, min(14, int(as_float(self.max_words, 7)))),
            min_priority=clamp01(self.min_priority, 0.55),
            min_confidence=clamp01(self.min_confidence, 0.6),
            max_total=max(0, min(500, int(as_float(self.max_total, 40)))),
            style=_slug(self.style),
        )

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def key_moments_only(self) -> bool:
        return self.mode == "key_moments"

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.mode == "off":
            out.append(
                "captions are off, so nothing was put on screen. "
                "--captions key_moments adds a few, for the moments that "
                "carry the episode."
            )
        if self.mode == "dense":
            out.append(
                "captions are in 'dense' mode: every line the style would "
                "allow becomes a candidate, which is close to subtitles. "
                "This is not a default and is rarely what an edit wants."
            )
        if self.max_per_minute > 6:
            out.append(
                f"the caption ceiling is {self.max_per_minute:.1f} a minute, "
                "which is one every ten seconds. Past about four a minute a "
                "viewer is reading rather than watching."
            )
        if self.max_seconds > 6:
            out.append(
                f"captions may stay up for {self.max_seconds:.1f}s. Past "
                "about four seconds a short caption reads as stuck."
            )
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        data["enabled"] = self.enabled
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "CaptionConfig":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass(frozen=True)
class AudioPolishConfig:
    """Everything that decides how much sound this pass adds."""

    mode: str = "off"
    #: Ceiling on risers, hits and whooshes in any 60 seconds.
    max_sfx_per_minute: float = 1.0
    #: Seconds between two cues of any kind.
    min_spacing: float = 6.0
    #: A cue may not start within this many seconds of a spoken word.
    speech_guard: float = 0.35
    #: Whether a music or ambience bed may be laid at all.
    music_bed: bool = False
    #: Whether the plan asks for the bed to duck under speech.
    ducking: bool = True
    #: Hard ceiling on cues in the whole episode.
    max_total: int = 30
    #: Longest a bed may run before it is cut into a second one.
    max_bed_seconds: float = 180.0
    #: A cue below this priority is recorded and never placed.
    min_priority: float = 0.5
    style: str = ""

    def validated(self) -> "AudioPolishConfig":
        return replace(
            self,
            mode=coerce_one(self.mode, AUDIO_POLISH_MODES, "off"),
            max_sfx_per_minute=max(
                0.0, min(30.0, as_float(self.max_sfx_per_minute, 1.0))),
            min_spacing=max(0.0, as_float(self.min_spacing, 6.0)),
            speech_guard=max(0.0, min(5.0, as_float(self.speech_guard, 0.35))),
            max_total=max(0, min(500, int(as_float(self.max_total, 30)))),
            max_bed_seconds=max(5.0, as_float(self.max_bed_seconds, 180.0)),
            min_priority=clamp01(self.min_priority, 0.5),
            style=_slug(self.style),
        )

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def uses_library(self) -> bool:
        return self.mode == "assets"

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.mode == "off":
            out.append(
                "audio polish is off, so no sound was planned. "
                "--audio-polish placeholders marks where sound belongs "
                "without needing a library."
            )
        if self.mode == "placeholders":
            out.append(PLACEHOLDER_ONLY)
        if self.max_sfx_per_minute > 4:
            out.append(
                f"the effect ceiling is {self.max_sfx_per_minute:.1f} a "
                "minute. Past about three a minute the effects stop marking "
                "moments and start being the edit."
            )
        if not self.ducking and self.music_bed:
            out.append(
                "a bed is allowed and ducking is off, so the plan asks for "
                "music under speech at a fixed level. Check it against the "
                "commentary by ear."
            )
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        data["enabled"] = self.enabled
        data["uses_library"] = self.uses_library
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AudioPolishConfig":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


# ---------------------------------------------------------------------------
# Style defaults
# ---------------------------------------------------------------------------

#: Per-style audio taste. Captions take their defaults from the style preset
#: itself, which already carries caption numbers; sound has no equivalent
#: field, so it lives here.
STYLE_AUDIO = {
    "cinematic_minecraft": {"max_sfx_per_minute": 0.8, "music_bed": True,
                            "min_spacing": 10.0},
    "fast_funny": {"max_sfx_per_minute": 2.0, "music_bed": True,
                   "min_spacing": 4.0},
    "documentary_story": {"max_sfx_per_minute": 0.6, "music_bed": True,
                          "min_spacing": 12.0},
    "minimal_clean": {"max_sfx_per_minute": 0.25, "music_bed": False,
                      "min_spacing": 30.0},
}

#: What a style that is not in the table gets. Deliberately the quiet end.
DEFAULT_AUDIO = {"max_sfx_per_minute": 1.0, "music_bed": False,
                 "min_spacing": 8.0}

#: Ceiling on the key-moment caption rate whatever the style allows. A style
#: tuned for four captions a minute is describing subtitles-with-taste; this
#: pass is describing punctuation, and the two are different features.
KEY_MOMENT_CEILING = 1.5

#: Floor on the spacing between two key-moment captions.
KEY_MOMENT_SPACING = 6.0


def caption_defaults(style, mode: str = "key_moments") -> CaptionConfig:
    """Caption settings for one style, before any explicit override.

    ``style`` is a ``StylePreset``. Its own caption numbers are the source of
    truth wherever it has one -- a style that says five words is not overruled
    here -- and this only tightens: in ``key_moments`` the rate and the spacing
    are pulled to the quiet end of whatever the style already allowed.
    """
    mode = coerce_one(mode, CAPTION_MODES, "off")
    per_minute = float(getattr(style, "max_captions_per_minute", 1.5) or 0.0)
    spacing = float(getattr(style, "min_caption_spacing", 8.0) or 0.0)
    words = int(getattr(style, "max_caption_words", 7) or 7)
    duration = float(getattr(style, "caption_duration", 2.2) or 2.2)
    priority = float(getattr(style, "caption_min_priority", 0.55) or 0.55)

    if mode == "key_moments":
        per_minute = min(per_minute, KEY_MOMENT_CEILING)
        spacing = max(spacing, KEY_MOMENT_SPACING)
        priority = max(priority, 0.5)

    return CaptionConfig(
        mode=mode,
        max_per_minute=per_minute,
        min_spacing=spacing,
        max_seconds=min(max(duration + 1.0, 2.0), 5.0),
        max_words=words,
        min_priority=priority,
        style=str(getattr(style, "name", "") or ""),
    ).validated()


def audio_defaults(style, mode: str = "placeholders") -> AudioPolishConfig:
    """Audio polish settings for one style, before any explicit override."""
    name = _slug(getattr(style, "name", "") or "")
    table = dict(DEFAULT_AUDIO)
    table.update(STYLE_AUDIO.get(name, {}))
    return AudioPolishConfig(
        mode=coerce_one(mode, AUDIO_POLISH_MODES, "off"),
        max_sfx_per_minute=table["max_sfx_per_minute"],
        min_spacing=table["min_spacing"],
        music_bed=bool(table["music_bed"]),
        style=name,
    ).validated()


def allowed_cue_kinds(style) -> set:
    """Which cue kinds this style tolerates.

    Read off the preset's own ``audio_kinds`` and ``forbidden_kinds`` rather
    than duplicated here, so a style that forbids whooshes forbids them in this
    pass too without anybody remembering to say it twice.
    """
    allowed_layers = set(getattr(style, "audio_kinds", ()) or ())
    forbidden = set(getattr(style, "forbidden_kinds", ()) or ())
    out: set = set()
    # A cue is allowed when *any* of the layer kinds that could express it is
    # allowed. A bed is the reason for the plural: a style that permits
    # ``music_start`` but not ``tension_bed`` still permits music underneath a
    # stretch, and reading only the first name would silently forbid it.
    for cue, layer_kinds in (
        ("riser", ("music_rise",)),
        ("hit", ("impact_sfx", "comedic_sfx")),
        ("whoosh", ("whoosh",)),
        ("ambience", ("ambience",)),
        ("music_bed", ("tension_bed", "music_start")),
        ("silence_drop", ("silence_hold",)),
    ):
        usable = [kind for kind in layer_kinds if kind not in forbidden]
        if allowed_layers:
            usable = [kind for kind in usable if kind in allowed_layers]
        if usable:
            out.add(cue)
    return out


# ---------------------------------------------------------------------------
# One caption
# ---------------------------------------------------------------------------

def caption_id_for(kind: str, at: float, text: str) -> str:
    return f"cap_{_slug(kind)[:16]}_{short_hash(round(at, 2), text, length=8)}"


def cue_id_for(kind: str, at: float) -> str:
    return f"cue_{_slug(kind)[:14]}_{short_hash(round(at, 2), length=8)}"


@dataclass
class CaptionDecision:
    """One transcript line, and what this pass decided about it.

    Accepted and rejected lines are the same record. A refused caption keeps
    its text and its reason, because "forty lines were considered and these
    four earned a caption" is the only way to tell restraint from a bug.
    """

    caption_id: str = ""
    accepted: bool = False
    #: One of ``KEY_MOMENTS``, or "" when nothing matched.
    moment: str = ""
    #: The condensed text that would go on screen.
    text: str = ""
    #: What was actually said, before condensing.
    full_line: str = ""
    condensed: bool = False

    #: Position on the cut. ``-1`` when the line was cut out of the edit.
    start: float = -1.0
    end: float = -1.0
    #: Position in the source file it was said in.
    source_start: float = 0.0
    source_end: float = 0.0
    asset_id: str = ""
    segment_id: str = ""
    placement_id: str = ""

    #: Where on screen it would go, from the style's safe zones.
    zone: str = ""
    priority: float = 0.0
    #: ASR confidence, when the transcript carried one.
    confidence: float = 1.0

    #: Why this line is (or is not) a key moment, in plain English.
    reason: str = ""
    #: The named rule that refused it, from ``CAPTION_REJECT_REASONS``.
    reject_reason: str = ""
    #: What that rule actually saw.
    reject_detail: str = ""
    evidence: list[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start) if self.start >= 0 else 0.0

    @property
    def words(self) -> int:
        return len([w for w in self.text.split() if w])

    def line(self) -> str:
        mark = "+" if self.accepted else "-"
        # A line refused before it was placed has no position on the cut. That
        # is not the same as "cut from the edit", which is one specific
        # refusal, so it prints as unknown rather than borrowing that meaning.
        where = f"{self.start:7.2f}" if self.start >= 0 else "     - "
        tail = self.reason if self.accepted else (
            f"{self.reject_reason}: {self.reject_detail or self.reason}")
        return (f'{mark} {where}  {self.moment or "-":<16} '
                f'"{self.text[:44]}"  {tail[:70]}')

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration"] = round(self.duration, 3)
        data["words"] = self.words
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CaptionDecision":
        data = data or {}
        return cls(
            caption_id=_text(data.get("caption_id"), 80),
            accepted=bool(data.get("accepted")),
            moment=coerce_one(data.get("moment"), KEY_MOMENTS, ""),
            text=_text(data.get("text"), 300),
            full_line=_text(data.get("full_line"), 500),
            condensed=bool(data.get("condensed")),
            start=as_float(data.get("start"), -1.0),
            end=as_float(data.get("end"), -1.0),
            source_start=as_float(data.get("source_start")),
            source_end=as_float(data.get("source_end")),
            asset_id=_text(data.get("asset_id"), 120),
            segment_id=_text(data.get("segment_id"), 120),
            placement_id=_text(data.get("placement_id"), 120),
            zone=_text(data.get("zone"), 40),
            priority=clamp01(data.get("priority"), 0.0),
            confidence=clamp01(data.get("confidence"), 1.0),
            reason=_text(data.get("reason"), 400),
            reject_reason=coerce_one(
                data.get("reject_reason"), CAPTION_REJECT_REASONS, ""),
            reject_detail=_text(data.get("reject_detail"), 400),
            evidence=as_text_list(data.get("evidence"), limit=20),
        )


@dataclass
class CaptionPlan:
    """Every line this pass looked at, and what became of it."""

    name: str = "structure"
    mode: str = "off"
    config: CaptionConfig = field(default_factory=CaptionConfig)
    style: str = ""
    sequence_name: str = ""
    cut_duration: float = 0.0
    decisions: list[CaptionDecision] = field(default_factory=list)
    #: Where the sidecar subtitle file was written, when one was.
    sidecar_path: str = ""
    #: Whether captions are in the rendered proxy. Always False today, and the
    #: report says why.
    burned_in: bool = False
    warnings: list[str] = field(default_factory=list)
    safety_notes: list[str] = field(default_factory=list)
    generated_at: str = ""
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.decisions)

    @property
    def accepted(self) -> list[CaptionDecision]:
        return [d for d in self.decisions if d.accepted]

    @property
    def rejected(self) -> list[CaptionDecision]:
        return [d for d in self.decisions if not d.accepted]

    @property
    def per_minute(self) -> float:
        if self.cut_duration <= 0:
            return 0.0
        return round(len(self.accepted) / (self.cut_duration / 60.0), 3)

    def by_reject_reason(self) -> dict:
        out: dict = {}
        for decision in self.rejected:
            key = decision.reject_reason or "unknown"
            out[key] = out.get(key, 0) + 1
        return out

    def by_moment(self) -> dict:
        out: dict = {}
        for decision in self.accepted:
            key = decision.moment or "unknown"
            out[key] = out.get(key, 0) + 1
        return out

    def stats(self) -> dict:
        accepted = self.accepted
        return {
            "considered": len(self.decisions),
            "accepted": len(accepted),
            "rejected": len(self.rejected),
            "captions_per_minute": self.per_minute,
            "longest_seconds": round(
                max((d.duration for d in accepted), default=0.0), 3),
            "most_words": max((d.words for d in accepted), default=0),
            "condensed": sum(1 for d in accepted if d.condensed),
            "by_moment": self.by_moment(),
            "by_reject_reason": self.by_reject_reason(),
            "cut_duration": round(self.cut_duration, 2),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "mode": self.mode,
            "style": self.style,
            "sequence_name": self.sequence_name,
            "generated_at": self.generated_at,
            "config": self.config.to_dict(),
            "stats": self.stats(),
            "sidecar_path": self.sidecar_path,
            "burned_in": self.burned_in,
            "not_measured": NOT_MEASURED,
            "warnings": list(self.warnings),
            "safety_notes": list(self.safety_notes),
            "accepted": [d.to_dict() for d in self.accepted],
            "rejected": [d.to_dict() for d in self.rejected],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CaptionPlan":
        data = data or {}
        raw = _dicts(data.get("accepted")) + _dicts(data.get("rejected"))
        decisions = [CaptionDecision.from_dict(item) for item in raw]
        # A round trip must not silently promote a rejection.
        for decision, source in zip(decisions, raw):
            decision.accepted = bool(source.get("accepted"))
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            mode=coerce_one(data.get("mode"), CAPTION_MODES, "off"),
            config=CaptionConfig.from_dict(data.get("config")),
            style=_text(data.get("style"), 80),
            sequence_name=_text(data.get("sequence_name"), 200),
            cut_duration=as_float((data.get("stats") or {}).get("cut_duration")),
            decisions=sorted(decisions, key=lambda d: (d.start, d.caption_id)),
            sidecar_path=_text(data.get("sidecar_path"), 500),
            burned_in=bool(data.get("burned_in")),
            warnings=as_text_list(data.get("warnings"), limit=60),
            safety_notes=as_text_list(data.get("safety_notes"), limit=60),
            generated_at=_text(data.get("generated_at"), 40),
        )


# ---------------------------------------------------------------------------
# One audio cue
# ---------------------------------------------------------------------------

@dataclass
class AudioCue:
    """One sound this pass thinks belongs somewhere, and whether it may."""

    cue_id: str = ""
    kind: str = "hit"
    accepted: bool = False

    start: float = 0.0
    end: float = 0.0
    #: The moment this cue is *for*, in plain English.
    target: str = ""
    #: The key-moment kind it serves, when it serves one.
    moment: str = ""
    placement_id: str = ""
    segment_id: str = ""

    priority: float = 0.5
    reason: str = ""
    reject_reason: str = ""
    reject_detail: str = ""
    evidence: list[str] = field(default_factory=list)

    #: What was matched from the library, when anything was.
    asset_id: str = ""
    asset_path: str = ""
    asset_filename: str = ""
    match_score: float = 0.0
    #: True when nothing plays here: either the mode is placeholders-only, or
    #: nothing in the library fits.
    placeholder_only: bool = True
    #: The sound a person would have to go and find.
    placeholder: str = ""
    #: True when the library was searched and came back empty for this cue.
    missing_asset: bool = False

    #: Anything a person needs to know before trusting this: ducked under
    #: speech, tiled bed, level unmeasured.
    safety_notes: list[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def counts_as_sfx(self) -> bool:
        return self.kind in SFX_KINDS

    def line(self) -> str:
        mark = "+" if self.accepted else "-"
        tail = self.reason if self.accepted else (
            f"{self.reject_reason}: {self.reject_detail or self.reason}")
        source = self.asset_filename or (
            f"[{self.placeholder}]" if self.placeholder else "[placeholder]")
        return (f"{mark} {self.start:7.2f}  {self.kind:<12} {source[:28]:<28} "
                f"{tail[:60]}")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration"] = round(self.duration, 3)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        data["counts_as_sfx"] = self.counts_as_sfx
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AudioCue":
        data = data or {}
        start = as_float(data.get("start"))
        return cls(
            cue_id=_text(data.get("cue_id"), 80),
            kind=coerce_one(data.get("kind"), AUDIO_CUE_KINDS, "hit"),
            accepted=bool(data.get("accepted")),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            target=_text(data.get("target"), 300),
            moment=coerce_one(data.get("moment"), KEY_MOMENTS, ""),
            placement_id=_text(data.get("placement_id"), 120),
            segment_id=_text(data.get("segment_id"), 120),
            priority=clamp01(data.get("priority"), 0.5),
            reason=_text(data.get("reason"), 400),
            reject_reason=coerce_one(
                data.get("reject_reason"), AUDIO_REJECT_REASONS, ""),
            reject_detail=_text(data.get("reject_detail"), 400),
            evidence=as_text_list(data.get("evidence"), limit=20),
            asset_id=_text(data.get("asset_id"), 120),
            asset_path=_text(data.get("asset_path"), 500),
            asset_filename=_text(data.get("asset_filename"), 200),
            match_score=clamp01(data.get("match_score"), 0.0),
            placeholder_only=bool(data.get("placeholder_only", True)),
            placeholder=_text(data.get("placeholder"), 200),
            missing_asset=bool(data.get("missing_asset")),
            safety_notes=as_text_list(data.get("safety_notes"), limit=20),
        )


@dataclass
class AudioPolishPlan:
    """Every cue this pass considered, and what became of it."""

    name: str = "structure"
    mode: str = "off"
    config: AudioPolishConfig = field(default_factory=AudioPolishConfig)
    style: str = ""
    sequence_name: str = ""
    cut_duration: float = 0.0
    cues: list[AudioCue] = field(default_factory=list)
    #: Where the library was read from, when it was read at all.
    library_root: str = ""
    library_size: int = 0
    warnings: list[str] = field(default_factory=list)
    safety_notes: list[str] = field(default_factory=list)
    generated_at: str = ""
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.cues)

    @property
    def accepted(self) -> list[AudioCue]:
        return [cue for cue in self.cues if cue.accepted]

    @property
    def rejected(self) -> list[AudioCue]:
        return [cue for cue in self.cues if not cue.accepted]

    @property
    def placed(self) -> list[AudioCue]:
        """Accepted cues that name a real file."""
        return [cue for cue in self.accepted if cue.asset_path]

    @property
    def placeholders(self) -> list[AudioCue]:
        return [cue for cue in self.accepted if not cue.asset_path]

    @property
    def missing(self) -> list[AudioCue]:
        return [cue for cue in self.accepted if cue.missing_asset]

    @property
    def effects(self) -> list[AudioCue]:
        """Accepted cues that count against the effect ceiling.

        A bed and a silence are not effects: one runs underneath for a minute
        and the other is nothing at all.
        """
        return [cue for cue in self.accepted if cue.counts_as_sfx]

    @property
    def sfx_per_minute(self) -> float:
        if self.cut_duration <= 0:
            return 0.0
        return round(len(self.effects) / (self.cut_duration / 60.0), 3)

    def shopping_list(self) -> list[dict]:
        """One entry per distinct sound a person would have to go and find."""
        out: dict = {}
        for cue in self.missing:
            key = cue.placeholder or cue.kind
            entry = out.setdefault(key, {
                "placeholder": key, "kind": cue.kind, "count": 0,
                "requirement": CUE_REQUIREMENT.get(cue.kind, ""),
                "first_at": round(cue.start, 2),
            })
            entry["count"] += 1
        return sorted(out.values(), key=lambda item: -item["count"])

    def by_kind(self) -> dict:
        out: dict = {}
        for cue in self.accepted:
            out[cue.kind] = out.get(cue.kind, 0) + 1
        return out

    def by_reject_reason(self) -> dict:
        out: dict = {}
        for cue in self.rejected:
            key = cue.reject_reason or "unknown"
            out[key] = out.get(key, 0) + 1
        return out

    def stats(self) -> dict:
        return {
            "considered": len(self.cues),
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "placed": len(self.placed),
            "placeholders": len(self.placeholders),
            "missing_assets": len(self.missing),
            "effects": len(self.effects),
            "sfx_per_minute": self.sfx_per_minute,
            "by_kind": self.by_kind(),
            "by_reject_reason": self.by_reject_reason(),
            "cut_duration": round(self.cut_duration, 2),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "mode": self.mode,
            "style": self.style,
            "sequence_name": self.sequence_name,
            "generated_at": self.generated_at,
            "config": self.config.to_dict(),
            "stats": self.stats(),
            "library_root": self.library_root,
            "library_size": self.library_size,
            "not_measured": NOT_MEASURED,
            "warnings": list(self.warnings),
            "safety_notes": list(self.safety_notes),
            "shopping_list": self.shopping_list(),
            "accepted": [cue.to_dict() for cue in self.accepted],
            "rejected": [cue.to_dict() for cue in self.rejected],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AudioPolishPlan":
        data = data or {}
        raw = _dicts(data.get("accepted")) + _dicts(data.get("rejected"))
        cues = [AudioCue.from_dict(item) for item in raw]
        for cue, source in zip(cues, raw):
            cue.accepted = bool(source.get("accepted"))
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            mode=coerce_one(data.get("mode"), AUDIO_POLISH_MODES, "off"),
            config=AudioPolishConfig.from_dict(data.get("config")),
            style=_text(data.get("style"), 80),
            sequence_name=_text(data.get("sequence_name"), 200),
            cut_duration=as_float((data.get("stats") or {}).get("cut_duration")),
            cues=sorted(cues, key=lambda cue: (cue.start, cue.cue_id)),
            library_root=_text(data.get("library_root"), 500),
            library_size=int(as_float(data.get("library_size"))),
            warnings=as_text_list(data.get("warnings"), limit=60),
            safety_notes=as_text_list(data.get("safety_notes"), limit=60),
            generated_at=_text(data.get("generated_at"), 40),
        )
