"""A visual plan, as operations Premiere could run.

Nothing here executes anything. The plan is built offline, validated against
the operation catalog offline, and is inspectable before anybody types a
``--yes`` — which happens elsewhere, one gate at a time, exactly as every other
executable plan in this system works.

## What maps, and what does not

The catalog is generous: ``animate`` moves any parameter along an easing curve,
``graphic.shape`` draws boxes and arrows and circles, ``text.create`` makes a
real timeline clip that every transform op then works on, ``clip.freeze`` holds
a frame and ``clip.speed_ramp`` retimes one. Most of the library maps onto
those five.

Two things genuinely do not, and they are listed as unsupported with the reason
rather than approximated:

* **screen shake** needs a per-frame position wobble. ``animate`` moves a
  parameter from one value to another; a shake is thirty small moves and the
  catalog has no primitive for generating them.
* **instant replay** means playing a range twice, which is a change to the
  *cut* rather than something laid on top of it. It belongs to the rough-cut
  layer and this one has no business inventing it.

**A callout knows what to point at and never where.** The vision pass names
entities; it does not localise them. So every callout operation lands at a
default position with a note saying so, and a person moves it. Emitting a
confident-looking coordinate would be the one dishonest thing in this file.
"""
from __future__ import annotations

import logging
from typing import Sequence

from editing.visuals.execution import (
    PremiereVisualOperation, PremiereVisualOperationPlan, UnsupportedTreatment,
)
from editing.tracks import DEFAULT_LAYOUT
from editing.visuals.schema import VisualLayerPlan, VisualTreatment, now

logger = logging.getLogger("nova.editing.visuals.premiere")

#: The track visual overlays land on. Above V1 (the rough cut) and V2 (the
#: style layer's captions and cards), so the whole visual pass can be removed
#: by deleting one track. Taken from the shared layout rather than written
#: out here: the asset pass used to name the same track independently, and
#: the two silently overwrote each other.
VISUAL_TRACK = DEFAULT_LAYOUT.treatments

#: Where a card's plate sits, as a fraction of the frame.
CARD_PLATE = {"position": [0.5, 0.5], "size": [1.0, 0.26]}

#: Colours, in one place so a plan looks like one video rather than five.
COLOURS = {
    "plate": "#000000",
    "text": "#FFFFFF",
    "danger": "#FF3B30",
    "objective": "#4CD964",
    "highlight": "#FFCC00",
    "flash": "#FFFFFF",
}

#: Font size per card weight, as a fraction of frame height. The catalog takes
#: absolute points, so these are resolved against 1080 at build time and the
#: note says so.
_TITLE_SIZE = 84
_SUBTITLE_SIZE = 38
_LABEL_SIZE = 52

#: Effects with no catalog representation, and why.
UNSUPPORTED = {
    "screen_shake": (
        "a shake is a per-frame position wobble. `animate` moves a parameter "
        "from one value to another along a curve, and the catalog has no "
        "primitive for generating thirty small moves.",
        "apply a Transform effect by hand, or use a shake preset -- "
        "`color.preset` is itself unsupported, so this is a manual step",
    ),
    "instant_replay": (
        "replaying a range means playing footage twice, which is a change to "
        "the cut rather than an overlay on it.",
        "cut the replay in by hand, or ask the rough-cut layer for it -- "
        "`replay_marker` puts a marker where it would go",
    ),
    "crop_pan": (
        "a pan across a scaled frame needs Position and Scale animated "
        "together with a known crop origin, and this system does not know "
        "where in the frame the subject is.",
        "animate Motion > Position by hand from the marker this plan leaves",
    ),
}


def can_express(effect: str) -> bool:
    """Whether the catalog has a representation for this effect."""
    return effect not in UNSUPPORTED and effect in _BUILDERS


