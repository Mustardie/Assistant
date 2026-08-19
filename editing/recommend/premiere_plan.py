"""Turning accepted recommendations into a draft Premiere plan.

**Nothing here executes anything.** It builds an EditPlan in the existing
``premiere.catalog`` vocabulary, validates it through ``premiere.validator``,
and writes both to disk. Execution is a separate decision, made by a person,
in a later session.

The honest constraint that shapes this module: recommendations are in **source
file time**, and most Premiere operations act on **clips already on a
sequence**. Until the footage is assembled, a punch-in has no clip to apply
itself to. So rather than emit operations that would fail (or worse, silently
apply to the wrong clip), the converter handles what genuinely converts today
and reports the rest as ``not_convertible`` with the reason.

What converts now:

* ``marker`` and ``structure_cut`` -> ``marker.add`` on the **project item**,
  which the catalog supports via its ``asset`` parameter and which works in
  source time with no sequence at all. A markered project item is genuinely
  useful: the human editor opens the clip and every beat this pipeline found is
  already flagged.
* ``hold`` -> no operations, deliberately. A hold means "do nothing here", so
  zero ops is the correct output, not a failure.

Everything else is kept as a recommendation and reported. That is the brief's
"if a recommendation cannot be converted yet, keep it as a recommendation
without lying".

Validation runs at 30 fps offline. ``premiere.engine`` uses the same default
for dry runs precisely so a plan can be checked without Premiere open, which is
most of the value of having a validator at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from editing.recommend.schema import EditRecommendation, RecommendationSet

#: Frame rate used for offline validation. Matches ``premiere.engine``'s dry-run
#: default so a plan validated here behaves identically there.
DRY_RUN_FPS = 30.0

#: Categories that become real operations today.
CONVERTIBLE = frozenset({"marker", "structure_cut"})

#: Categories that correctly produce no operations at all.
NO_OP = frozenset({"hold"})

#: Why each remaining category cannot be converted yet. Stated per category so
#: the report explains the gap rather than waving at it.
_NOT_YET = {
    "trim_dead_air": "needs the footage on a sequence to know which clip to trim",
    "punch_in": "needs a clip on a sequence to animate Motion > Scale on",
    "slow_push_in": "needs a clip on a sequence to animate Motion > Scale on",
    "speed_ramp": "needs a clip on a sequence to retime",
    "freeze_frame": "needs a clip on a sequence to freeze",
    "text_overlay": "needs a sequence and a chosen style before text is placed",
    "caption_emphasis": "needs a sequence and a caption track",
    "music_cue": "placeholder only -- no track has been chosen",
    "sound_effect": "placeholder only -- no sound library is wired up",
    "ducking": "needs the music and speech clips on a sequence",
    "audio_fade": "needs the clip on a sequence to write level keyframes",
    "color_adjust": "needs a clip on a sequence to apply Lumetri to",
    "transition": "needs two adjacent clips on a sequence",
    "unknown": "no operation is defined for this category",
}


@dataclass
class DraftPlan:
    """A validated (or rejected) draft plan, and what did not make it in."""

    ops: list[dict] = field(default_factory=list)
    #: (recommendation_id, category, reason) for each one not converted.
    not_convertible: list[dict] = field(default_factory=list)
    #: Recommendations that correctly produced nothing.
    no_op: list[dict] = field(default_factory=list)
    valid: bool = False
    validation_error: Optional[dict] = None
    explanation: list[str] = field(default_factory=list)
    generated_at: str = ""
    warnings: list[str] = field(default_factory=list)
    #: Always False here. Present so any consumer reading this file can see
    #: unambiguously that nothing was run.
    executed: bool = False

    @property
    def operation_count(self) -> int:
        return len(self.ops)

    def as_edit_plan(self, *, dry_run: bool = True) -> dict:
        """The plan in the shape ``premiere.validator`` and the engine expect."""
        plan: dict = {
            "ops": list(self.ops),
            "on_error": "abort",
            "label": "editing-brain-v1 draft",
        }
        if dry_run:
            plan["dry_run"] = True
        return plan

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "executed": self.executed,
            "valid": self.valid,
            "operation_count": self.operation_count,
            "validation_error": self.validation_error,
            "explanation": list(self.explanation),
            "not_convertible": list(self.not_convertible),
            "no_op": list(self.no_op),
            "warnings": list(self.warnings),
            "plan": self.as_edit_plan(),
        }


def build_plan(
    recommendations: RecommendationSet | Sequence[EditRecommendation],
    *,
    asset_paths: Optional[dict] = None,
) -> DraftPlan:
    """Convert accepted recommendations into a draft plan.

    Only ``accepted`` recommendations with evidence are considered. Rejected,
    downgraded and held ones are never converted -- that is the point of the
    safety pass -- though they remain in the recommendation file.
    """
    entries = (
        recommendations.recommendations
        if isinstance(recommendations, RecommendationSet)
        else list(recommendations)
    )
    asset_paths = asset_paths or {}

    draft = DraftPlan(generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    for entry in entries:
        if not entry.is_actionable:
            continue

        if entry.category in NO_OP:
            draft.no_op.append({
                "recommendation_id": entry.recommendation_id,
                "category": entry.category,
                "reason": "A hold means no edit; zero operations is correct.",
            })
            continue

        if entry.category not in CONVERTIBLE:
            draft.not_convertible.append({
                "recommendation_id": entry.recommendation_id,
                "category": entry.category,
                "start": round(entry.start, 3),
                "end": round(entry.end, 3),
                "reason": _NOT_YET.get(entry.category, "no operation defined yet"),
            })
            continue

        ops = _to_marker_ops(entry, asset_paths)
        entry.premiere_ops = ops
        draft.ops.extend(ops)

    if not draft.ops:
        draft.warnings.append(
            "No recommendation converted to a Premiere operation. Markers "
            "convert today; picture and audio edits need the footage on a "
            "sequence first."
        )
    return draft


def _to_marker_ops(entry: EditRecommendation, asset_paths: dict) -> list[dict]:
    """A marker on the project item, in source time.

    ``asset`` targets the project item rather than the sequence, so this works
    on footage that has been imported but not yet edited -- which is exactly the
    state this pipeline leaves a project in.
    """
    path = asset_paths.get(entry.asset_id) or entry.source_file
    name = _marker_name(entry)
    op: dict = {
        "op": "marker.add",
        "time": round(entry.start, 3),
        "name": name,
        "comment": _marker_comment(entry),
        "type": "comment",
        "asset": path,
        "note": f"{entry.layer} layer: {entry.category}",
    }
    # A range marker where the recommendation genuinely spans time; a point
    # marker for a cut, which has no duration by definition.
    if entry.duration >= 0.25 and entry.category != "structure_cut":
        op["duration"] = round(entry.duration, 3)
    return [op]


def _marker_name(entry: EditRecommendation) -> str:
    if entry.category == "structure_cut":
        return "CUT"
    effect = entry.effects[0].upper() if entry.effects else "NOTE"
    return f"{effect}"[:32]


def _marker_comment(entry: EditRecommendation) -> str:
    """Everything a human editor needs to judge the marker, in one string."""
    parts = [entry.reason]
    if entry.evidence.channels:
        parts.append("evidence: " + "+".join(entry.evidence.channels))
    if entry.evidence.audio_types:
        parts.append("audio: " + ", ".join(entry.evidence.audio_types[:4]))
    if entry.notes:
        parts.append(entry.notes)
    parts.append(f"priority {entry.priority:.2f}")
    return " | ".join(parts)[:500]


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def dry_run(draft: DraftPlan, *, fps: float = DRY_RUN_FPS) -> DraftPlan:
    """Validate the draft offline. Never touches Premiere.

    Uses ``premiere.validator`` directly rather than going through the engine,
    because the engine's dry-run path is the same validation plus a bridge
    object we have no reason to construct. A validation failure is recorded on
    the draft rather than raised -- an invalid plan is a result to report, and
    the caller still wants the recommendations that produced it.
    """
    if not draft.ops:
        draft.valid = False
        draft.validation_error = {
            "code": "empty_plan",
            "error": "There are no operations to validate.",
            "hint": "Markers are the only category that converts today; check "
                    "whether the safety pass rejected everything.",
        }
        return draft

    try:
        from premiere import validator
    except ImportError as exc:  # pragma: no cover - premiere always ships here
        draft.valid = False
        draft.validation_error = {
            "code": "premiere_unavailable",
            "error": f"Could not import the Premiere validator: {exc}",
        }
        return draft

    try:
        validated = validator.validate_plan(draft.as_edit_plan(), fps=fps)
    except Exception as exc:  # noqa: BLE001 - report, never raise
        to_dict = getattr(exc, "to_dict", None)
        draft.valid = False
        draft.validation_error = (
            to_dict() if callable(to_dict)
            else {"code": "validation_error", "error": str(exc)}
        )
        return draft

    draft.valid = True
    draft.validation_error = None
    draft.explanation = validator.explain(validated)
    return draft


def build_and_dry_run(
    recommendations: RecommendationSet | Sequence[EditRecommendation],
    *,
    asset_paths: Optional[dict] = None,
    fps: float = DRY_RUN_FPS,
) -> DraftPlan:
    """Convert and validate in one call. Still executes nothing."""
    return dry_run(build_plan(recommendations, asset_paths=asset_paths), fps=fps)
