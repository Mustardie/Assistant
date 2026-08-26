"""Types for the conform pass: the layer that makes the decisions real.

Every earlier pass in this system answers "what should happen". None of them
answered "what does Premiere have to be told". For the cut, the style layer and
the asset library that gap was already closed. For four things it was not:

* **captions** existed only as an ``.srt`` sidecar,
* **sound cues** existed only as notes with timestamps,
* **visual treatments** existed as a plan nobody executed,
* **colour, music, mix and transitions** were not decided at all.

This module holds the decision records for the last group and the plan object
that carries all of it into Premiere. The decisions are deliberately small and
explicit: the point is that a person can read *why* the edit is a degree
warmer, not that the system has opinions about colour science.

The shape follows the passes that came before it, on purpose -- a
``*Plan`` with an ``ops`` list, a ``dry_run_passed`` flag and an
``as_edit_plan()`` -- so it runs through the same validator, the same engine
and the same execution guards, rather than opening a second road into Premiere.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from editing.schema import as_float, as_text_list
from editing.tracks import DEFAULT_LAYOUT, TrackLayout

#: How much of the finished edit this pass is allowed to build.
#:
#: ``off``        nothing; the pass still reports what it would have done
#: ``captions``   captions and text only
#: ``sound``      captions plus sound effects, music and the mix
#: ``full``       everything, including visual treatments and colour
CONFORM_MODES = ("off", "captions", "sound", "full")

#: Named colour treatments. Small on purpose: the goal is that the editor can
#: *intentionally* choose a look and have it happen, not that it can grade.
#: Each is a set of Lumetri parameters the ``color.grade`` operation takes.
COLOR_LOOKS = {
    "neutral": {
        "summary": "Leave the footage alone.",
        "params": {},
    },
    "clean": {
        "summary": "A touch of contrast and saturation; the footage, tidied.",
        "params": {"contrast": 6.0, "saturation": 106.0, "sharpen": 6.0},
    },
    "warm": {
        "summary": "Warmer and slightly lifted: friendly, daytime, casual.",
        "params": {"temperature": 8.0, "contrast": 5.0, "shadows": 4.0,
                   "saturation": 108.0},
    },
    "cool": {
        "summary": "Cooler and harder: night, tension, machinery.",
        "params": {"temperature": -10.0, "contrast": 10.0, "blacks": -4.0,
                   "saturation": 96.0},
    },
    "punchy": {
        "summary": "High contrast and vivid: fast, loud, gameplay.",
        "params": {"contrast": 14.0, "saturation": 112.0, "vibrance": 10.0,
                   "highlights": -6.0, "shadows": 6.0, "sharpen": 10.0},
    },
    "flat": {
        "summary": "Contrast pulled down, for footage that is already crushed.",
        "params": {"contrast": -8.0, "shadows": 10.0, "highlights": -8.0},
    },
}

#: Why a colour look was chosen. Every decision names one.
COLOR_REASONS = (
    "style_default", "dark_footage", "bright_footage", "low_saturation",
    "high_saturation", "requested", "no_evidence",
)

#: Loudness targets, in LUFS. -14 is the level streaming platforms normalise
#: to, so mixing to it means the platform does nothing on the way out.
DEFAULT_TARGET_LUFS = -14.0
#: True peak ceiling. -1 dBTP leaves room for the encoder's own overshoot.
DEFAULT_PEAK_CEILING_DB = -1.0
#: How far under dialogue a music bed sits. A widely used starting point, and
#: the number this system will calibrate first once real edits exist.
DEFAULT_MUSIC_UNDER_DIALOGUE_DB = -18.0
#: How far under dialogue a one-shot effect sits.
DEFAULT_SFX_UNDER_DIALOGUE_DB = -8.0


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(value, limit: int = 400) -> str:
    return str(value if value is not None else "")[:limit]


def _dicts(value) -> list[dict]:
    return [item for item in (value or []) if isinstance(item, dict)]


def decision_id_for(kind: str, at: float, detail: str = "") -> str:
    seed = f"{kind}|{round(float(at or 0.0), 3)}|{detail}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

@dataclass
class ColorDecision:
    """One colour treatment, and the evidence for it.

    Applied to the programme track as a whole rather than per clip: a grade
    that changes at every cut is a mistake, not a style, and this pass has no
    way to tell one shot from another well enough to shot-match.
    """

    look: str = "neutral"
    applied: bool = False
    reason: str = "no_evidence"
    summary: str = ""
    #: The Lumetri parameters that will be sent, after any strength scaling.
    params: dict = field(default_factory=dict)
    #: 0-1. Scales every parameter away from its neutral value.
    strength: float = 1.0
    #: What the footage measured, when it was measured at all.
    measured: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    clips_affected: int = 0

    def line(self) -> str:
        mark = "+" if self.applied else "-"
        return (f"{mark} colour {self.look:<8} x{self.strength:.2f}  "
                f"{self.reason:<16} {self.summary[:60]}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ColorDecision":
        data = data or {}
        return cls(
            look=_text(data.get("look"), 40) or "neutral",
            applied=bool(data.get("applied")),
            reason=_text(data.get("reason"), 40) or "no_evidence",
            summary=_text(data.get("summary")),
            params=dict(data.get("params") or {}),
            strength=as_float(data.get("strength"), 1.0),
            measured=dict(data.get("measured") or {}),
            evidence=as_text_list(data.get("evidence"), limit=20),
            clips_affected=int(as_float(data.get("clips_affected"))),
        )


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

@dataclass
class MusicDecision:
    """One music bed: which track, where it sits, and how loud.

    Deliberately one bed rather than a cue sheet. A first music pass that
    reliably lays one appropriate track under the episode at a measured level,
    trimmed to the cut and faded at both ends, is worth more than a scoring
    system that never runs.
    """

    decision_id: str = ""
    placed: bool = False
    asset_id: str = ""
    asset_path: str = ""
    asset_name: str = ""
    #: Where on the cut it starts and ends, in sequence seconds.
    start: float = 0.0
    end: float = 0.0
    #: Which part of the music file is used.
    source_in: float = 0.0
    source_out: float = 0.0
    #: How many times the track is repeated to cover the range.
    loops: int = 1
    track: str = "A3"
    gain_db: float = DEFAULT_MUSIC_UNDER_DIALOGUE_DB
    fade_in: float = 1.5
    fade_out: float = 2.5
    #: Speech ranges the bed ducks under, in sequence time.
    ducks_under: list = field(default_factory=list)
    duck_db: float = -8.0
    #: Beat grid read off the file, when one could be read.
    bpm: float = 0.0
    beat_offset: float = 0.0
    #: True when the start was moved to land on a beat.
    beat_aligned: bool = False
    reason: str = ""
    reject_reason: str = ""
    evidence: list[str] = field(default_factory=list)
    measured: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def line(self) -> str:
        mark = "+" if self.placed else "-"
        source = self.asset_name or "[none]"
        tail = self.reason if self.placed else (self.reject_reason or self.reason)
        return (f"{mark} music {self.start:7.2f}-{self.end:7.2f} "
                f"{source[:28]:<28} {self.gain_db:6.1f}dB  {tail[:50]}")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration"] = round(self.duration, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MusicDecision":
        data = data or {}
        return cls(
            decision_id=_text(data.get("decision_id"), 40),
            placed=bool(data.get("placed")),
            asset_id=_text(data.get("asset_id"), 120),
            asset_path=_text(data.get("asset_path"), 500),
            asset_name=_text(data.get("asset_name"), 200),
            start=as_float(data.get("start")),
            end=as_float(data.get("end")),
            source_in=as_float(data.get("source_in")),
            source_out=as_float(data.get("source_out")),
            loops=int(as_float(data.get("loops"), 1.0)) or 1,
            track=_text(data.get("track"), 8) or "A3",
            gain_db=as_float(data.get("gain_db"),
                             DEFAULT_MUSIC_UNDER_DIALOGUE_DB),
            fade_in=as_float(data.get("fade_in"), 1.5),
            fade_out=as_float(data.get("fade_out"), 2.5),
            ducks_under=[list(r) for r in (data.get("ducks_under") or [])
                         if isinstance(r, (list, tuple)) and len(r) == 2],
            duck_db=as_float(data.get("duck_db"), -8.0),
            bpm=as_float(data.get("bpm")),
            beat_offset=as_float(data.get("beat_offset")),
            beat_aligned=bool(data.get("beat_aligned")),
            reason=_text(data.get("reason")),
            reject_reason=_text(data.get("reject_reason"), 80),
            evidence=as_text_list(data.get("evidence"), limit=20),
            measured=dict(data.get("measured") or {}),
        )


# ---------------------------------------------------------------------------
# Mix
# ---------------------------------------------------------------------------

@dataclass
class LevelMeasurement:
    """What one audio source actually measures.

    Measured, not assumed. Everything about levels in this system before now
    was a constant in a table with a comment saying it was an opinion; this is
    the number FFmpeg read off the file.
    """

    source: str = ""
    role: str = "dialogue"
    path: str = ""
    lufs: float = 0.0
    peak_db: float = 0.0
    #: Loudness range, in LU. High means the source is uneven.
    lra: float = 0.0
    measured: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LevelMeasurement":
        data = data or {}
        return cls(
            source=_text(data.get("source"), 200),
            role=_text(data.get("role"), 40) or "dialogue",
            path=_text(data.get("path"), 500),
            lufs=as_float(data.get("lufs")),
            peak_db=as_float(data.get("peak_db")),
            lra=as_float(data.get("lra")),
            measured=bool(data.get("measured")),
            error=_text(data.get("error"), 300),
        )


@dataclass
class MixDecision:
    """The finished mix: one measured gain per audio role, plus the fades.

    The contract is narrow and checkable. Dialogue is brought to the target
    loudness. Music and effects are set relative to *measured* dialogue rather
    than to a guess. Nothing is allowed to push the true peak above the
    ceiling. If a source could not be measured, its gain stays at the documented
    default and the decision says so instead of pretending.
    """

    target_lufs: float = DEFAULT_TARGET_LUFS
    peak_ceiling_db: float = DEFAULT_PEAK_CEILING_DB
    #: role -> dB of gain to apply
    gains: dict = field(default_factory=dict)
    measurements: list[LevelMeasurement] = field(default_factory=list)
    #: Ranges where speech is present, in sequence time; what music ducks under.
    speech_ranges: list = field(default_factory=list)
    #: Fade lengths applied at the head and tail of the programme audio.
    programme_fade_in: float = 0.0
    programme_fade_out: float = 0.5
    #: True when every role in the mix was measured rather than assumed.
    fully_measured: bool = False
    clipping_prevented: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def gain_for(self, role: str) -> float:
        return as_float(self.gains.get(role), 0.0)

    def measurement_for(self, role: str) -> Optional[LevelMeasurement]:
        for entry in self.measurements:
            if entry.role == role and entry.measured:
                return entry
        return None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["measurements"] = [m.to_dict() for m in self.measurements]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MixDecision":
        data = data or {}
        return cls(
            target_lufs=as_float(data.get("target_lufs"), DEFAULT_TARGET_LUFS),
            peak_ceiling_db=as_float(data.get("peak_ceiling_db"),
                                     DEFAULT_PEAK_CEILING_DB),
            gains={str(k): as_float(v) for k, v in
                   (data.get("gains") or {}).items()},
            measurements=[LevelMeasurement.from_dict(m)
                          for m in _dicts(data.get("measurements"))],
            speech_ranges=[list(r) for r in (data.get("speech_ranges") or [])
                           if isinstance(r, (list, tuple)) and len(r) == 2],
            programme_fade_in=as_float(data.get("programme_fade_in")),
            programme_fade_out=as_float(data.get("programme_fade_out"), 0.5),
            fully_measured=bool(data.get("fully_measured")),
            clipping_prevented=bool(data.get("clipping_prevented")),
            notes=as_text_list(data.get("notes"), limit=40),
            warnings=as_text_list(data.get("warnings"), limit=40),
        )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

@dataclass
class TransitionDecision:
    """One deliberate transition at one cut.

    "Deliberate" is the whole design. Transitions are the easiest thing in an
    editor to overuse, so this pass places one only where the cut itself says
    something changed -- a scene boundary, the end of the episode -- and never
    on an ordinary cut between two shots of the same thing.
    """

    decision_id: str = ""
    applied: bool = False
    at: float = 0.0
    clip_index: int = 0
    track: str = "V1"
    edge: str = "out"
    transition: str = "Cross Dissolve"
    duration: float = 0.5
    reason: str = ""
    reject_reason: str = ""
    evidence: list[str] = field(default_factory=list)

    def line(self) -> str:
        mark = "+" if self.applied else "-"
        tail = self.reason if self.applied else (self.reject_reason or self.reason)
        return (f"{mark} {self.at:7.2f}  {self.transition[:18]:<18} "
                f"{self.duration:.2f}s  {tail[:56]}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TransitionDecision":
        data = data or {}
        return cls(
            decision_id=_text(data.get("decision_id"), 40),
            applied=bool(data.get("applied")),
            at=as_float(data.get("at")),
            clip_index=int(as_float(data.get("clip_index"))),
            track=_text(data.get("track"), 8) or "V1",
            edge=_text(data.get("edge"), 12) or "out",
            transition=_text(data.get("transition"), 80) or "Cross Dissolve",
            duration=as_float(data.get("duration"), 0.5),
            reason=_text(data.get("reason")),
            reject_reason=_text(data.get("reject_reason"), 80),
            evidence=as_text_list(data.get("evidence"), limit=20),
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class ConformConfig:
    """What this pass is allowed to build, and to what target."""

    mode: str = "full"
    layout: TrackLayout = field(default_factory=lambda: DEFAULT_LAYOUT)

    captions: bool = True
    sound: bool = True
    music: bool = True
    visuals: bool = True
    color: bool = True
    transitions: bool = True

    #: An explicit look name overrides whatever the footage suggests.
    color_look: str = ""
    color_strength: float = 1.0

    target_lufs: float = DEFAULT_TARGET_LUFS
    peak_ceiling_db: float = DEFAULT_PEAK_CEILING_DB
    music_under_dialogue_db: float = DEFAULT_MUSIC_UNDER_DIALOGUE_DB
    sfx_under_dialogue_db: float = DEFAULT_SFX_UNDER_DIALOGUE_DB

    #: Folder of music the pass may choose from. Empty means the asset library.
    music_library: str = ""
    #: Ceiling on deliberate transitions across the whole episode.
    max_transitions: int = 6
    #: Caption styling, passed through to ``text.create``.
    caption_font: str = "Arial"
    caption_size: int = 54
    caption_color: str = "#FFFFFF"
    caption_stroke_color: str = "#000000"
    caption_stroke_width: float = 4.0
    #: How long a caption fades up and down. Zero disables the animation.
    caption_fade: float = 0.18

    def enabled(self, part: str) -> bool:
        """Whether one part of the pass may run, given the mode."""
        if self.mode == "off":
            return False
        if self.mode == "captions":
            allowed = {"captions"}
        elif self.mode == "sound":
            allowed = {"captions", "sound", "music"}
        else:
            allowed = {"captions", "sound", "music", "visuals", "color",
                       "transitions"}
        return part in allowed and bool(getattr(self, part, False))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["layout"] = self.layout.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ConformConfig":
        data = data or {}
        known = {f: data[f] for f in
                 ("mode", "captions", "sound", "music", "visuals", "color",
                  "transitions", "color_look", "color_strength", "target_lufs",
                  "peak_ceiling_db", "music_under_dialogue_db",
                  "sfx_under_dialogue_db", "music_library", "max_transitions",
                  "caption_font", "caption_size", "caption_color",
                  "caption_stroke_color", "caption_stroke_width",
                  "caption_fade")
                 if f in data}
        return cls(layout=TrackLayout.from_dict(data.get("layout")), **known)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass
class ConformPlan:
    """Everything the earlier passes decided, as operations Premiere can run.

    Carries the same surface as the rough cut plan on purpose: ``ops``,
    ``as_edit_plan()``, ``dry_run_passed``. That is what lets it go through the
    existing validator, engine and execution guards instead of needing its own.
    """

    name: str = "structure"
    sequence_name: str = ""
    config: ConformConfig = field(default_factory=ConformConfig)
    layout: TrackLayout = field(default_factory=lambda: DEFAULT_LAYOUT)
    cut_duration: float = 0.0

    ops: list[dict] = field(default_factory=list)

    color: ColorDecision = field(default_factory=ColorDecision)
    music: MusicDecision = field(default_factory=MusicDecision)
    mix: MixDecision = field(default_factory=MixDecision)
    transitions: list[TransitionDecision] = field(default_factory=list)

    #: How many operations each contributing layer produced. This is the
    #: number that answers "did the captions actually become anything".
    contributions: dict = field(default_factory=dict)
    #: Decisions from an earlier pass that could not become an operation, with
    #: the reason. Never silently dropped.
    unconverted: list[dict] = field(default_factory=list)

    dry_run_passed: bool = False
    dry_run_error: Optional[dict] = None
    explanation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_at: str = ""
    schema_version: int = 1

    @property
    def operation_count(self) -> int:
        return len(self.ops)

    def as_edit_plan(self, *, dry_run: bool = False) -> dict:
        plan = {"ops": list(self.ops), "on_error": "abort"}
        if dry_run:
            plan["dry_run"] = True
        return plan

    def counts(self) -> dict:
        by_op: dict = {}
        for op in self.ops:
            name = op.get("op", "?")
            by_op[name] = by_op.get(name, 0) + 1
        return dict(sorted(by_op.items()))

    def stats(self) -> dict:
        return {
            "operations": self.operation_count,
            "by_operation": self.counts(),
            "contributions": dict(self.contributions),
            "unconverted": len(self.unconverted),
            "colour": self.color.look if self.color.applied else "none",
            "music": self.music.asset_name if self.music.placed else "none",
            "transitions": len([t for t in self.transitions if t.applied]),
            "mix_measured": self.mix.fully_measured,
            "dry_run_passed": self.dry_run_passed,
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "sequence_name": self.sequence_name,
            "config": self.config.to_dict(),
            "layout": self.layout.to_dict(),
            "cut_duration": round(self.cut_duration, 3),
            "ops": list(self.ops),
            "color": self.color.to_dict(),
            "music": self.music.to_dict(),
            "mix": self.mix.to_dict(),
            "transitions": [t.to_dict() for t in self.transitions],
            "contributions": dict(self.contributions),
            "unconverted": list(self.unconverted),
            "dry_run_passed": self.dry_run_passed,
            "dry_run_error": self.dry_run_error,
            "explanation": list(self.explanation),
            "warnings": list(self.warnings),
            "generated_at": self.generated_at or now(),
            "schema_version": self.schema_version,
            "stats": self.stats(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConformPlan":
        data = data or {}
        return cls(
            name=_text(data.get("name"), 80) or "structure",
            sequence_name=_text(data.get("sequence_name"), 200),
            config=ConformConfig.from_dict(data.get("config")),
            layout=TrackLayout.from_dict(data.get("layout")),
            cut_duration=as_float(data.get("cut_duration")),
            ops=_dicts(data.get("ops")),
            color=ColorDecision.from_dict(data.get("color")),
            music=MusicDecision.from_dict(data.get("music")),
            mix=MixDecision.from_dict(data.get("mix")),
            transitions=[TransitionDecision.from_dict(t)
                         for t in _dicts(data.get("transitions"))],
            contributions=dict(data.get("contributions") or {}),
            unconverted=_dicts(data.get("unconverted")),
            dry_run_passed=bool(data.get("dry_run_passed")),
            dry_run_error=data.get("dry_run_error"),
            explanation=as_text_list(data.get("explanation"), limit=400),
            warnings=as_text_list(data.get("warnings"), limit=100),
            generated_at=_text(data.get("generated_at"), 40),
        )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

@dataclass
class DeliveryResult:
    """Where the finished video went, or why there isn't one.

    The pipeline used to end at "edit plan generated successfully". This is the
    record that lets it end at "the file is at this path and it is this many
    bytes" -- or, just as usefully, at an honest account of why it is not.
    """

    sequence_name: str = ""
    requested_path: str = ""
    output_path: str = ""
    exists: bool = False
    size_bytes: int = 0
    duration: float = 0.0
    #: "direct" (Premiere rendered it) or "media_encoder" (AME did / is doing).
    method: str = ""
    preset: str = ""
    started: bool = False
    complete: bool = False
    waited: float = 0.0
    error: Optional[dict] = None
    warnings: list[str] = field(default_factory=list)
    finished_at: str = ""

    @property
    def delivered(self) -> bool:
        """True only when a file actually exists and has content in it."""
        return bool(self.exists and self.size_bytes > 0)

    def line(self) -> str:
        if self.delivered:
            return (f"delivered  {self.output_path}  "
                    f"{self.size_bytes / 1_000_000:.1f} MB  via {self.method}")
        if self.started and not self.complete:
            return f"rendering  {self.requested_path}  via {self.method}"
        reason = (self.error or {}).get("error", "no export was attempted")
        return f"not delivered  {reason}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["delivered"] = self.delivered
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DeliveryResult":
        data = data or {}
        return cls(
            sequence_name=_text(data.get("sequence_name"), 200),
            requested_path=_text(data.get("requested_path"), 500),
            output_path=_text(data.get("output_path"), 500),
            exists=bool(data.get("exists")),
            size_bytes=int(as_float(data.get("size_bytes"))),
            duration=as_float(data.get("duration")),
            method=_text(data.get("method"), 40),
            preset=_text(data.get("preset"), 500),
            started=bool(data.get("started")),
            complete=bool(data.get("complete")),
            waited=as_float(data.get("waited")),
            error=data.get("error"),
            warnings=as_text_list(data.get("warnings"), limit=40),
            finished_at=_text(data.get("finished_at"), 40),
        )