def build_premiere_plan(
    plan: VisualLayerPlan,
    *,
    name: str = "structure",
    track: str = VISUAL_TRACK,
) -> PremiereVisualOperationPlan:
    """Every accepted treatment, as catalog operations. Executes nothing."""
    out = PremiereVisualOperationPlan(
        name=name,
        sequence_name=plan.sequence_name,
        track=track,
        generated_at=now(),
    )

    for treatment in plan.accepted:
        if treatment.effect in UNSUPPORTED:
            reason, alternative = UNSUPPORTED[treatment.effect]
            out.unsupported.append(UnsupportedTreatment(
                treatment_id=treatment.treatment_id,
                effect=treatment.effect,
                start=treatment.start,
                reason=reason,
                alternative=alternative,
            ))
            continue

        builder = _BUILDERS.get(treatment.effect)
        if builder is None:
            out.unsupported.append(UnsupportedTreatment(
                treatment_id=treatment.treatment_id,
                effect=treatment.effect,
                start=treatment.start,
                reason="no operation builder exists for this effect",
                alternative="place it by hand from the marker this plan "
                            "leaves",
            ))
            continue

        for op in builder(treatment, track):
            out.operations.append(PremiereVisualOperation(
                treatment_id=treatment.treatment_id,
                effect=treatment.effect,
                op=op,
                note=treatment.reason[:200],
            ))

    _warn(out)
    return out


def _warn(out: PremiereVisualOperationPlan) -> None:
    if out.unsupported:
        out.warnings.append(
            f"{len(out.unsupported)} accepted treatment(s) have no catalog "
            "representation. Each one names why and what to do instead."
        )
    callouts = [entry for entry in out.operations
                if entry.op.get("op") == "graphic.shape"]
    if callouts:
        out.warnings.append(
            f"{len(callouts)} shape(s) land at a default position. This "
            "system knows what is on screen and never where, so every callout "
            "has to be moved by hand."
        )
    out.warnings.append(
        "Text is created with engine='render', which rasterises to a PNG "
        "overlay. That path exists on every install; the MOGRT path needs a "
        "registered template and produces text that stays editable in "
        "Premiere."
    )
    out.warnings.append(
        "Nothing here has run. Every operation is a proposal, validated "
        "offline against the catalog and nothing else."
    )


# ---------------------------------------------------------------------------
# The builders
# ---------------------------------------------------------------------------
#
# Each takes ``(treatment, track)`` and returns a list of catalog operations.
# They are small on purpose: an effect that needs more than three operations to
# express is usually an effect the catalog cannot really do.

def _clip_at(treatment: VisualTreatment) -> dict:
    """The selector for the rough-cut clip this treatment sits on.

    By time rather than by index: an index drifts the moment anything earlier
    is split or removed, and a midpoint stays correct as long as the clip is
    where the plan says it is. Session 3's rule, and the same reasoning.
    """
    return {"track": "V1", "at": round(treatment.start + 0.01, 3)}


def _text_op(treatment: VisualTreatment, track: str, *, text: str,
             size: int, colour: str, position: Sequence[float],
             note: str = "") -> dict:
    return {
        "op": "text.create",
        "text": text[:200],
        "track": track,
        "time": round(treatment.start, 3),
        "duration": round(max(0.3, treatment.duration), 3),
        "position": [round(float(position[0]), 4),
                     round(float(position[1]), 4)],
        "engine": "render",
        "size": size,
        "color": colour,
        "align": "center",
        "note": (note or f"{treatment.effect} -- {treatment.reason}")[:200],
    }


def _plate_op(treatment: VisualTreatment, track: str, *,
              opacity: float = 55.0) -> dict:
    return {
        "op": "graphic.shape",
        "shape": "rectangle",
        "track": track,
        "time": round(treatment.start, 3),
        "duration": round(max(0.3, treatment.duration), 3),
        "position": list(CARD_PLATE["position"]),
        "size": list(CARD_PLATE["size"]),
        "fill": COLOURS["plate"],
        "opacity": opacity,
        "note": f"plate behind the {treatment.effect}",
    }


def _card(treatment: VisualTreatment, track: str) -> list[dict]:
    text = str(treatment.payload.get("text") or "")
    subtitle = str(treatment.payload.get("subtitle") or "")
    ops = [_plate_op(treatment, track),
           _text_op(treatment, track, text=text, size=_TITLE_SIZE,
                    colour=COLOURS["text"], position=[0.5, 0.47])]
    if subtitle:
        ops.append(_text_op(treatment, track, text=subtitle,
                            size=_SUBTITLE_SIZE, colour=COLOURS["text"],
                            position=[0.5, 0.57],
                            note=f"subtitle on the {treatment.effect}"))
    return ops


