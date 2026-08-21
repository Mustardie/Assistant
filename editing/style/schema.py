"""What a layered edit is, as data.

A rough cut is one list of clips. A *styled* edit is several independent
passes over that same timeline — captions, emphasis, audio cues, cards — and
the useful property of this schema is that those passes stay separate all the
way to the end.

Why that matters, concretely: if the caption pass turns out to be too dense,
you want to drop the caption layer and keep the rest. If a punch-in lands
badly, you want to see it as a member of the emphasis layer rather than as
operation number 47. So ``LayerItem`` carries its layer, and
``LayeredEditPlan`` reports, filters and validates by layer.

Three invariants:

* **Every item names its own reason.** Not a category, a sentence. A styled
  edit that cannot explain any individual choice is indistinguishable from a
  random one, and this session exists to make that difference visible.
* **Deferring is a status, not a deletion.** ``deferred`` and ``rejected``
  items stay in the plan with their reason, exactly as the Session 2 safety
  pass and the Session 4 critic do.
* **Execution is never implicit.** ``dry_run_passed`` defaults False and
  ``executed`` is written only by the executor.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from editing.schema import _slug, as_float, as_str_list, clamp01, short_hash
from editing.style.presets import (
    ACTIVE_KINDS, CAPTION_KINDS, CARD_KINDS, LAYER_KINDS, TEXT_KINDS,
    ZOOM_KINDS,
)

#: The passes, in the order they are compiled and reported. ``base`` is the
#: rough cut itself -- carried here so a layered plan is a complete description
#: of the timeline rather than a diff against something you have to go and find.
LAYERS = (
    "base",       # the rough cut's own clips
    "marker",     # structure / pacing notes
    "caption",    # text on screen, or the marker standing in for it
    "emphasis",   # picture: punches, pushes, freeze placeholders
    "audio",      # music, SFX, fades, ducking
    "title",      # title and chapter cards
    "polish",     # small finishing touches
    "deferred",   # everything that did not make it, with the reason
)

#: Layers whose items can change what the viewer sees or hears. ``base`` is
#: excluded: those clips are already on the timeline, placed by Session 3.
EDITING_LAYERS = frozenset({"caption", "emphasis", "audio", "title", "polish"})

STATUSES = ("planned", "deferred", "rejected")

#: What an item is meant to do to the viewer. Same vocabulary as Session 2's
#: recommendations, so the chain from recommendation to layer item keeps one
#: language end to end.
EFFECTS = (
    "clarity", "tension", "comedy", "impact", "pacing", "explanation",
    "anticipation", "payoff", "structure", "atmosphere", "unknown",
)

#: What could go wrong with an item. Present on planned items too -- a planned
#: edit with a named risk is still worth flagging to whoever reads the report.
RISKS = (
    "over_editing", "hides_gameplay", "text_unreadable", "text_spam",
    "bad_timing", "unnecessary", "low_confidence", "audio_masking",
    "repetitive", "stacked", "placeholder_only", "not_convertible",
    "style_limited", "critic_flagged",
)


def _coerce_one(value: Any, allowed: Sequence[str], default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


def _coerce_many(value: Any, allowed: Sequence[str]) -> list[str]:
    out: list[str] = []
    for item in as_str_list(value, limit=40):
        token = _slug(item)
        if token in allowed and token not in out:
            out.append(token)
    return out


@dataclass
class LayerEvidence:
    """What the item was built from.

    IDs plus the one human-readable line needed to review it. Same shape as
    ``recommend.Evidence`` on purpose: an item's evidence should read the same
    whether it came from a recommendation, a transcript line or a critic
    finding.
    """

    visual_event_ids: list[str] = field(default_factory=list)
    transcript_quotes: list[str] = field(default_factory=list)
    audio_event_ids: list[str] = field(default_factory=list)
    audio_types: list[str] = field(default_factory=list)
    segment_ids: list[str] = field(default_factory=list)
    #: Critic findings that bear on this moment, when a critique exists.
    critic_finding_ids: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.visual_event_ids or self.transcript_quotes
            or self.audio_event_ids or self.segment_ids
        )

    @property
    def channels(self) -> list[str]:
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
    def from_dict(cls, data: Optional[dict]) -> "LayerEvidence":
        data = data or {}
        return cls(
            visual_event_ids=as_str_list(data.get("visual_event_ids"), limit=50),
            transcript_quotes=[
                str(q)[:300] for q in (data.get("transcript_quotes") or [])
            ][:20],
            audio_event_ids=as_str_list(data.get("audio_event_ids"), limit=50),
            audio_types=as_str_list(data.get("audio_types"), limit=20),
            segment_ids=as_str_list(data.get("segment_ids"), limit=50),
            critic_finding_ids=as_str_list(
                data.get("critic_finding_ids"), limit=50
            ),
            summary=str(data.get("summary") or "")[:500],
        )


@dataclass
class LayerItem:
    """One styled choice, on one layer, at one moment.

    Times are **sequence time** throughout. ``source_start``/``source_end``
    keep the original source position when there was one, so an item can still
    be traced back to the footage after the cut moved it.
    """

    item_id: str
    layer: str = "marker"
    kind: str = "structure_marker"
    #: The Session 2 recommendation behind this, when there was one.
    recommendation_id: str = ""
    #: The rough-cut clip this sits on.
    placement_id: str = ""
    start: float = 0.0
    end: float = 0.0
    source_start: Optional[float] = None
    source_end: Optional[float] = None
    asset_id: str = ""
    #: The preset that produced it, so a plan built from two styles is legible.
    style: str = ""
    reason: str = ""
    evidence: LayerEvidence = field(default_factory=LayerEvidence)
    effect: str = "unknown"
    intensity: str = "low"
    #: 0..1. Ranks items against each other when a density ceiling bites.
    priority: float = 0.5
    risks: list[str] = field(default_factory=list)
    status: str = "planned"
    status_reason: str = ""
    #: Draft Premiere operations. Empty for a marker-only or deferred item.
    premiere_ops: list[dict] = field(default_factory=list)
    #: Kind-specific detail: caption text, zone, scale, placeholder type.
    payload: dict = field(default_factory=dict)
    notes: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_planned(self) -> bool:
        return self.status == "planned"

    @property
    def is_active(self) -> bool:
        """Whether this actually changes the picture or sound.

        Kind alone is not enough. A caption that could not be placed safely,
        or a card the style held back, ends up as a marker -- and a marker
        costs the viewer nothing, so charging it against the edit ceiling
        would make a restrained plan look busy and starve the next real edit.
        A candidate with no operations yet counts as active: it is asking to
        become one.
        """
        return self.kind in ACTIVE_KINDS and not self.is_marker_only

    @property
    def is_text(self) -> bool:
        """Whether this draws words on screen. Cards included."""
        return self.kind in TEXT_KINDS

    @property
    def is_caption(self) -> bool:
        """Whether this counts against the caption ceiling. Cards excluded."""
        return self.kind in CAPTION_KINDS

    @property
    def is_card(self) -> bool:
        return self.kind in CARD_KINDS

    @property
    def is_zoom(self) -> bool:
        return self.kind in ZOOM_KINDS

    @property
    def is_convertible(self) -> bool:
        """Whether it reaches the operation plan at all."""
        return self.status == "planned" and bool(self.premiere_ops)

    @property
    def is_marker_only(self) -> bool:
        """Planned, but realised as a note rather than an edit."""
        return self.status == "planned" and all(
            str(op.get("op")) == "marker.add" for op in self.premiere_ops
        ) and bool(self.premiere_ops)

    def overlaps(self, start: float, end: float) -> float:
        return max(0.0, min(self.end, end) - max(self.start, start))

    def defer(self, reason: str, *, risk: str = "") -> "LayerItem":
        """Keep it, unbuilt, with the reason. Never delete."""
        self.status = "deferred"
        self.status_reason = reason
        self.premiere_ops = []
        if risk and risk not in self.risks:
            self.risks.append(risk)
        return self

    def reject(self, reason: str, *, risk: str = "") -> "LayerItem":
        self.status = "rejected"
        self.status_reason = reason
        self.premiere_ops = []
        if risk and risk not in self.risks:
            self.risks.append(risk)
        return self

    def summary(self) -> str:
        marks = {"planned": "+", "deferred": "?", "rejected": "-"}
        detail = self.payload.get("text") or self.payload.get("placeholder") or ""
        return (
            f"{marks.get(self.status, '?')} [{self.start:8.2f}] "
            f"{self.layer:<9} {self.kind:<18} p={self.priority:.2f}  "
            f"{str(detail)[:34]:<34} {self.reason[:44]}"
        )

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "layer": self.layer,
            "kind": self.kind,
            "recommendation_id": self.recommendation_id,
            "placement_id": self.placement_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "source_start": (
                round(self.source_start, 3) if self.source_start is not None else None
            ),
            "source_end": (
                round(self.source_end, 3) if self.source_end is not None else None
            ),
            "asset_id": self.asset_id,
            "style": self.style,
            "reason": self.reason,
            "evidence": self.evidence.to_dict(),
            "effect": self.effect,
            "intensity": self.intensity,
            "priority": round(self.priority, 3),
            "risks": list(self.risks),
            "status": self.status,
            "status_reason": self.status_reason,
            "premiere_ops": [dict(op) for op in self.premiere_ops],
            "payload": dict(self.payload),
            "notes": self.notes,
            "is_convertible": self.is_convertible,
            "is_marker_only": self.is_marker_only,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LayerItem":
        start = max(0.0, as_float(data.get("start")))
        end = max(start, as_float(data.get("end"), start))
        kind = _coerce_one(data.get("kind"), LAYER_KINDS, "structure_marker")
        source_start = data.get("source_start")
        source_end = data.get("source_end")
        return cls(
            item_id=str(data.get("item_id") or "") or (
                "li_" + short_hash(kind, start, data.get("layer"))
            ),
            layer=_coerce_one(data.get("layer"), LAYERS, "marker"),
            kind=kind,
            recommendation_id=str(data.get("recommendation_id") or ""),
            placement_id=str(data.get("placement_id") or ""),
            start=start,
            end=end,
            source_start=(
                as_float(source_start) if source_start is not None else None
            ),
            source_end=as_float(source_end) if source_end is not None else None,
            asset_id=str(data.get("asset_id") or ""),
            style=str(data.get("style") or ""),
            reason=str(data.get("reason") or "")[:1000],
            evidence=LayerEvidence.from_dict(data.get("evidence")),
            effect=_coerce_one(data.get("effect"), EFFECTS, "unknown"),
            intensity=_coerce_one(
                data.get("intensity"), ("low", "medium", "high"), "low"
            ),
            priority=clamp01(data.get("priority", 0.5), 0.5),
            risks=_coerce_many(data.get("risks"), RISKS),
            status=_coerce_one(data.get("status"), STATUSES, "planned"),
            status_reason=str(data.get("status_reason") or "")[:600],
            premiere_ops=[
                dict(op) for op in (data.get("premiere_ops") or [])
                if isinstance(op, dict)
            ],
            payload=dict(data.get("payload") or {}),
            notes=str(data.get("notes") or "")[:600],
        )


def item_id_for(kind: str, start: float, detail: Any = "") -> str:
    return "li_" + short_hash(kind, round(float(start), 3), detail)


@dataclass
class LayeredEditPlan:
    """Every layer for one sequence, plus the operations they compile to.

    Items are stored flat and grouped on the way out. One list means an item
    can never be in two layers at once, and the per-layer views stay derived
    rather than maintained -- which is what makes ``defer`` safe to call on an
    item you are holding a reference to.
    """

    sequence_name: str = ""
    style: str = ""
    items: list[LayerItem] = field(default_factory=list)
    ops: list[dict] = field(default_factory=list)
    generated_at: str = ""
    dry_run_passed: bool = False
    dry_run_error: Optional[dict] = None
    explanation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Whether the sequence this styles is the rough cut's scratch sequence.
    on_scratch: bool = True
    #: Whether the rough cut it styles was actually built in Premiere.
    roughcut_executed: bool = False
    #: Always False until the executor writes it.
    executed: bool = False
    #: Runtime of the cut being styled, so density is reportable.
    cut_duration: float = 0.0
    #: The preset, inlined, so a saved plan is readable without the code.
    preset: dict = field(default_factory=dict)
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.items)

    @property
    def operation_count(self) -> int:
        return len(self.ops)

    @property
    def minutes(self) -> float:
        return self.cut_duration / 60.0 if self.cut_duration > 0 else 0.0

    def layer(self, name: str) -> list[LayerItem]:
        return [item for item in self.items if item.layer == name]

    def planned(self) -> list[LayerItem]:
        return [item for item in self.items if item.status == "planned"]

    def deferred(self) -> list[LayerItem]:
        return [item for item in self.items if item.status == "deferred"]

    def rejected(self) -> list[LayerItem]:
        return [item for item in self.items if item.status == "rejected"]

    def convertible(self) -> list[LayerItem]:
        return [item for item in self.items if item.is_convertible]

    def of_kind(self, kind: str) -> list[LayerItem]:
        return [item for item in self.items if item.kind == kind]

    def ranked(self) -> list[LayerItem]:
        """Most defensible first, then in time order."""
        return sorted(self.items, key=lambda i: (-i.priority, i.start))

    def density(self) -> dict:
        """Edits per minute, by class and by layer.

        The number a person actually wants when asking "is this over-edited?".
        Markers are counted but reported separately, because a timeline full of
        notes is not an over-edited timeline.
        """
        minutes = self.minutes or 0.0
        planned = self.planned()
        active = [item for item in planned if item.is_active]
        captions = [item for item in planned if item.is_caption]
        cards = [item for item in planned if item.is_card]
        zooms = [item for item in planned if item.is_zoom]
        markers = [item for item in planned if item.is_marker_only]

        def per_minute(count: int) -> float:
            return round(count / minutes, 2) if minutes > 0 else 0.0

        by_layer: dict = {}
        for item in planned:
            by_layer[item.layer] = by_layer.get(item.layer, 0) + 1
        by_kind: dict = {}
        for item in planned:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1

        return {
            "cut_duration": round(self.cut_duration, 2),
            "minutes": round(minutes, 3),
            "planned": len(planned),
            "active_edits": len(active),
            "edits_per_minute": per_minute(len(active)),
            "captions": len(captions),
            "captions_per_minute": per_minute(len(captions)),
            "cards": len(cards),
            "zooms": len(zooms),
            "zooms_per_minute": per_minute(len(zooms)),
            "markers": len(markers),
            "markers_per_minute": per_minute(len(markers)),
            "by_layer": by_layer,
            "by_kind": by_kind,
        }

    def stats(self) -> dict:
        by_status: dict = {}
        by_layer: dict = {}
        for item in self.items:
            by_status[item.status] = by_status.get(item.status, 0) + 1
            by_layer[item.layer] = by_layer.get(item.layer, 0) + 1
        by_op: dict = {}
        for op in self.ops:
            name = str(op.get("op") or "?")
            by_op[name] = by_op.get(name, 0) + 1
        return {
            "items": len(self.items),
            "planned": len(self.planned()),
            "deferred": len(self.deferred()),
            "rejected": len(self.rejected()),
            "convertible": len(self.convertible()),
            "marker_only": sum(1 for i in self.items if i.is_marker_only),
            "operations": len(self.ops),
            "by_status": by_status,
            "by_layer": by_layer,
            "by_operation": by_op,
        }

    def as_edit_plan(self, *, dry_run: bool = True) -> dict:
        """The plan in the shape ``premiere.validator`` and the engine expect."""
        plan: dict = {
            "ops": list(self.ops),
            "on_error": "abort",
            "label": (
                f"editing-brain-v1 layers [{self.style}]: {self.sequence_name}"
            ),
        }
        if dry_run:
            plan["dry_run"] = True
        return plan

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "sequence_name": self.sequence_name,
            "style": self.style,
            "on_scratch": self.on_scratch,
            "roughcut_executed": self.roughcut_executed,
            "executed": self.executed,
            "dry_run_passed": self.dry_run_passed,
            "dry_run_error": self.dry_run_error,
            "explanation": list(self.explanation),
            "warnings": list(self.warnings),
            "stats": self.stats(),
            "density": self.density(),
            "preset": dict(self.preset),
            "layers": {
                name: [item.to_dict() for item in self.layer(name)]
                for name in LAYERS
            },
            "plan": self.as_edit_plan(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LayeredEditPlan":
        items: list[LayerItem] = []
        layers = data.get("layers") or {}
        if isinstance(layers, dict):
            for name in LAYERS:
                for entry in (layers.get(name) or []):
                    item = LayerItem.from_dict(entry)
                    item.layer = name
                    items.append(item)
        for entry in (data.get("items") or []):
            items.append(LayerItem.from_dict(entry))
        density = data.get("density") or {}
        return cls(
            sequence_name=str(data.get("sequence_name") or ""),
            style=str(data.get("style") or ""),
            items=items,
            ops=[
                dict(op) for op in ((data.get("plan") or {}).get("ops") or [])
                if isinstance(op, dict)
            ],
            generated_at=str(data.get("generated_at") or ""),
            dry_run_passed=bool(data.get("dry_run_passed")),
            dry_run_error=data.get("dry_run_error"),
            explanation=as_str_list(data.get("explanation"), limit=500),
            warnings=as_str_list(data.get("warnings"), limit=200),
            on_scratch=bool(data.get("on_scratch", True)),
            roughcut_executed=bool(data.get("roughcut_executed")),
            executed=bool(data.get("executed")),
            cut_duration=as_float(density.get("cut_duration")),
            preset=dict(data.get("preset") or {}),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )
