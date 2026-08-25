"""What a visual plan could become, in each of the two places it could go.

Two output paths exist and they have completely different shapes, so they get
completely different objects rather than one with a mode flag:

* :class:`PremiereVisualOperationPlan` — a list of catalog operations,
  validated offline, ready to be *inspected* and then, separately and
  explicitly, executed. Effects the catalog cannot express are listed as
  unsupported with the reason, never quietly dropped.
* :class:`FFmpegVisualPreviewPlan` — a capability statement. The proxy renderer
  encodes each segment and joins them with the concat demuxer; burning an
  overlay in would mean a second full re-encode with a filtergraph, which is a
  different render strategy and not one this session builds. So what this
  produces is: which effects *could* be burned in and with what filter, which
  could not and why, and a sidecar marker file that a person can read while
  watching the proxy.

:class:`VisualExecutionPlan` holds both, and :class:`FinalEditPlan` holds
everything — the cut, the captions, the sound and the visuals — as one object
somebody can read end to end.

The invariant across all of it: ``burned_in`` is False everywhere and there is
no code path that sets it True. Nothing in this system draws a frame.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from editing.schema import as_float, as_str_list, as_text_list
from editing.visuals.schema import (
    COMPOSER_MODES, NOT_MEASURED, NOT_RENDERED, PREVIEW_NOTE, VisualLayerPlan,
    _dicts, _text, coerce_one, now,
)

#: How well FFmpeg could show one effect in a preview render.
#:
#: ``burn_in``   a documented filter exists and would be clean
#: ``sidecar``   representable only as a marker or a note beside the video
#: ``none``      not representable at all, in any form FFmpeg has
PREVIEW_SUPPORT = ("burn_in", "sidecar", "none")


@dataclass
class PremiereVisualOperation:
    """One catalog operation, and the treatment that asked for it."""

    treatment_id: str = ""
    effect: str = ""
    #: The operation, in the shape ``premiere.validator`` expects.
    op: dict = field(default_factory=dict)
    #: What this operation is for, in plain English.
    note: str = ""

    @property
    def name(self) -> str:
        return str(self.op.get("op") or "")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["name"] = self.name
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PremiereVisualOperation":
        data = data or {}
        return cls(
            treatment_id=_text(data.get("treatment_id"), 80),
            effect=_text(data.get("effect"), 60),
            op=dict(data.get("op") or {}),
            note=_text(data.get("note"), 400),
        )


@dataclass
class UnsupportedTreatment:
    """A treatment that survived every rule and still cannot be executed.

    Kept in the plan rather than dropped. "This effect is a good idea and
    nothing here can do it" is a useful sentence; silence is not.
    """

    treatment_id: str = ""
    effect: str = ""
    start: float = 0.0
    reason: str = ""
    alternative: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "UnsupportedTreatment":
        data = data or {}
        return cls(
            treatment_id=_text(data.get("treatment_id"), 80),
            effect=_text(data.get("effect"), 60),
            start=as_float(data.get("start")),
            reason=_text(data.get("reason"), 400),
            alternative=_text(data.get("alternative"), 400),
        )


@dataclass
class PremiereVisualOperationPlan:
    """Every visual treatment, as operations Premiere could run.

    Nothing here has been executed. The plan is built offline, validated
    against the catalog offline, and is inspectable before anybody types a
    ``--yes`` — which happens elsewhere, one gate at a time, exactly as every
    other executable plan in this system works.
    """

    name: str = "structure"
    sequence_name: str = ""
    #: The track visual overlays land on. Above the rough cut's V1, so the
    #: whole pass can be removed by deleting one track.
    track: str = "V3"
    operations: list[PremiereVisualOperation] = field(default_factory=list)
    unsupported: list[UnsupportedTreatment] = field(default_factory=list)
    #: Set by an offline validation pass. Execution refuses while this is
    #: False, the same way every other plan in this system works.
    dry_run_passed: bool = False
    dry_run_error: Optional[dict] = None
    warnings: list[str] = field(default_factory=list)
    generated_at: str = ""
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.operations)

    @property
    def operation_count(self) -> int:
        return len(self.operations)

    def by_op(self) -> dict:
        out: dict = {}
        for entry in self.operations:
            out[entry.name] = out.get(entry.name, 0) + 1
        return out

    def as_edit_plan(self, *, dry_run: bool = True) -> dict:
        """The plan in the shape ``premiere.validator`` and the engine expect."""
        plan: dict = {
            "ops": [dict(entry.op) for entry in self.operations],
            "on_error": "abort",
            "label": f"editing-brain-v1 visual layer: {self.sequence_name}",
        }
        if dry_run:
            plan["dry_run"] = True
        return plan

    def stats(self) -> dict:
        return {
            "operations": len(self.operations),
            "treatments": len({e.treatment_id for e in self.operations}),
            "unsupported": len(self.unsupported),
            "by_op": self.by_op(),
            "dry_run_passed": self.dry_run_passed,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "sequence_name": self.sequence_name,
            "track": self.track,
            "generated_at": self.generated_at,
            "stats": self.stats(),
            "dry_run_passed": self.dry_run_passed,
            "dry_run_error": self.dry_run_error,
            "not_rendered": NOT_RENDERED,
            "warnings": list(self.warnings),
            "operations": [entry.to_dict() for entry in self.operations],
            "unsupported": [entry.to_dict() for entry in self.unsupported],
            "plan": self.as_edit_plan(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PremiereVisualOperationPlan":
        data = data or {}
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            sequence_name=_text(data.get("sequence_name"), 200),
            track=_text(data.get("track"), 20) or "V3",
            operations=[PremiereVisualOperation.from_dict(item)
                        for item in _dicts(data.get("operations"))],
            unsupported=[UnsupportedTreatment.from_dict(item)
                         for item in _dicts(data.get("unsupported"))],
            dry_run_passed=bool(data.get("dry_run_passed")),
            dry_run_error=data.get("dry_run_error"),
            warnings=as_text_list(data.get("warnings"), limit=60),
            generated_at=_text(data.get("generated_at"), 40),
        )


@dataclass
class PreviewItem:
    """One treatment, and what FFmpeg could do about it."""

    treatment_id: str = ""
    effect: str = ""
    start: float = 0.0
    end: float = 0.0
    support: str = "none"
    #: The filter fragment that *would* burn this in, when one exists. Recorded
    #: so a later session can wire the preview render without re-deriving it,
    #: and so a reader can see exactly what was and was not claimed.
    filter_fragment: str = ""
    #: Why it is not burnable, when it is not.
    reason: str = ""
    #: The line this treatment contributes to the sidecar marker file.
    marker_text: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PreviewItem":
        data = data or {}
        start = as_float(data.get("start"))
        return cls(
            treatment_id=_text(data.get("treatment_id"), 80),
            effect=_text(data.get("effect"), 60),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            support=coerce_one(data.get("support"), PREVIEW_SUPPORT, "none"),
            filter_fragment=_text(data.get("filter_fragment"), 600),
            reason=_text(data.get("reason"), 400),
            marker_text=_text(data.get("marker_text"), 300),
        )


@dataclass
class FFmpegVisualPreviewPlan:
    """What a proxy render could and could not show of this visual plan.

    ``burned_in`` is False and there is no code path that sets it True. The
    renderer this system has encodes each segment and joins them with the
    concat demuxer; overlaying anything would mean a second full re-encode with
    a filtergraph, which is a different strategy with its own failure modes and
    is not built. Saying so is the whole point of this object.
    """

    name: str = "structure"
    items: list[PreviewItem] = field(default_factory=list)
    #: Where the marker file was written, when one was.
    sidecar_path: str = ""
    #: Always False. See the class docstring.
    burned_in: bool = False
    #: Why not, in one sentence, so a reader never has to go and find out.
    burn_in_note: str = PREVIEW_NOTE
    warnings: list[str] = field(default_factory=list)
    generated_at: str = ""
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.items)

    def of_support(self, *kinds: str) -> list[PreviewItem]:
        wanted = set(kinds)
        return [item for item in self.items if item.support in wanted]

    @property
    def burnable(self) -> list[PreviewItem]:
        """Items a preview render *could* show, if one were built."""
        return self.of_support("burn_in")

    @property
    def sidecar_only(self) -> list[PreviewItem]:
        return self.of_support("sidecar")

    @property
    def invisible(self) -> list[PreviewItem]:
        """Items FFmpeg has no way to represent at all, in any form."""
        return self.of_support("none")

    def stats(self) -> dict:
        by_support: dict = {}
        for item in self.items:
            by_support[item.support] = by_support.get(item.support, 0) + 1
        return {
            "items": len(self.items),
            "burnable": len(self.burnable),
            "sidecar_only": len(self.sidecar_only),
            "invisible": len(self.invisible),
            "burned_in": False,
            "by_support": by_support,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "generated_at": self.generated_at,
            "stats": self.stats(),
            "sidecar_path": self.sidecar_path,
            "burned_in": False,
            "burn_in_note": self.burn_in_note,
            "warnings": list(self.warnings),
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FFmpegVisualPreviewPlan":
        data = data or {}
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            items=[PreviewItem.from_dict(item)
                   for item in _dicts(data.get("items"))],
            sidecar_path=_text(data.get("sidecar_path"), 500),
            # Never read from the document: a plan claiming a burned-in effect
            # would be a plan this package could not have written.
            burned_in=False,
            burn_in_note=_text(data.get("burn_in_note"), 600) or PREVIEW_NOTE,
            warnings=as_text_list(data.get("warnings"), limit=60),
            generated_at=_text(data.get("generated_at"), 40),
        )


@dataclass
class VisualExecutionPlan:
    """Both output paths, and which of them this run asked for."""

    mode: str = "plan_only"
    premiere: Optional[PremiereVisualOperationPlan] = None
    preview: Optional[FFmpegVisualPreviewPlan] = None
    #: Treatments that reach neither path: a note for a person and nothing more.
    placeholder_only: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def executes_anything(self) -> bool:
        """Always False. Kept as a property so a reader can grep for it."""
        return False

    def stats(self) -> dict:
        return {
            "mode": self.mode,
            "premiere_operations": (
                self.premiere.operation_count if self.premiere else 0),
            "premiere_unsupported": (
                len(self.premiere.unsupported) if self.premiere else 0),
            "preview_items": len(self.preview) if self.preview else 0,
            "preview_burnable": (
                len(self.preview.burnable) if self.preview else 0),
            "placeholder_only": len(self.placeholder_only),
            "executed": False,
        }

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "stats": self.stats(),
            "executed": False,
            "premiere": self.premiere.to_dict() if self.premiere else None,
            "preview": self.preview.to_dict() if self.preview else None,
            "placeholder_only": list(self.placeholder_only),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VisualExecutionPlan":
        data = data or {}
        premiere = data.get("premiere")
        preview = data.get("preview")
        return cls(
            mode=coerce_one(data.get("mode"), COMPOSER_MODES, "plan_only"),
            premiere=(PremiereVisualOperationPlan.from_dict(premiere)
                      if isinstance(premiere, dict) else None),
            preview=(FFmpegVisualPreviewPlan.from_dict(preview)
                     if isinstance(preview, dict) else None),
            placeholder_only=as_str_list(
                data.get("placeholder_only"), limit=200),
            warnings=as_text_list(data.get("warnings"), limit=60),
        )


# ---------------------------------------------------------------------------
# The final edit
# ---------------------------------------------------------------------------

@dataclass
class FinalEditSegment:
    """One clip of the cut, with everything planned on top of it.

    The composer's unit of work. A person reading one of these should be able
    to say what happens in that clip without opening another file.
    """

    index: int = 0
    placement_id: str = ""
    asset_id: str = ""
    source_file: str = ""
    start: float = 0.0
    end: float = 0.0
    source_in: float = 0.0
    source_out: float = 0.0
    speed: float = 1.0
    keep_reason: str = ""
    protected: bool = False

    #: Treatment ids landing on this clip.
    treatments: list[str] = field(default_factory=list)
    #: Caption ids landing on this clip.
    captions: list[str] = field(default_factory=list)
    #: Audio cue ids landing on this clip.
    audio_cues: list[str] = field(default_factory=list)
    #: What a person should know about this clip specifically.
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_busy(self) -> bool:
        """Whether this clip carries more than one thing at once.

        Not a refusal -- the safety pass has already run -- but the thing a
        person should look at first when a plan feels over-edited.
        """
        return len(self.treatments) + len(self.captions) > 2

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration"] = round(self.duration, 3)
        data["is_busy"] = self.is_busy
        for key in ("start", "end", "source_in", "source_out"):
            data[key] = round(getattr(self, key), 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "FinalEditSegment":
        data = data or {}
        start = as_float(data.get("start"))
        return cls(
            index=int(as_float(data.get("index"))),
            placement_id=_text(data.get("placement_id"), 120),
            asset_id=_text(data.get("asset_id"), 120),
            source_file=_text(data.get("source_file"), 500),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            source_in=as_float(data.get("source_in")),
            source_out=as_float(data.get("source_out")),
            speed=as_float(data.get("speed"), 1.0) or 1.0,
            keep_reason=_text(data.get("keep_reason"), 120),
            protected=bool(data.get("protected")),
            treatments=as_str_list(data.get("treatments"), limit=60),
            captions=as_str_list(data.get("captions"), limit=60),
            audio_cues=as_str_list(data.get("audio_cues"), limit=60),
            notes=as_text_list(data.get("notes"), limit=20),
        )


@dataclass
class FinalEditPlan:
    """The whole edit, as one object: cut, captions, sound and visuals.

    Assembled rather than decided. Every part of it was chosen by the pass that
    owns that decision; this exists so there is one file a person can read to
    find out what the edit *is*, instead of five files and a mental model of
    how they relate.
    """

    name: str = "structure"
    mode: str = "plan_only"
    style: str = ""
    sequence_name: str = ""
    run_id: str = ""
    #: Which cut this was composed from: ``retention`` or ``roughcut``.
    base: str = "roughcut"
    duration: float = 0.0

    segments: list[FinalEditSegment] = field(default_factory=list)
    visuals: VisualLayerPlan = field(default_factory=VisualLayerPlan)
    execution: VisualExecutionPlan = field(default_factory=VisualExecutionPlan)

    #: Counts lifted from the caption and audio plans, so this object is
    #: readable on its own without loading either.
    caption_summary: dict = field(default_factory=dict)
    audio_summary: dict = field(default_factory=dict)

    warnings: list[str] = field(default_factory=list)
    #: What a person has to look at before trusting any of this.
    manual_checks: list[str] = field(default_factory=list)
    generated_at: str = ""
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def busy_segments(self) -> list[FinalEditSegment]:
        return [segment for segment in self.segments if segment.is_busy]

    @property
    def untouched_segments(self) -> list[FinalEditSegment]:
        return [
            segment for segment in self.segments
            if not segment.treatments and not segment.captions
        ]

    def stats(self) -> dict:
        visual_stats = self.visuals.stats()
        return {
            "segments": len(self.segments),
            "duration": round(self.duration, 2),
            "busy_segments": len(self.busy_segments),
            "untouched_segments": len(self.untouched_segments),
            "visual_treatments": visual_stats["accepted"],
            "visual_rejected": visual_stats["rejected"],
            "effects_per_minute": visual_stats["effects_per_minute"],
            "captions": int(self.caption_summary.get("accepted") or 0),
            "audio_cues": int(self.audio_summary.get("accepted") or 0),
            **{f"execution_{k}": v for k, v in self.execution.stats().items()},
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "mode": self.mode,
            "style": self.style,
            "run_id": self.run_id,
            "base": self.base,
            "sequence_name": self.sequence_name,
            "generated_at": self.generated_at,
            "stats": self.stats(),
            "not_rendered": NOT_RENDERED,
            "not_measured": NOT_MEASURED,
            "warnings": list(self.warnings),
            "manual_checks": list(self.manual_checks),
            "segments": [segment.to_dict() for segment in self.segments],
            "visuals": self.visuals.to_dict(),
            "execution": self.execution.to_dict(),
            "captions": dict(self.caption_summary),
            "audio": dict(self.audio_summary),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FinalEditPlan":
        data = data or {}
        stats = data.get("stats") or {}
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            mode=coerce_one(data.get("mode"), COMPOSER_MODES, "plan_only"),
            style=_text(data.get("style"), 80),
            sequence_name=_text(data.get("sequence_name"), 200),
            run_id=_text(data.get("run_id"), 120),
            base=_text(data.get("base"), 40) or "roughcut",
            duration=as_float(stats.get("duration")),
            segments=[FinalEditSegment.from_dict(item)
                      for item in _dicts(data.get("segments"))],
            visuals=VisualLayerPlan.from_dict(data.get("visuals") or {}),
            execution=VisualExecutionPlan.from_dict(
                data.get("execution") or {}),
            caption_summary=dict(data.get("captions") or {}),
            audio_summary=dict(data.get("audio") or {}),
            warnings=as_text_list(data.get("warnings"), limit=80),
            manual_checks=as_text_list(data.get("manual_checks"), limit=40),
            generated_at=_text(data.get("generated_at"), 40),
        )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@dataclass
class VisualReport:
    """The structured summary of a visual plan.

    Separate from the rendered text so the review package and a JSON consumer
    read the same numbers the report prints, rather than one of them
    re-deriving them and drifting.
    """

    name: str = "structure"
    layer: str = "off"
    style: str = ""
    stats: dict = field(default_factory=dict)
    #: The six questions the review index has to answer, each with its answer.
    answers: list[dict] = field(default_factory=list)
    #: Effects that would be worth a second look before shipping.
    overdone_risks: list[str] = field(default_factory=list)
    #: What only a person can settle.
    manual_checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["not_rendered"] = NOT_RENDERED
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "VisualReport":
        data = data or {}
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            layer=_text(data.get("layer"), 20) or "off",
            style=_text(data.get("style"), 80),
            stats=dict(data.get("stats") or {}),
            answers=[dict(item) for item in _dicts(data.get("answers"))],
            overdone_risks=as_text_list(
                data.get("overdone_risks"), limit=40),
            manual_checks=as_text_list(data.get("manual_checks"), limit=40),
            warnings=as_text_list(data.get("warnings"), limit=60),
            generated_at=_text(data.get("generated_at"), 40),
        )


@dataclass
class VisualComparisonReport:
    """The cut with the visual layer, against the cut without it.

    Counts only. There is deliberately no score and no percentage that could
    be read as a claim that the edit got better -- the same stance the
    retention comparison takes, for the same reason: a number that goes up is
    exactly what somebody would trust without checking.
    """

    name: str = "structure"
    layer: str = "off"
    cut_duration: float = 0.0
    segments: int = 0
    #: Segments that carry at least one treatment.
    segments_touched: int = 0
    segments_untouched: int = 0
    segments_busy: int = 0
    treatments: int = 0
    rejected: int = 0
    effects_per_minute: float = 0.0
    callouts_per_minute: float = 0.0
    #: Moments found and left alone, with the reason each was left alone.
    untreated: list[dict] = field(default_factory=list)
    by_family: dict = field(default_factory=dict)
    by_reject_reason: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    generated_at: str = ""

    def stats(self) -> dict:
        return {
            "segments": self.segments,
            "segments_touched": self.segments_touched,
            "segments_untouched": self.segments_untouched,
            "segments_busy": self.segments_busy,
            "treatments": self.treatments,
            "rejected": self.rejected,
            "effects_per_minute": self.effects_per_minute,
            "callouts_per_minute": self.callouts_per_minute,
            "untreated_moments": len(self.untreated),
        }

    def to_dict(self) -> dict:
        data = asdict(self)
        data["stats"] = self.stats()
        data["not_measured"] = NOT_MEASURED
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "VisualComparisonReport":
        data = data or {}
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in data.items() if k in known}
        clean["untreated"] = _dicts(clean.get("untreated"))
        clean["notes"] = as_text_list(clean.get("notes"), limit=40)
        return cls(**clean)


def build_comparison(plan: VisualLayerPlan,
                     final: Optional[FinalEditPlan] = None
                     ) -> VisualComparisonReport:
    """What the visual layer changed about the cut, as counts."""
    stats = plan.stats()
    report = VisualComparisonReport(
        name=plan.name,
        layer=plan.layer,
        cut_duration=plan.cut_duration,
        treatments=stats["accepted"],
        rejected=stats["rejected"],
        effects_per_minute=stats["effects_per_minute"],
        callouts_per_minute=stats["callouts_per_minute"],
        by_family=dict(stats["by_family"]),
        by_reject_reason=dict(stats["by_reject_reason"]),
        generated_at=now(),
    )

    if final is not None:
        report.segments = len(final.segments)
        report.segments_untouched = len(final.untouched_segments)
        report.segments_touched = sum(
            1 for segment in final.segments if segment.treatments)
        report.segments_busy = len(final.busy_segments)

    # The other half of the report: what was found and deliberately left
    # alone. A visual layer that treated everything it found would be a
    # different, worse feature, and this is the list that proves it did not.
    for moment in plan.untreated_moments():
        refusals = [
            t for t in plan.rejected if t.moment_id == moment.moment_id]
        report.untreated.append({
            "moment_id": moment.moment_id,
            "kind": moment.kind,
            "at": round(moment.start, 2),
            "label": moment.label[:160],
            "why": (refusals[0].reject_detail if refusals
                    else "no treatment in the library suited it"),
            "reject_reason": (refusals[0].reject_reason if refusals
                              else "no_evidence"),
        })

    report.notes.append(NOT_MEASURED)
    report.notes.append(NOT_RENDERED)
    return report
