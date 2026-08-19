"""What an edit recommendation is.

A recommendation is a *proposal with its evidence attached*. That shape is the
whole design: every field that says what to do is paired with a field saying
why, and the why cites the specific visual, transcript and audio records it
came from. An editor (or a later layer) can therefore disagree with any
recommendation by inspecting what it was built on, rather than having to trust
it.

Three properties this schema deliberately enforces:

* **Hold is a real answer.** ``hold`` is a first-class category, not the
  absence of a recommendation. A planner that can only say "cut here" will
  edit everything; one that can say "leave this alone, it is strong raw" is
  the difference between an edit and a mess.
* **Nothing is silently dropped.** The safety pass sets ``status`` to
  ``rejected`` or ``downgraded`` and writes ``status_reason``; it never deletes.
  So the output always shows what was considered and why it did not survive.
* **Evidence is required for anything acted on.** ``has_evidence`` gates the
  Premiere conversion, so a recommendation with nothing behind it cannot
  become an operation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from editing.schema import _slug, as_float, as_str_list, clamp01, short_hash

#: What kind of edit is being proposed.
EDIT_CATEGORIES = (
    "structure_cut",        # a cut point between beats
    "trim_dead_air",        # remove silence / a long pause
    "hold",                 # deliberately leave this alone
    "punch_in",             # a hard scale-up on a moment
    "slow_push_in",         # a gradual scale-up across a beat
    "speed_ramp",           # speed up or slow down
    "freeze_frame",
    "text_overlay",
    "caption_emphasis",
    "marker",               # leave a timeline marker for the human editor
    "music_cue",            # placeholder: music starts/changes here
    "sound_effect",         # placeholder: an impact/comedic sound here
    "ducking",              # placeholder: duck the bed under speech
    "audio_fade",
    "color_adjust",
    "transition",
    "unknown",
)

#: Categories that make a *change* to the picture or sound. Used by the safety
#: pass, which rates density of change rather than density of recommendations:
#: markers and holds are annotations, and ten of them in a row is fine.
ACTIVE_CATEGORIES = frozenset({
    "punch_in", "slow_push_in", "speed_ramp", "freeze_frame", "text_overlay",
    "caption_emphasis", "transition", "color_adjust",
})

#: Categories that are purely advisory -- they never become a Premiere edit
#: without a human, so they are exempt from the over-editing budget.
PASSIVE_CATEGORIES = frozenset({"hold", "marker", "unknown"})

INTENSITIES = ("low", "medium", "high")

#: What the edit is meant to do to the viewer.
VIEWER_EFFECTS = (
    "clarity", "tension", "comedy", "impact", "pacing", "explanation",
    "anticipation", "payoff", "unknown",
)

#: What could go wrong with it. Present on every recommendation, including
#: accepted ones -- an accepted edit with a named risk is still worth flagging
#: to whoever reviews the plan.
RISKS = (
    "over_editing", "hides_gameplay", "text_unreadable", "bad_timing",
    "unnecessary", "low_confidence", "audio_masking", "repetitive",
)

STATUSES = ("accepted", "rejected", "downgraded", "hold")

#: Ordering used when a recommendation is downgraded a step.
_INTENSITY_ORDER = ("low", "medium", "high")


def _coerce_one(value: Any, allowed: Sequence[str], default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


def _coerce_many(value: Any, allowed: Sequence[str]) -> list[str]:
    out = []
    for item in as_str_list(value):
        token = _slug(item)
        if token in allowed and token not in out:
            out.append(token)
    return out


@dataclass
class Evidence:
    """The records a recommendation was built from.

    IDs rather than copies, so a recommendation file stays small and cannot
    drift out of sync with the timeline it describes. ``summary`` carries the
    one human-readable line needed to review it without cross-referencing.
    """

    visual_event_ids: list[str] = field(default_factory=list)
    transcript_quotes: list[str] = field(default_factory=list)
    audio_event_ids: list[str] = field(default_factory=list)
    #: Denormalised for readability: the audio types behind this proposal.
    audio_types: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.visual_event_ids or self.transcript_quotes or self.audio_event_ids
        )

    @property
    def channels(self) -> list[str]:
        """Which of the three channels contributed. Used by the safety pass."""
        present = []
        if self.visual_event_ids:
            present.append("visual")
        if self.transcript_quotes:
            present.append("transcript")
        if self.audio_event_ids:
            present.append("audio")
        return present

    def to_dict(self) -> dict:
        data = asdict(self)
        data["channels"] = self.channels
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Evidence":
        data = data or {}
        return cls(
            visual_event_ids=as_str_list(data.get("visual_event_ids"), limit=50),
            transcript_quotes=[
                str(quote)[:300] for quote in (data.get("transcript_quotes") or [])
            ][:20],
            audio_event_ids=as_str_list(data.get("audio_event_ids"), limit=50),
            audio_types=as_str_list(data.get("audio_types"), limit=20),
            summary=str(data.get("summary") or "")[:500],
        )


@dataclass
class EditRecommendation:
    """One proposed edit, with everything needed to judge it."""

    recommendation_id: str
    asset_id: str
    source_file: str
    start: float
    end: float
    category: str = "unknown"
    #: 0..1. Ranks recommendations against each other, not confidence.
    priority: float = 0.5
    reason: str = ""
    evidence: Evidence = field(default_factory=Evidence)
    intensity: str = "low"
    effects: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    status: str = "accepted"
    status_reason: str = ""
    #: Which layer proposed it, for debugging the planner.
    layer: str = ""
    #: A draft Premiere operation batch, when the category converts to one.
    premiere_ops: list[dict] = field(default_factory=list)
    notes: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def has_evidence(self) -> bool:
        return not self.evidence.is_empty

    @property
    def is_active(self) -> bool:
        """Whether this changes the picture or sound (vs. annotating it)."""
        return self.category in ACTIVE_CATEGORIES

    @property
    def is_actionable(self) -> bool:
        """Whether it should reach the Premiere plan at all."""
        return self.status == "accepted" and self.has_evidence

    @property
    def was_softened(self) -> bool:
        """Whether the safety pass acted on this.

        A ``hold`` needs care: it is either a deliberate "leave this alone"
        from the pacing layer, or an active edit the safety pass pushed all the
        way down. ``status_reason`` is what separates them -- only the safety
        pass writes it -- and conflating the two would make the report claim
        the planner chose restraint when it was actually overruled.
        """
        if self.status in ("rejected", "downgraded"):
            return True
        return self.status == "hold" and bool(self.status_reason)

    @property
    def is_deliberate_hold(self) -> bool:
        return self.category == "hold" and not self.status_reason

    def overlaps(self, start: float, end: float) -> float:
        return max(0.0, min(self.end, end) - max(self.start, start))

    def downgrade(self, reason: str) -> "EditRecommendation":
        """Soften rather than remove.

        A too-strong edit at the right moment is usually a right instinct with
        a wrong dial, so the safety pass steps the intensity down and records
        why. Once it can go no lower, the recommendation is held instead --
        never silently deleted.
        """
        index = _INTENSITY_ORDER.index(self.intensity) if (
            self.intensity in _INTENSITY_ORDER
        ) else 0
        if index > 0:
            self.intensity = _INTENSITY_ORDER[index - 1]
            self.status = "downgraded"
        else:
            self.status = "hold"
            self.category = "hold"
            self.premiere_ops = []
        self.status_reason = reason
        self.priority = max(0.0, self.priority - 0.15)
        return self

    def reject(self, reason: str) -> "EditRecommendation":
        """Mark as rejected, keeping it visible in the output."""
        self.status = "rejected"
        self.status_reason = reason
        self.premiere_ops = []
        return self

    def summary(self) -> str:
        """One line, for the report and the CLI."""
        marks = {"accepted": "+", "downgraded": "~", "rejected": "-", "hold": "="}
        return (
            f"{marks.get(self.status, '?')} [{self.start:7.2f}-{self.end:7.2f}] "
            f"{self.category:<16} {self.intensity:<6} p={self.priority:.2f}  "
            f"{self.reason[:70]}"
        )

    def to_dict(self) -> dict:
        return {
            "recommendation_id": self.recommendation_id,
            "asset_id": self.asset_id,
            "source_file": self.source_file,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "category": self.category,
            "priority": round(self.priority, 3),
            "reason": self.reason,
            "evidence": self.evidence.to_dict(),
            "intensity": self.intensity,
            "effects": list(self.effects),
            "risks": list(self.risks),
            "status": self.status,
            "status_reason": self.status_reason,
            "layer": self.layer,
            "premiere_ops": [dict(op) for op in self.premiere_ops],
            "notes": self.notes,
            "has_evidence": self.has_evidence,
            "is_actionable": self.is_actionable,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EditRecommendation":
        start = max(0.0, as_float(data.get("start")))
        end = max(start, as_float(data.get("end"), start))
        return cls(
            recommendation_id=str(data.get("recommendation_id") or short_hash(
                data.get("asset_id"), start, end, data.get("category"))),
            asset_id=str(data.get("asset_id") or ""),
            source_file=str(data.get("source_file") or ""),
            start=start,
            end=end,
            category=_coerce_one(data.get("category"), EDIT_CATEGORIES, "unknown"),
            priority=clamp01(data.get("priority", 0.5), 0.5),
            reason=str(data.get("reason") or "")[:1000],
            evidence=Evidence.from_dict(data.get("evidence")),
            intensity=_coerce_one(data.get("intensity"), INTENSITIES, "low"),
            effects=_coerce_many(data.get("effects"), VIEWER_EFFECTS),
            risks=_coerce_many(data.get("risks"), RISKS),
            status=_coerce_one(data.get("status"), STATUSES, "accepted"),
            status_reason=str(data.get("status_reason") or "")[:500],
            layer=str(data.get("layer") or ""),
            premiere_ops=[
                dict(op) for op in (data.get("premiere_ops") or [])
                if isinstance(op, dict)
            ],
            notes=str(data.get("notes") or "")[:1000],
        )


@dataclass
class RecommendationSet:
    """Every recommendation for a run, plus how it was produced."""

    recommendations: list[EditRecommendation] = field(default_factory=list)
    generated_at: str = ""
    style: str = "cinematic_minecraft"
    #: Per-layer counts, so an odd result can be traced to the layer that made it.
    layer_counts: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.recommendations)

    def accepted(self) -> list[EditRecommendation]:
        return [r for r in self.recommendations if r.status == "accepted"]

    def actionable(self) -> list[EditRecommendation]:
        return [r for r in self.recommendations if r.is_actionable]

    def by_status(self, status: str) -> list[EditRecommendation]:
        return [r for r in self.recommendations if r.status == status]

    def removed(self) -> list[EditRecommendation]:
        """Everything the safety pass acted on, including forced holds."""
        return [r for r in self.recommendations if r.was_softened]

    def deliberate_holds(self) -> list[EditRecommendation]:
        """Moments the planner chose to leave alone, not ones it was forced to."""
        return [r for r in self.recommendations if r.is_deliberate_hold]

    def top(self, limit: int = 20) -> list[EditRecommendation]:
        ranked = sorted(
            self.accepted(), key=lambda r: r.priority, reverse=True
        )
        return ranked[:limit]

    def for_asset(self, asset_id: str) -> list[EditRecommendation]:
        return [r for r in self.recommendations if r.asset_id == asset_id]

    def stats(self) -> dict:
        by_status: dict = {}
        by_category: dict = {}
        by_effect: dict = {}
        for entry in self.recommendations:
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
            by_category[entry.category] = by_category.get(entry.category, 0) + 1
            for effect in entry.effects:
                by_effect[effect] = by_effect.get(effect, 0) + 1
        return {
            "total": len(self.recommendations),
            "accepted": len(self.accepted()),
            "actionable": len(self.actionable()),
            "by_status": by_status,
            "by_category": by_category,
            "by_effect": by_effect,
            "layers": dict(self.layer_counts),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "style": self.style,
            "stats": self.stats(),
            "warnings": list(self.warnings),
            "recommendations": [r.to_dict() for r in self.recommendations],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RecommendationSet":
        return cls(
            recommendations=[
                EditRecommendation.from_dict(entry)
                for entry in (data.get("recommendations") or [])
            ],
            generated_at=str(data.get("generated_at") or ""),
            style=str(data.get("style") or "cinematic_minecraft"),
            layer_counts=dict((data.get("stats") or {}).get("layers") or {}),
            warnings=as_str_list(data.get("warnings"), limit=200),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )
