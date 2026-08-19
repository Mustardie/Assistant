"""What a rough cut is, as data.

The central record is ``ClipPlacement``: one source range, placed at one
position on the scratch sequence, carrying the recommendation IDs that put it
there. That chain — **source file → source range → recommendation → sequence
position → Premiere operation** — is the thing this session exists to preserve.
Without it a rough cut is an opaque artefact; with it, every frame on the
timeline can be traced back to the evidence that justified keeping it.

Two invariants the schema enforces:

* **Sequence layout is computed, not observed.** ``sequence_start`` and
  ``sequence_end`` are derived from the ranges and their speed factors before
  anything touches Premiere, so markers and effects can be planned offline and
  the whole plan is dry-runnable.
* **Execution is never implicit.** ``ExecutionReport.executed`` defaults to
  False and ``RoughCutPlan`` carries the dry-run result it must pass before
  anything runs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from editing.schema import as_float, as_str_list, clamp01, short_hash

#: Why a source range was kept. Ordered loosely by strength.
KEEP_REASONS = (
    "payoff", "reveal", "danger", "funny", "tension", "audio_reaction",
    "contrast", "setup", "hold", "filler", "unknown",
)

#: What happened to a recommendation during conversion.
CONVERSION_STATES = ("converted", "unconverted", "skipped")


@dataclass
class ClipPlacement:
    """One source range on the scratch sequence.

    ``speed`` is the playback rate applied to this clip (1.0 = untouched, 2.0 =
    double speed). It is what makes ``sequence_duration`` differ from
    ``source_duration``, and every later position in the sequence depends on
    it — which is why it lives on the placement rather than in a side table.
    """

    placement_id: str
    asset_id: str
    source_file: str
    source_in: float
    source_out: float
    sequence_start: float
    #: Track this lands on. V1 for the rough assembly.
    track: str = "V1"
    #: 0-based position within the track, in append order.
    index: int = 0
    speed: float = 1.0
    keep_reason: str = "unknown"
    #: Recommendation IDs that caused this range to be kept, in priority order.
    recommendation_ids: list[str] = field(default_factory=list)
    #: Timeline segment IDs this range covers.
    segment_ids: list[str] = field(default_factory=list)
    #: True when a hold said "leave this alone" -- no effects may be applied.
    protected: bool = False
    notes: str = ""

    @property
    def source_duration(self) -> float:
        return max(0.0, self.source_out - self.source_in)

    @property
    def sequence_duration(self) -> float:
        """How long this occupies the timeline, after any speed change."""
        rate = self.speed if self.speed > 0 else 1.0
        return self.source_duration / rate

    @property
    def sequence_end(self) -> float:
        return self.sequence_start + self.sequence_duration

    @property
    def sequence_midpoint(self) -> float:
        """Used to target this clip by time rather than by index.

        Index-based selectors drift if anything earlier is split or removed;
        a midpoint stays correct as long as the clip itself is where the plan
        says it is.
        """
        return self.sequence_start + self.sequence_duration / 2.0

    def source_to_sequence(self, source_time: float) -> Optional[float]:
        """Map a time in the source file to its position on the sequence.

        Returns None when ``source_time`` falls outside this placement's range
        -- the caller usually wants to try the next placement rather than
        receive a clamped answer that would silently put a marker in the wrong
        place.
        """
        if not (self.source_in <= source_time <= self.source_out):
            return None
        rate = self.speed if self.speed > 0 else 1.0
        return self.sequence_start + (source_time - self.source_in) / rate

    def selector(self) -> dict:
        """The Premiere clip selector for this placement."""
        return {"track": self.track, "at": round(self.sequence_midpoint, 3)}

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update({
            "source_duration": round(self.source_duration, 3),
            "sequence_duration": round(self.sequence_duration, 3),
            "sequence_end": round(self.sequence_end, 3),
            "sequence_midpoint": round(self.sequence_midpoint, 3),
            "source_in": round(self.source_in, 3),
            "source_out": round(self.source_out, 3),
            "sequence_start": round(self.sequence_start, 3),
            "selector": self.selector(),
        })
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ClipPlacement":
        source_in = max(0.0, as_float(data.get("source_in")))
        source_out = max(source_in, as_float(data.get("source_out"), source_in))
        return cls(
            placement_id=str(data.get("placement_id") or ""),
            asset_id=str(data.get("asset_id") or ""),
            source_file=str(data.get("source_file") or ""),
            source_in=source_in,
            source_out=source_out,
            sequence_start=max(0.0, as_float(data.get("sequence_start"))),
            track=str(data.get("track") or "V1"),
            index=int(as_float(data.get("index"))),
            speed=max(0.05, as_float(data.get("speed"), 1.0)),
            keep_reason=str(data.get("keep_reason") or "unknown"),
            recommendation_ids=as_str_list(data.get("recommendation_ids"), limit=50),
            segment_ids=as_str_list(data.get("segment_ids"), limit=50),
            protected=bool(data.get("protected")),
            notes=str(data.get("notes") or "")[:500],
        )


@dataclass
class SequenceMarker:
    """A marker planned at a computed sequence position."""

    time: float
    name: str
    comment: str = ""
    kind: str = "comment"
    duration: float = 0.0
    #: What this marker came from, for tracing it back.
    recommendation_id: str = ""
    category: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["time"] = round(self.time, 3)
        data["duration"] = round(self.duration, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SequenceMarker":
        return cls(
            time=max(0.0, as_float(data.get("time"))),
            name=str(data.get("name") or "")[:64],
            comment=str(data.get("comment") or "")[:500],
            kind=str(data.get("kind") or "comment"),
            duration=max(0.0, as_float(data.get("duration"))),
            recommendation_id=str(data.get("recommendation_id") or ""),
            category=str(data.get("category") or ""),
        )


@dataclass
class Unconverted:
    """A recommendation that did not become an operation, and why.

    Kept as a first-class record rather than a log line: "what could this not
    do" is one of the questions the session brief asks the system to answer.
    """

    recommendation_id: str
    category: str
    start: float
    end: float
    reason: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Unconverted":
        return cls(
            recommendation_id=str(data.get("recommendation_id") or ""),
            category=str(data.get("category") or ""),
            start=as_float(data.get("start")),
            end=as_float(data.get("end")),
            reason=str(data.get("reason") or ""),
        )


@dataclass
class RoughCutPlan:
    """A complete rough cut, ready to validate and (only then) run."""

    sequence_name: str = "Nova Rough Cut"
    placements: list[ClipPlacement] = field(default_factory=list)
    markers: list[SequenceMarker] = field(default_factory=list)
    ops: list[dict] = field(default_factory=list)
    unconverted: list[Unconverted] = field(default_factory=list)
    #: Source paths that must exist in the project before assembly.
    source_paths: list[str] = field(default_factory=list)
    generated_at: str = ""
    #: Set by the dry run; ``execute`` refuses to run while this is False.
    dry_run_passed: bool = False
    dry_run_error: Optional[dict] = None
    explanation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Whether the plan targets a scratch sequence (the safe default).
    on_scratch: bool = True
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.placements)

    @property
    def total_duration(self) -> float:
        """Runtime of the assembled cut."""
        return max((p.sequence_end for p in self.placements), default=0.0)

    @property
    def source_duration(self) -> float:
        """How much source footage the cut draws on."""
        return sum(p.source_duration for p in self.placements)

    @property
    def operation_count(self) -> int:
        return len(self.ops)

    def placement_at(self, sequence_time: float) -> Optional[ClipPlacement]:
        for placement in self.placements:
            if placement.sequence_start <= sequence_time < placement.sequence_end:
                return placement
        return None

    def as_edit_plan(self, *, dry_run: bool = True) -> dict:
        """The plan in the shape ``premiere.validator`` and the engine expect."""
        plan: dict = {
            "ops": list(self.ops),
            "on_error": "abort",
            "label": f"editing-brain-v1 rough cut: {self.sequence_name}",
        }
        if dry_run:
            plan["dry_run"] = True
        return plan

    def stats(self) -> dict:
        by_reason: dict = {}
        for placement in self.placements:
            by_reason[placement.keep_reason] = (
                by_reason.get(placement.keep_reason, 0) + 1
            )
        by_unconverted: dict = {}
        for entry in self.unconverted:
            by_unconverted[entry.category] = by_unconverted.get(entry.category, 0) + 1
        return {
            "placements": len(self.placements),
            "markers": len(self.markers),
            "operations": len(self.ops),
            "unconverted": len(self.unconverted),
            "protected": sum(1 for p in self.placements if p.protected),
            "sped_up": sum(1 for p in self.placements if p.speed != 1.0),
            "cut_duration": round(self.total_duration, 2),
            "source_duration": round(self.source_duration, 2),
            "by_keep_reason": by_reason,
            "unconverted_by_category": by_unconverted,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "sequence_name": self.sequence_name,
            "on_scratch": self.on_scratch,
            "dry_run_passed": self.dry_run_passed,
            "dry_run_error": self.dry_run_error,
            "explanation": list(self.explanation),
            "warnings": list(self.warnings),
            "stats": self.stats(),
            "source_paths": list(self.source_paths),
            "placements": [p.to_dict() for p in self.placements],
            "markers": [m.to_dict() for m in self.markers],
            "unconverted": [u.to_dict() for u in self.unconverted],
            "plan": self.as_edit_plan(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RoughCutPlan":
        return cls(
            sequence_name=str(data.get("sequence_name") or "Nova Rough Cut"),
            placements=[
                ClipPlacement.from_dict(p) for p in (data.get("placements") or [])
            ],
            markers=[
                SequenceMarker.from_dict(m) for m in (data.get("markers") or [])
            ],
            ops=[
                dict(op) for op in ((data.get("plan") or {}).get("ops") or [])
                if isinstance(op, dict)
            ],
            unconverted=[
                Unconverted.from_dict(u) for u in (data.get("unconverted") or [])
            ],
            source_paths=as_str_list(data.get("source_paths"), limit=500),
            generated_at=str(data.get("generated_at") or ""),
            dry_run_passed=bool(data.get("dry_run_passed")),
            dry_run_error=data.get("dry_run_error"),
            explanation=as_str_list(data.get("explanation"), limit=500),
            warnings=as_str_list(data.get("warnings"), limit=200),
            on_scratch=bool(data.get("on_scratch", True)),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )


@dataclass
class ExecutionReport:
    """What actually happened when a plan was run.

    ``executed`` is False on every path that did not genuinely run operations
    against Premiere, including a refused execution -- so a consumer can trust
    this one field rather than inferring from the absence of errors.
    """

    mode: str = "plan_only"
    executed: bool = False
    sequence_name: str = ""
    on_scratch: bool = True
    operations_attempted: int = 0
    operations_succeeded: int = 0
    dry_run_passed: bool = False
    error: Optional[dict] = None
    results: list[dict] = field(default_factory=list)
    refused_reason: str = ""
    started_at: str = ""
    elapsed: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.executed and self.error is None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ok"] = self.ok
        data["elapsed"] = round(self.elapsed, 2)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionReport":
        return cls(
            mode=str(data.get("mode") or "plan_only"),
            executed=bool(data.get("executed")),
            sequence_name=str(data.get("sequence_name") or ""),
            on_scratch=bool(data.get("on_scratch", True)),
            operations_attempted=int(as_float(data.get("operations_attempted"))),
            operations_succeeded=int(as_float(data.get("operations_succeeded"))),
            dry_run_passed=bool(data.get("dry_run_passed")),
            error=data.get("error"),
            results=[dict(r) for r in (data.get("results") or []) if isinstance(r, dict)],
            refused_reason=str(data.get("refused_reason") or ""),
            started_at=str(data.get("started_at") or ""),
            elapsed=as_float(data.get("elapsed")),
            warnings=as_str_list(data.get("warnings"), limit=200),
        )


def placement_id_for(asset_id: str, source_in: float, source_out: float) -> str:
    return "p_" + short_hash(asset_id, round(source_in, 3), round(source_out, 3))