def _label(colour_key: str, position: Sequence[float] = (0.5, 0.82)):
    def build(treatment: VisualTreatment, track: str) -> list[dict]:
        text = str(treatment.payload.get("text") or "")
        if not text:
            return []
        return [_text_op(treatment, track, text=text, size=_LABEL_SIZE,
                         colour=COLOURS[colour_key], position=position)]
    return build


def _zoom(treatment: VisualTreatment, track: str) -> list[dict]:
    """A scale move on the rough-cut clip itself, not on an overlay.

    ``component`` and ``property`` are separate fields: the catalog addresses a
    parameter as (component, name) rather than as a path string, because the
    same property name appears on several components and a path would have to
    be parsed back apart to find out which.
    """
    scale = float(treatment.payload.get("scale") or 106.0)
    return [{
        "op": "animate",
        "clip": _clip_at(treatment),
        "component": "Motion",
        "property": "Scale",
        "from": 100.0,
        "to": round(scale, 2),
        "start": round(treatment.start, 3),
        "duration": round(max(0.2, treatment.duration), 3),
        "easing": treatment.easing,
        "relative_to": "sequence",
        "note": f"{treatment.effect} -- {treatment.reason}"[:200],
    }]


def _freeze(treatment: VisualTreatment, track: str) -> list[dict]:
    ops: list[dict] = [{
        "op": "clip.freeze",
        "clip": _clip_at(treatment),
        "at": round(float(treatment.payload.get("hold_at")
                          or treatment.start), 3),
        "duration": round(max(0.2, treatment.duration), 3),
        "note": f"{treatment.effect} -- {treatment.reason}"[:200],
    }]
    text = str(treatment.payload.get("text") or "")
    if text:
        ops.append(_text_op(treatment, track, text=text, size=_LABEL_SIZE,
                            colour=COLOURS["text"], position=[0.5, 0.8]))
    return ops


def _shape(shape: str, colour_key: str, filled: bool):
    def build(treatment: VisualTreatment, track: str) -> list[dict]:
        op = {
            "op": "graphic.shape",
            "shape": shape,
            "track": track,
            "time": round(treatment.start, 3),
            "duration": round(max(0.3, treatment.duration), 3),
            # Centre of frame. This system knows *what* is on screen and never
            # *where*, and a confident-looking coordinate would be the one
            # dishonest thing in this file.
            "position": [0.5, 0.5],
            "size": [0.18, 0.18],
            "fill": "#00000000" if not filled else COLOURS[colour_key],
            "stroke_color": COLOURS[colour_key],
            "stroke_width": 8.0,
            "opacity": 90.0,
            "note": (f"{treatment.effect} pointing at "
                     f"{treatment.payload.get('target') or 'an unnamed thing'}"
                     " -- POSITION IS A GUESS, move it by hand")[:200],
        }
        return [op]
    return build


def _flash(treatment: VisualTreatment, track: str) -> list[dict]:
    opacity = float(treatment.payload.get("opacity") or 60.0)
    return [{
        "op": "graphic.shape",
        "shape": "rectangle",
        "track": track,
        "time": round(treatment.start, 3),
        "duration": round(max(0.1, treatment.duration), 3),
        "position": [0.5, 0.5],
        "size": [1.0, 1.0],
        "fill": COLOURS["flash"],
        "opacity": opacity,
        "note": f"{treatment.effect} -- {treatment.reason}"[:200],
    }]


def _letterbox(treatment: VisualTreatment, track: str) -> list[dict]:
    bars = float(treatment.payload.get("bars") or 0.11)
    common = {
        "op": "graphic.shape",
        "shape": "rectangle",
        "track": track,
        "time": round(treatment.start, 3),
        "duration": round(max(0.3, treatment.duration), 3),
        "size": [1.0, round(bars, 4)],
        "fill": COLOURS["plate"],
        "opacity": 100.0,
    }
    return [
        {**common, "position": [0.5, round(bars / 2, 4)],
         "note": "letterbox: top bar"},
        {**common, "position": [0.5, round(1.0 - bars / 2, 4)],
         "note": "letterbox: bottom bar"},
    ]


def _speed_ramp(treatment: VisualTreatment, track: str) -> list[dict]:
    rate = float(treatment.payload.get("rate") or 2.0)
    return [{
        "op": "clip.speed_ramp",
        "clip": _clip_at(treatment),
        "points": [
            {"time": 0.0, "rate": 1.0},
            {"time": round(max(0.2, treatment.duration / 2), 3),
             "rate": round(rate, 2)},
            {"time": round(max(0.4, treatment.duration), 3), "rate": 1.0},
        ],
        "smooth": True,
        "note": f"{treatment.effect} -- {treatment.reason}"[:200],
    }]


