"""What a retention-aware cut is, as data.

## The gap this closes

Session 8 built the retention planner: hook candidates, risk zones, setups and
payoffs, open loops, a peak, an ending. It executes nothing, and until now
nothing read it. The seam has been sitting there since
``retention_suggestions_for(stage)`` was written, with a note in the handoff
saying so.

This layer is the consumer. It takes those findings and *changes the cut*:
moves the best hook to the front, compresses the stretches the planner flagged
as sagging, protects the setups a kept payoff needs, and kills ordinary silence
harder than the general selector dares to.

## Five rules

* **The cut is rebuilt, never mutated.** A retention pass produces a new
  ``RoughCutPlan``; the cut it read is untouched on disk. Same rule the critic
  and style passes follow, for the same reason -- disagreeing with the
  retention pass must not cost you the cut it was arguing with.
* **Protection is applied before compression.** A setup a payoff needs cannot
  be compressed by a later rule, because the protection claimed that footage
  first. Ordering is the mechanism; there is no negotiation afterwards.
* **Times are resolved through the episode track, never by arithmetic.** The
  memory records which of two incompatible timebases it used, and guessing
  would put the cold open in the wrong place on a real edit.
* **Nothing is deleted, only decided.** Every rejected retention action stays
  in the plan with the rule that refused it, exactly as Session 2 and Session
  10C do.
* **No analytics, ever.** This layer may say "three risk zones were
  compressed". It may never say retention improved, because nothing here has
  measured a viewer. See :data:`NOT_MEASURED`.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from editing.schema import (
    _slug, as_bool, as_float, as_str_list, clamp01, short_hash,
)

#: How the retention wiring behaves.
#:
#: ``off``                do nothing. The cut is whatever it already was.
#: ``report_only``        decide everything, change no frame, write the report.
#:                        The safe way to find out what it would do.
#: ``retention``          apply on top of the heuristic rough cut.
#: ``director_retention`` apply on top of the director's cut.
#: ``hybrid``             apply on top of whichever cut exists, preferring the
#:                        director's.
MODES = ("off", "report_only", "retention", "director_retention", "hybrid")

#: Modes that actually change ranges.
ACTING_MODES = frozenset({"retention", "director_retention", "hybrid"})

#: Where a retention decision came from. Every decision names one, so a report
#: can say which layer is responsible for a change.
SOURCE_TYPES = (
    "hook",
    "risk",
    "open_loop",
    "setup",
    "payoff",
    "callback",
    "dead_air",
    "director_decision",
    "recommendation",
    "climax",
    "ending",
    "none",
)

#: What one retention decision asks for.
ACTIONS = (
    "cold_open",           # take this range and play it first
    "keep",                # leave this range as it is
    "cut",                 # remove it
    "shorten",             # use part of it
    "speed_up",            # use it, retimed
    "protect",             # keep it, and stop anything else touching it
    "hold",                # keep it at full speed, no effects
    "marker_only",         # change no frame; leave a note
    "reject",              # this was proposed and refused
    "needs_human_review",  # not sure, and saying so
)

#: Actions that change which frames end up in the cut.
CHANGING_ACTIONS = frozenset({"cold_open", "cut", "shorten", "speed_up"})

#: Actions that keep footage and stop anything else touching it.
PROTECTING_ACTIONS = frozenset({"protect", "hold"})

#: Actions that change no frame.
PASSIVE_ACTIONS = frozenset({"marker_only", "reject", "needs_human_review",
                             "keep"})

#: What a decision claims it does for a viewer. Qualitative, always -- see
#: ``NOT_MEASURED``.
VIEWER_EFFECTS = (
    "opens_a_question",
    "answers_a_question",
    "raises_tension",
    "releases_tension",
    "lands_a_joke",
    "removes_a_dull_stretch",
    "keeps_momentum",
    "protects_a_payoff",
    "restates_the_goal",
    "closes_the_episode",
    "none_stated",
)

#: How hard ordinary silence is cut.
AGGRESSIVENESS = ("low", "medium", "high")

#: Seconds of ordinary silence tolerated at each setting. "Ordinary" means
#: silence that serves nothing -- see ``PURPOSEFUL_SILENCE``.
ORDINARY_SILENCE = {"low": 2.0, "medium": 1.2, "high": 0.6}

#: What silence can be *for*. Silence covering one of these is not dead air,
#: it is timing, and cutting it is how an edit stops being funny.
PURPOSEFUL_SILENCE = (
    "comedy_pause",
    "tension",
    "reaction",
    "reveal",
    "aftermath",
    "transition",
    "emotional_beat",
    "setup_payoff_timing",
)

#: Hook kinds that can open an episode. Read off ``HookCandidate.hook_type``.
OPENABLE_HOOKS = frozenset({"danger", "mystery", "failure", "comedy",
                            "reveal", "challenge"})

#: Actions on screen that are never a cold open on their own, however well the
#: hook scored. Opening on somebody walking is the single most common way a
#: Minecraft episode loses a viewer in the first ten seconds.
BORING_OPENERS = frozenset({
    "walking", "running", "sorting", "crafting", "building", "idle",
    "menu", "inventory", "reading", "waiting",
})

#: What happens to the footage a cold open was lifted from.
DUPLICATE_POLICIES = ("remove", "shorten", "keep")

#: Risk types this layer knows how to compress. Anything else becomes a marker.
COMPRESSIBLE_RISKS = frozenset({
    "boring_repetition", "low_visual_change", "dead_air", "mid_video_slump",
    "overlong_explanation", "no_stakes", "payoff_delayed",
})

#: Why a decision was refused. Closed, so a report can group thirty of them.
REJECT_REASONS = (
    "unresolvable",
    "protected_range",
    "would_cut_setup",
    "would_cut_payoff",
    "low_confidence",
    "no_evidence",
    "duplicate_footage",
    "over_compression",
    "too_short",
    "no_hook_found",
    "hook_is_boring",
    "hook_needs_context",
    "hook_spoils_ending",
    "purposeful_silence",
    "speech_present",
    "runtime_cap",
    "disabled",
    "unknown",
)

#: Where a retention pass can go wrong.
FAILURE_STAGES = (
    "config", "no_retention_plan", "no_base_cut", "no_track", "empty_plan",
    "convert", "write", "unknown",
)

#: Said on every plan, every report and every comparison.
NOT_MEASURED = (
    "Nothing here measures retention, watch time or audience response. These "
    "are structural edits made from evidence the earlier passes recorded: "
    "where silence is, where the same action repeats, where a question is "
    "asked and answered. 'Risk zones compressed' is a count of what was "
    "changed, not a claim about what a viewer will do."
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


def _env(name: str, default: str) -> str:
    import os
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    import os
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetentionCutConfig:
    """Everything that decides how hard the retention wiring pulls.

    Frozen, and serialised whole onto the plan, so a cut always says what
    settings produced it.

    ``mode``
        ``report_only`` is the one to start with: it decides everything and
        changes no frame, so you can read what it *would* do before letting it.
    ``dead_air_aggressiveness``
        ``high`` cuts ordinary silence past 0.6s, ``low`` past 2.0s. Silence
        that serves comedy, tension or a reaction is never ordinary and is
        governed by ``max_purposeful_silence`` instead.
    ``max_compression_share``
        Ceiling on how much of the base cut the sag pass may remove. A
        retention pass that removes 80% of an episode has not compressed a sag,
        it has deleted the video.
    """

    mode: str = "report_only"

    # -- cold open ---------------------------------------------------------
    cold_open: bool = True
    min_cold_open_seconds: float = 5.0
    max_cold_open_seconds: float = 20.0
    #: What happens to the footage the cold open was lifted from.
    duplicate_policy: str = "remove"
    #: Let the same footage appear twice. Off: obvious duplication is the
    #: fastest way to make a cut look automated.
    allow_duplicate_footage: bool = False
    #: A hook this far into the episode is a spoiler rather than a tease.
    #: 0.9 means the last tenth is off limits.
    hook_spoiler_position: float = 0.9
    #: A hook needing more than this much prior context to make sense is not a
    #: hook.
    max_hook_setup_seconds: float = 6.0
    #: A hook must score at least this to be used at all.
    min_hook_score: float = 0.35

    # -- sag compression ---------------------------------------------------
    compress_sag: bool = True
    #: Playback rate applied when a sag is sped up rather than cut.
    grind_speed: float = 2.0
    #: Seconds of a compressed stretch kept so the story still follows.
    keep_context_seconds: float = 2.0
    #: Risks below this severity are marked, never acted on.
    min_risk_severity: str = "medium"
    #: Ceiling on how much of the base cut compression may remove, 0..1.
    max_compression_share: float = 0.5

    # -- protection --------------------------------------------------------
    protect_setups: bool = True
    protect_payoffs: bool = True
    protect_callbacks: bool = True
    #: A payoff may not be shortened below this share of itself.
    min_payoff_share: float = 0.8

    # -- dead air ----------------------------------------------------------
    kill_dead_air: bool = True
    dead_air_aggressiveness: str = "medium"
    #: Overrides ``ORDINARY_SILENCE`` when set above zero.
    max_ordinary_silence: float = 0.0
    #: Silence that serves comedy or tension may run this long.
    max_purposeful_silence: float = 2.5

    # -- limits ------------------------------------------------------------
    target_duration: float = 0.0
    max_duration: float = 0.0
    #: A decision below this confidence is recorded and never acted on.
    min_confidence: float = 0.55
    #: The style preset in force, so its taste can shift the defaults.
    style: str = ""

    @classmethod
    def from_env(cls) -> "RetentionCutConfig":
        return cls(
            mode=_env("EDITING_RETENTION_MODE", "report_only"),
            cold_open=_env_bool("EDITING_RETENTION_COLD_OPEN", True),
            max_cold_open_seconds=_env_float(
                "EDITING_MAX_COLD_OPEN_SECONDS", 20.0),
            dead_air_aggressiveness=_env(
                "EDITING_DEAD_AIR_AGGRESSIVENESS", "medium"),
            compress_sag=_env_bool("EDITING_RETENTION_COMPRESS", True),
        )

    def validated(self) -> "RetentionCutConfig":
        """Clamp to values the compiler can honour. Never raises."""
        from dataclasses import replace

        minimum = max(0.5, as_float(self.min_cold_open_seconds, 5.0))
        return replace(
            self,
            mode=coerce_one(self.mode, MODES, "report_only"),
            min_cold_open_seconds=minimum,
            max_cold_open_seconds=max(
                minimum, as_float(self.max_cold_open_seconds, 20.0)),
            duplicate_policy=coerce_one(
                self.duplicate_policy, DUPLICATE_POLICIES, "remove"),
            hook_spoiler_position=clamp01(self.hook_spoiler_position, 0.9),
            max_hook_setup_seconds=max(
                0.0, as_float(self.max_hook_setup_seconds, 6.0)),
            min_hook_score=clamp01(self.min_hook_score, 0.35),
            grind_speed=max(1.0, min(as_float(self.grind_speed, 2.0), 8.0)),
            keep_context_seconds=max(
                0.0, as_float(self.keep_context_seconds, 2.0)),
            min_risk_severity=coerce_one(
                self.min_risk_severity, ("low", "medium", "high"), "medium"),
            max_compression_share=clamp01(self.max_compression_share, 0.5),
            min_payoff_share=clamp01(self.min_payoff_share, 0.8),
            dead_air_aggressiveness=coerce_one(
                self.dead_air_aggressiveness, AGGRESSIVENESS, "medium"),
            max_ordinary_silence=max(
                0.0, as_float(self.max_ordinary_silence)),
            max_purposeful_silence=max(
                0.0, as_float(self.max_purposeful_silence, 2.5)),
            target_duration=max(0.0, as_float(self.target_duration)),
            max_duration=max(0.0, as_float(self.max_duration)),
            min_confidence=clamp01(self.min_confidence, 0.55),
            style=_slug(self.style),
        )

    # -- derived -----------------------------------------------------------

    @property
    def acts(self) -> bool:
        """Whether this configuration may change a frame."""
        return self.mode in ACTING_MODES

    @property
    def ordinary_silence_limit(self) -> float:
        """Seconds of purposeless silence tolerated."""
        if self.max_ordinary_silence > 0:
            return self.max_ordinary_silence
        return ORDINARY_SILENCE.get(self.dead_air_aggressiveness, 1.2)

    @property
    def prefers_director(self) -> bool:
        return self.mode in ("director_retention", "hybrid")

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.mode == "off":
            out.append(
                "mode is 'off', so the retention findings change nothing. "
                "Use --mode report_only to see what they would do."
            )
        if self.mode == "report_only":
            out.append(
                "mode is 'report_only': every decision below was made and "
                "none of them was applied. The cut is unchanged."
            )
        if self.allow_duplicate_footage:
            out.append(
                "duplicate footage is allowed, so a cold open lifted from "
                "later can appear twice. That reads as a teaser when it is "
                "deliberate and as a mistake when it is not."
            )
        if self.dead_air_aggressiveness == "high":
            out.append(
                f"dead air is being cut past {self.ordinary_silence_limit:.1f}s, "
                "which is tight. Breathing room between sentences is not dead "
                "air, and over-cutting it makes speech sound clipped."
            )
        if self.max_compression_share >= 0.8:
            out.append(
                f"compression may remove up to "
                f"{self.max_compression_share:.0%} of the cut, which is not "
                "compressing a sag so much as deleting the video."
            )
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ordinary_silence_limit"] = self.ordinary_silence_limit
        data["acts"] = self.acts
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RetentionCutConfig":
        data = data or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# One range, resolved
# ---------------------------------------------------------------------------

@dataclass
class SourceSpan:
    """A stretch of one real source file.

    What every retention decision resolves to. Episode time is where a finding
    *says* it is; this is where the footage actually lives, and only this can
    be acted on.
    """

    asset_id: str = ""
    source_file: str = ""
    start: float = 0.0
    end: float = 0.0
    segment_ids: list[str] = field(default_factory=list)
    placement_ids: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def overlaps(self, other: "SourceSpan") -> float:
        if self.asset_id != other.asset_id:
            return 0.0
        return max(0.0, min(self.end, other.end) - max(self.start, other.start))

    def covers(self, asset_id: str, start: float, end: float) -> float:
        if self.asset_id != asset_id:
            return 0.0
        return max(0.0, min(self.end, end) - max(self.start, start))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        data["duration"] = round(self.duration, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SourceSpan":
        data = data or {}
        start = max(0.0, as_float(data.get("start")))
        return cls(
            asset_id=_text(data.get("asset_id"), 120),
            source_file=_text(data.get("source_file"), 500),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            segment_ids=as_str_list(data.get("segment_ids"), limit=80),
            placement_ids=as_str_list(data.get("placement_ids"), limit=80),
        )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

@dataclass
class RetentionCutDecision:
    """One retention finding, turned into something that could change the cut.

    ``accepted`` is set by the deterministic validation pass and by nothing
    else, the same way a ``DirectorDecision`` works -- a finding is a request
    until a rule agrees with it.
    """

    decision_id: str = ""
    action: str = "marker_only"
    source_type: str = "none"
    #: The episode-layer record this came from.
    source_id: str = ""

    #: Where the finding said it was, in episode time.
    episode_start: float = 0.0
    episode_end: float = 0.0
    #: Where that actually is, in source footage. Empty when unresolvable.
    spans: list[SourceSpan] = field(default_factory=list)

    speed: float = 1.0
    confidence: float = 0.0
    priority: float = 0.5
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    viewer_effect: str = "none_stated"

    accepted: bool = False
    rejected_reason: str = ""
    #: The named rule that refused it, from ``REJECT_REASONS``.
    reject_code: str = ""
    #: What the validation pass changed, in the order it changed it.
    modifications: list[str] = field(default_factory=list)
    safety_notes: list[str] = field(default_factory=list)
    schema_version: int = 1

    # -- derived -----------------------------------------------------------

    @property
    def episode_duration(self) -> float:
        return max(0.0, self.episode_end - self.episode_start)

    @property
    def source_seconds(self) -> float:
        return round(sum(span.duration for span in self.spans), 3)

    @property
    def is_resolved(self) -> bool:
        return bool(self.spans)

    @property
    def changes_footage(self) -> bool:
        return self.action in CHANGING_ACTIONS

    @property
    def protects(self) -> bool:
        return self.action in PROTECTING_ACTIONS

    @property
    def modified(self) -> bool:
        return bool(self.modifications)

    def line(self) -> str:
        mark = "+" if self.accepted else "x"
        if self.modified and self.accepted:
            mark = "~"
        window = f"{self.episode_start:.0f}-{self.episode_end:.0f}s"
        tail = self.reason if self.accepted else (
            self.rejected_reason or self.reason)
        return (f"{mark} {self.action:<18} {self.source_type:<18} "
                f"{window:<14} {self.confidence:.2f}  {tail[:56]}")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "action": self.action,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "episode_start": round(self.episode_start, 3),
            "episode_end": round(self.episode_end, 3),
            "episode_duration": round(self.episode_duration, 3),
            "spans": [span.to_dict() for span in self.spans],
            "source_seconds": self.source_seconds,
            "speed": round(self.speed, 4),
            "confidence": round(self.confidence, 3),
            "priority": round(self.priority, 3),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "viewer_effect": self.viewer_effect,
            "accepted": self.accepted,
            "rejected_reason": self.rejected_reason,
            "reject_code": self.reject_code,
            "modifications": list(self.modifications),
            "safety_notes": list(self.safety_notes),
            "is_resolved": self.is_resolved,
            "changes_footage": self.changes_footage,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RetentionCutDecision":
        data = data or {}
        start = max(0.0, as_float(data.get("episode_start")))
        return cls(
            decision_id=_text(data.get("decision_id"), 60),
            action=coerce_one(data.get("action"), ACTIONS, "marker_only"),
            source_type=coerce_one(
                data.get("source_type"), SOURCE_TYPES, "none"),
            source_id=_text(data.get("source_id"), 80),
            episode_start=start,
            episode_end=max(start, as_float(data.get("episode_end"), start)),
            spans=[SourceSpan.from_dict(item)
                   for item in _dicts(data.get("spans"))],
            speed=max(0.05, as_float(data.get("speed"), 1.0)),
            confidence=clamp01(data.get("confidence"), 0.0),
            priority=clamp01(data.get("priority"), 0.5),
            reason=_text(data.get("reason"), 600),
            evidence=as_str_list(data.get("evidence"), limit=40),
            viewer_effect=coerce_one(
                data.get("viewer_effect"), VIEWER_EFFECTS, "none_stated"),
            accepted=as_bool(data.get("accepted")),
            rejected_reason=_text(data.get("rejected_reason"), 600),
            reject_code=coerce_one(
                data.get("reject_code"), REJECT_REASONS, "unknown")
            if data.get("reject_code") else "",
            modifications=as_str_list(data.get("modifications"), limit=20),
            safety_notes=as_str_list(data.get("safety_notes"), limit=20),
        )


def decision_id_for(action: str, source_id: str, start: float) -> str:
    return "r_" + short_hash(
        _slug(action), str(source_id), f"{float(start):.3f}", length=8)


# ---------------------------------------------------------------------------
# The four kinds of plan
# ---------------------------------------------------------------------------

@dataclass
class ColdOpenPlan:
    """The opening, and everything that had to be decided to choose it.

    ``fallback_reason`` is filled when there is no cold open, and it is the
    most-read field in this record: "why does my episode still open on
    walking" needs an answer that is a rule rather than a shrug.
    """

    chosen: bool = False
    hook_id: str = ""
    hook_type: str = "unknown"
    score: float = 0.0
    confidence: float = 0.0

    #: Where it was in the episode.
    original_start: float = 0.0
    original_end: float = 0.0
    #: The source footage it maps to.
    spans: list[SourceSpan] = field(default_factory=list)
    #: How long the opening runs.
    duration: float = 0.0

    viewer_question: str = ""
    suggested_text: str = ""
    text_source: str = "none"
    payoff_at: Optional[float] = None
    payoff_id: str = ""

    #: What happened to the original occurrence: remove / shorten / keep.
    duplicate_policy: str = "remove"
    original_removed: bool = False
    original_shortened_to: float = 0.0

    #: Hooks that were considered and passed over, with the rule that did it.
    rejected: list[dict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fallback_reason: str = ""

    @property
    def duplicates_footage(self) -> bool:
        return self.chosen and not self.original_removed \
            and self.duplicate_policy == "keep"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["spans"] = [span.to_dict() for span in self.spans]
        data["duplicates_footage"] = self.duplicates_footage
        data["original_start"] = round(self.original_start, 3)
        data["original_end"] = round(self.original_end, 3)
        data["duration"] = round(self.duration, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ColdOpenPlan":
        data = data or {}
        payoff = data.get("payoff_at")
        return cls(
            chosen=as_bool(data.get("chosen")),
            hook_id=_text(data.get("hook_id"), 80),
            hook_type=_text(data.get("hook_type"), 40) or "unknown",
            score=clamp01(data.get("score"), 0.0),
            confidence=clamp01(data.get("confidence"), 0.0),
            original_start=as_float(data.get("original_start")),
            original_end=as_float(data.get("original_end")),
            spans=[SourceSpan.from_dict(item)
                   for item in _dicts(data.get("spans"))],
            duration=as_float(data.get("duration")),
            viewer_question=_text(data.get("viewer_question"), 400),
            suggested_text=_text(data.get("suggested_text"), 400),
            text_source=_text(data.get("text_source"), 40) or "none",
            payoff_at=(as_float(payoff) if payoff is not None else None),
            payoff_id=_text(data.get("payoff_id"), 80),
            duplicate_policy=coerce_one(
                data.get("duplicate_policy"), DUPLICATE_POLICIES, "remove"),
            original_removed=as_bool(data.get("original_removed")),
            original_shortened_to=as_float(data.get("original_shortened_to")),
            rejected=_dicts(data.get("rejected")),
            risks=as_str_list(data.get("risks"), limit=20),
            warnings=as_str_list(data.get("warnings"), limit=20),
            fallback_reason=_text(data.get("fallback_reason"), 600),
        )


@dataclass
class SagCompressionPlan:
    """Every stretch the retention planner called weak, and what was done.

    ``seconds_removed`` counts what actually left the cut, not what was
    proposed -- the two differ whenever protection refused something, and the
    proposed number would flatter the pass.
    """

    zones: list[dict] = field(default_factory=list)
    decisions: list[RetentionCutDecision] = field(default_factory=list)
    seconds_removed: float = 0.0
    seconds_sped_up: float = 0.0
    zones_compressed: int = 0
    zones_marked_only: int = 0
    zones_refused: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "zones": list(self.zones),
            "decisions": [item.to_dict() for item in self.decisions],
            "seconds_removed": round(self.seconds_removed, 2),
            "seconds_sped_up": round(self.seconds_sped_up, 2),
            "zones_compressed": self.zones_compressed,
            "zones_marked_only": self.zones_marked_only,
            "zones_refused": self.zones_refused,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SagCompressionPlan":
        data = data or {}
        return cls(
            zones=_dicts(data.get("zones")),
            decisions=[RetentionCutDecision.from_dict(item)
                       for item in _dicts(data.get("decisions"))],
            seconds_removed=as_float(data.get("seconds_removed")),
            seconds_sped_up=as_float(data.get("seconds_sped_up")),
            zones_compressed=int(as_float(data.get("zones_compressed"))),
            zones_marked_only=int(as_float(data.get("zones_marked_only"))),
            zones_refused=int(as_float(data.get("zones_refused"))),
            warnings=as_str_list(data.get("warnings"), limit=40),
        )


@dataclass
class SetupProtectionDecision:
    """A setup kept because something later needs it.

    ``payoff_kept`` is the whole justification. A setup whose payoff is not in
    the cut is not protected -- it is a stretch of footage with no reason to be
    there, and saying so is more useful than defending it.
    """

    setup_id: str = ""
    payoff_id: str = ""
    payoff_kept: bool = False
    protected: bool = False
    episode_start: float = 0.0
    episode_end: float = 0.0
    spans: list[SourceSpan] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    warning: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["spans"] = [span.to_dict() for span in self.spans]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SetupProtectionDecision":
        data = data or {}
        return cls(
            setup_id=_text(data.get("setup_id"), 80),
            payoff_id=_text(data.get("payoff_id"), 80),
            payoff_kept=as_bool(data.get("payoff_kept")),
            protected=as_bool(data.get("protected")),
            episode_start=as_float(data.get("episode_start")),
            episode_end=as_float(data.get("episode_end")),
            spans=[SourceSpan.from_dict(item)
                   for item in _dicts(data.get("spans"))],
            confidence=clamp01(data.get("confidence"), 0.0),
            reason=_text(data.get("reason"), 600),
            warning=_text(data.get("warning"), 600),
        )


@dataclass
class PayoffProtectionDecision:
    """A payoff kept whole, and whether its setup survived with it."""

    payoff_id: str = ""
    setup_id: str = ""
    setup_kept: bool = False
    protected: bool = False
    is_climax: bool = False
    episode_start: float = 0.0
    episode_end: float = 0.0
    spans: list[SourceSpan] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    warning: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["spans"] = [span.to_dict() for span in self.spans]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PayoffProtectionDecision":
        data = data or {}
        return cls(
            payoff_id=_text(data.get("payoff_id"), 80),
            setup_id=_text(data.get("setup_id"), 80),
            setup_kept=as_bool(data.get("setup_kept")),
            protected=as_bool(data.get("protected")),
            is_climax=as_bool(data.get("is_climax")),
            episode_start=as_float(data.get("episode_start")),
            episode_end=as_float(data.get("episode_end")),
            spans=[SourceSpan.from_dict(item)
                   for item in _dicts(data.get("spans"))],
            confidence=clamp01(data.get("confidence"), 0.0),
            reason=_text(data.get("reason"), 600),
            warning=_text(data.get("warning"), 600),
        )


@dataclass
class DeadAirDecision:
    """One stretch of silence, and whether it was doing a job.

    ``purpose`` is the field that decides everything. Silence covering a
    reaction, a reveal or the beat after a death is timing; silence covering
    nothing is dead air. Cutting the first is how an edit stops being funny.
    """

    decision_id: str = ""
    episode_start: float = 0.0
    episode_end: float = 0.0
    spans: list[SourceSpan] = field(default_factory=list)
    #: One of ``PURPOSEFUL_SILENCE``, or empty when it serves nothing.
    purpose: str = ""
    action: str = "cut"
    seconds_removed: float = 0.0
    #: What was left in place, when the silence was trimmed rather than cut.
    seconds_kept: float = 0.0
    accepted: bool = False
    reason: str = ""
    rejected_reason: str = ""
    confidence: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.episode_end - self.episode_start)

    @property
    def is_purposeful(self) -> bool:
        return bool(self.purpose)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["spans"] = [span.to_dict() for span in self.spans]
        data["duration"] = round(self.duration, 3)
        data["is_purposeful"] = self.is_purposeful
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DeadAirDecision":
        data = data or {}
        return cls(
            decision_id=_text(data.get("decision_id"), 60),
            episode_start=as_float(data.get("episode_start")),
            episode_end=as_float(data.get("episode_end")),
            spans=[SourceSpan.from_dict(item)
                   for item in _dicts(data.get("spans"))],
            purpose=_text(data.get("purpose"), 40),
            action=coerce_one(data.get("action"), ACTIONS, "cut"),
            seconds_removed=as_float(data.get("seconds_removed")),
            seconds_kept=as_float(data.get("seconds_kept")),
            accepted=as_bool(data.get("accepted")),
            reason=_text(data.get("reason"), 600),
            rejected_reason=_text(data.get("rejected_reason"), 600),
            confidence=clamp01(data.get("confidence"), 0.0),
        )


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------

@dataclass
class RetentionCutFailure:
    """Why a retention pass produced nothing, and what to do about it."""

    stage: str = "unknown"
    code: str = "retention_failed"
    message: str = ""
    hint: str = ""
    recoverable: bool = True
    detail: dict = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"{self.stage}: {self.message}"]
        if self.hint:
            lines.append(f"  fix : {self.hint}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["RetentionCutFailure"]:
        if not data:
            return None
        return cls(
            stage=coerce_one(data.get("stage"), FAILURE_STAGES, "unknown"),
            code=_text(data.get("code"), 60) or "retention_failed",
            message=_text(data.get("message"), 1000),
            hint=_text(data.get("hint"), 1000),
            recoverable=as_bool(data.get("recoverable"), True),
            detail=dict(data.get("detail") or {}),
        )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass
class RetentionCutPlan:
    """Every retention decision, and the cut that follows from them.

    Contains no Premiere operation and touches nothing. ``convert`` turns the
    accepted decisions into ranges, and Session 3's builder -- with every guard
    it has always had -- turns those into a cut.
    """

    name: str = "structure"
    mode: str = "report_only"
    config: RetentionCutConfig = field(default_factory=RetentionCutConfig)

    #: Which cut this was applied on top of: heuristic / director.
    base: str = "heuristic"
    #: The timebase the episode findings were in. Recorded because acting on
    #: the wrong one puts the cold open in the wrong place.
    timebase: str = "empty"

    cold_open: ColdOpenPlan = field(default_factory=ColdOpenPlan)
    sag: SagCompressionPlan = field(default_factory=SagCompressionPlan)
    setups: list[SetupProtectionDecision] = field(default_factory=list)
    payoffs: list[PayoffProtectionDecision] = field(default_factory=list)
    dead_air: list[DeadAirDecision] = field(default_factory=list)
    decisions: list[RetentionCutDecision] = field(default_factory=list)

    #: What the base cut was, and what this one is.
    base_duration: float = 0.0
    cut_duration: float = 0.0
    base_ranges: int = 0
    cut_ranges: int = 0

    sources: dict = field(default_factory=dict)
    generated_at: str = ""
    warnings: list[str] = field(default_factory=list)
    failure: Optional[RetentionCutFailure] = None
    not_measured: str = NOT_MEASURED
    schema_version: int = 1

    # -- derived -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self.decisions)

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def applied(self) -> bool:
        """Whether this plan changed any frame of the cut."""
        return self.mode in ACTING_MODES and bool(self.accepted)

    @property
    def accepted(self) -> list[RetentionCutDecision]:
        return [item for item in self.decisions if item.accepted]

    @property
    def rejected(self) -> list[RetentionCutDecision]:
        return [item for item in self.decisions if not item.accepted]

    @property
    def protected_spans(self) -> list[SourceSpan]:
        """Every span something claimed and nothing may touch."""
        out: list[SourceSpan] = []
        for setup in self.setups:
            if setup.protected:
                out.extend(setup.spans)
        for payoff in self.payoffs:
            if payoff.protected:
                out.extend(payoff.spans)
        for decision in self.accepted:
            if decision.protects:
                out.extend(decision.spans)
        return out

    @property
    def seconds_removed(self) -> float:
        return round(
            self.sag.seconds_removed
            + sum(item.seconds_removed for item in self.dead_air
                  if item.accepted),
            2,
        )

    @property
    def unresolved_warnings(self) -> list[str]:
        """Setups without payoffs, payoffs without setups, and duplicates."""
        out: list[str] = []
        for setup in self.setups:
            if setup.warning:
                out.append(setup.warning)
        for payoff in self.payoffs:
            if payoff.warning:
                out.append(payoff.warning)
        if self.cold_open.duplicates_footage:
            out.append(
                "The cold open leaves the same footage in twice. That reads "
                "as a teaser when deliberate and a mistake when not."
            )
        return out

    def of_action(self, *actions: str) -> list[RetentionCutDecision]:
        wanted = set(actions)
        return [item for item in self.decisions if item.action in wanted]

    def of_source(self, *kinds: str) -> list[RetentionCutDecision]:
        wanted = set(kinds)
        return [item for item in self.decisions if item.source_type in wanted]

    def stats(self) -> dict:
        by_action: dict = {}
        by_source: dict = {}
        by_reject: dict = {}
        for item in self.decisions:
            by_action[item.action] = by_action.get(item.action, 0) + 1
            by_source[item.source_type] = by_source.get(item.source_type, 0) + 1
            if not item.accepted and item.reject_code:
                by_reject[item.reject_code] = \
                    by_reject.get(item.reject_code, 0) + 1
        return {
            "decisions": len(self.decisions),
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "by_action": by_action,
            "by_source": by_source,
            "by_reject_code": by_reject,
            "cold_open": self.cold_open.chosen,
            "cold_open_seconds": round(self.cold_open.duration, 2),
            "zones_compressed": self.sag.zones_compressed,
            "zones_marked_only": self.sag.zones_marked_only,
            "seconds_removed": self.seconds_removed,
            "seconds_sped_up": round(self.sag.seconds_sped_up, 2),
            "setups_protected": sum(1 for s in self.setups if s.protected),
            "payoffs_protected": sum(1 for p in self.payoffs if p.protected),
            "dead_air_cut": sum(1 for d in self.dead_air if d.accepted),
            "dead_air_kept": sum(
                1 for d in self.dead_air if not d.accepted),
            "base_duration": round(self.base_duration, 2),
            "cut_duration": round(self.cut_duration, 2),
            "unresolved_warnings": len(self.unresolved_warnings),
            "applied": self.applied,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "mode": self.mode,
            "base": self.base,
            "timebase": self.timebase,
            "generated_at": self.generated_at,
            "config": self.config.to_dict(),
            "sources": dict(self.sources),
            "stats": self.stats(),
            "cold_open": self.cold_open.to_dict(),
            "sag": self.sag.to_dict(),
            "setups": [item.to_dict() for item in self.setups],
            "payoffs": [item.to_dict() for item in self.payoffs],
            "dead_air": [item.to_dict() for item in self.dead_air],
            "decisions": [item.to_dict() for item in self.decisions],
            "base_duration": round(self.base_duration, 3),
            "cut_duration": round(self.cut_duration, 3),
            "base_ranges": self.base_ranges,
            "cut_ranges": self.cut_ranges,
            "unresolved_warnings": list(self.unresolved_warnings),
            "warnings": list(self.warnings),
            "failure": self.failure.to_dict() if self.failure else None,
            "not_measured": self.not_measured,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RetentionCutPlan":
        data = data or {}
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            mode=coerce_one(data.get("mode"), MODES, "report_only"),
            config=RetentionCutConfig.from_dict(data.get("config")),
            base=_text(data.get("base"), 40) or "heuristic",
            timebase=_text(data.get("timebase"), 40) or "empty",
            cold_open=ColdOpenPlan.from_dict(data.get("cold_open")),
            sag=SagCompressionPlan.from_dict(data.get("sag")),
            setups=[SetupProtectionDecision.from_dict(item)
                    for item in _dicts(data.get("setups"))],
            payoffs=[PayoffProtectionDecision.from_dict(item)
                     for item in _dicts(data.get("payoffs"))],
            dead_air=[DeadAirDecision.from_dict(item)
                      for item in _dicts(data.get("dead_air"))],
            decisions=[RetentionCutDecision.from_dict(item)
                       for item in _dicts(data.get("decisions"))],
            base_duration=as_float(data.get("base_duration")),
            cut_duration=as_float(data.get("cut_duration")),
            base_ranges=int(as_float(data.get("base_ranges"))),
            cut_ranges=int(as_float(data.get("cut_ranges"))),
            sources=dict(data.get("sources") or {}),
            generated_at=_text(data.get("generated_at"), 40),
            warnings=as_str_list(data.get("warnings"), limit=100),
            failure=RetentionCutFailure.from_dict(data.get("failure")),
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass
class RetentionCutReport:
    """The readable summary of a retention pass."""

    name: str = ""
    mode: str = ""
    base: str = ""
    applied: bool = False
    stats: dict = field(default_factory=dict)
    cold_open: dict = field(default_factory=dict)
    compression: dict = field(default_factory=dict)
    protection: dict = field(default_factory=dict)
    dead_air: dict = field(default_factory=dict)
    rejected: list[dict] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    next_commands: list[str] = field(default_factory=list)
    failure: Optional[dict] = None
    not_measured: str = NOT_MEASURED
    generated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetentionCutComparison:
    """The retention cut measured against the cut it was built from.

    Counts only. There is deliberately no score, no grade and no percentage
    that could be read as an audience prediction -- see ``NOT_MEASURED``.
    """

    name: str = ""
    mode: str = ""
    base: str = ""
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    difference: dict = field(default_factory=dict)
    cold_open: dict = field(default_factory=dict)
    changes: list[dict] = field(default_factory=list)
    protected: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    duplicated_footage: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    not_measured: str = NOT_MEASURED
    generated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RetentionCutComparison":
        data = data or {}
        return cls(
            name=_text(data.get("name"), 120),
            mode=_text(data.get("mode"), 40),
            base=_text(data.get("base"), 40),
            before=dict(data.get("before") or {}),
            after=dict(data.get("after") or {}),
            difference=dict(data.get("difference") or {}),
            cold_open=dict(data.get("cold_open") or {}),
            changes=_dicts(data.get("changes")),
            protected=_dicts(data.get("protected")),
            rejected=_dicts(data.get("rejected")),
            unresolved=as_str_list(data.get("unresolved"), limit=60),
            duplicated_footage=_dicts(data.get("duplicated_footage")),
            notes=as_str_list(data.get("notes"), limit=60),
            generated_at=_text(data.get("generated_at"), 40),
        )