def _marker(treatment: VisualTreatment, track: str) -> list[dict]:
    """A marker. Changes no frame, and is the honest form of several effects."""
    detail = str(treatment.payload.get("note")
                 or treatment.payload.get("text") or "")
    return [{
        "op": "marker.add",
        "time": round(treatment.start, 3),
        "name": treatment.effect.replace("_", " ").upper()[:24],
        "type": "comment",
        "comment": (f"{treatment.effect}: {treatment.reason}"
                    + (f" | {detail}" if detail else "")
                    + f" [{treatment.treatment_id}]")[:500],
        "duration": round(max(0.0, treatment.duration), 3),
    }]


#: Effect -> the builder that expresses it. Anything absent from here and from
#: ``UNSUPPORTED`` is a gap rather than a decision, and the plan says so.
_BUILDERS = {
    # emphasis
    "zoom_punch": _zoom,
    "quick_punch_in": _zoom,
    "slow_zoom_hold": _zoom,
    "freeze_frame": _freeze,
    "freeze_frame_label": _freeze,
    # callouts
    "arrow_callout": _shape("arrow", "highlight", False),
    "circle_highlight": _shape("circle", "highlight", False),
    "box_highlight": _shape("rounded_rect", "highlight", False),
    "entity_callout": _shape("rounded_rect", "highlight", False),
    "label_tag": _label("text"),
    "danger_warning_label": _label("danger"),
    "objective_label": _label("objective"),
    # cards
    "title_card": _card,
    "objective_card": _card,
    "progress_card": _card,
    "recap_card": _card,
    "chapter_card": _card,
    "setup_payoff_card": _card,
    "later_card": _card,
    "build_progress_card": _card,
    # motion
    "impact_flash": _flash,
    "letterbox": _letterbox,
    "speed_ramp": _speed_ramp,
    "replay_marker": _marker,
    "montage_marker": _marker,
    "dramatic_pause": _marker,
    # minecraft
    "hardcore_warning": _label("danger", (0.5, 0.2)),
    "totem_reminder": _label("highlight", (0.5, 0.78)),
    "health_emphasis": _label("danger", (0.3, 0.86)),
    "villager_danger_meter": _label("danger", (0.5, 0.2)),
    "progression_counter": _label("text", (0.8, 0.14)),
    "day_counter": _label("text", (0.85, 0.12)),
    "coordinates_card": _label("text", (0.2, 0.12)),
}


def validate_offline(plan: PremiereVisualOperationPlan, *,
                     fps: float = 30.0) -> PremiereVisualOperationPlan:
    """Check every operation against the catalog. Touches no host application.

    A plan that will not validate is a plan that would have failed at
    execution, and finding that out here costs nothing. The result is recorded
    on the plan the same way every other dry run in this system records it.
    """
    try:
        from premiere.validator import validate_plan
    except ImportError as exc:  # pragma: no cover - premiere always ships here
        plan.dry_run_passed = False
        plan.dry_run_error = {
            "code": "validator_missing",
            "error": f"the premiere package could not be imported: {exc}",
            "hint": "the plan is still readable; only the offline check is "
                    "unavailable",
        }
        return plan

    if not plan.operations:
        plan.dry_run_passed = False
        plan.dry_run_error = {
            "code": "empty_plan",
            "error": "no accepted treatment produced an operation.",
            "hint": "read the plan's rejections: this is the normal result "
                    "when every treatment was refused or is placeholder-only.",
        }
        return plan

    try:
        validate_plan(plan.as_edit_plan(), fps=fps)
    except Exception as exc:  # noqa: BLE001 - a refusal is a result
        plan.dry_run_passed = False
        plan.dry_run_error = {
            "code": getattr(exc, "code", "validation_failed"),
            "error": str(getattr(exc, "message", exc))[:400],
            "hint": str(getattr(exc, "hint", ""))[:400],
            "path": str(getattr(exc, "path", "")),
        }
        return plan

    plan.dry_run_passed = True
    plan.dry_run_error = None
    return plan
