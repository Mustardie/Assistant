"""EditPlan validation -- the gate between creative reasoning and Premiere.

Nothing reaches the host without passing through here. That is the whole point
of the separation the brief asks for: the model produces a plan, the plan is
proved well-formed, and only then does anything touch the timeline. A rejected
plan costs one round-trip and produces an error the model can act on; an
unvalidated plan costs a corrupted sequence.

Two layers of checking:

* per-operation field validation, driven by the catalog (types, ranges,
  enums, required-ness, selector shapes);
* semantic checks that a type system cannot express -- "clip.move needs either
  'to' or 'by'", "a bake window must be non-empty", "audio.duck ranges must be
  ordered".

Both run before execution, over the whole plan, so the model gets every problem
at once rather than discovering them one failed operation at a time.
"""
from __future__ import annotations

from typing import Optional

from premiere import catalog
from premiere.errors import ValidationError
from premiere.schema import parse_time

#: How a plan behaves when one operation fails.
ERROR_MODES = ("abort", "continue", "rollback")


def _need_one_of(params: dict, keys, op: str, *, hint: str = "") -> None:
    present = [k for k in keys if k in params]
    if not present:
        raise ValidationError(
            f"{op} needs one of: {', '.join(keys)}",
            hint=hint or f"add one of {', '.join(keys)}",
        )


def _check_property_target(params: dict, op: str) -> None:
    # 'property' is required by the catalog already; this catches the common
    # model mistake of packing "Motion > Scale" into one string, which would
    # otherwise fail deep in ExtendScript with an unhelpful message.
    prop = params.get("property", "")
    if ">" in prop or "/" in prop:
        left, _, right = prop.replace("/", ">").partition(">")
        raise ValidationError(
            f"{op}: put the component and property in separate fields",
            hint=f'use {{"component": "{left.strip()}", "property": "{right.strip()}"}}',
            path="property",
        )


def _check_range(params: dict, key: str, op: str) -> None:
    rng = params.get(key)
    if rng and len(rng) == 2 and rng[1] <= rng[0]:
        raise ValidationError(
            f"{op}: {key} end ({rng[1]:g}s) must be after start ({rng[0]:g}s)",
            path=key,
        )


def _semantic(op_name: str, params: dict) -> None:
    """Per-operation checks beyond field types."""

    if op_name in ("property.get", "property.set", "property.reset",
                   "keyframe.list", "keyframe.set", "keyframe.add",
                   "keyframe.remove", "keyframe.clear", "keyframe.move",
                   "keyframe.interpolation", "keyframe.bake", "animate"):
        _check_property_target(params, op_name)

    if op_name == "clip.move":
        _need_one_of(params, ("to", "by", "track"), op_name,
                     hint="'to' sets an absolute start time, 'by' nudges, "
                          "'track' changes track")

    if op_name == "clip.trim":
        _need_one_of(params, ("by", "to"), op_name,
                     hint="'by' trims relatively, 'to' trims to an absolute time")
        if params.get("edge") == "both" and "to" in params:
            raise ValidationError(
                "clip.trim: edge='both' cannot use an absolute 'to' time",
                hint="use 'by' for both edges, or trim each edge separately",
                path="edge",
            )

    if op_name == "clip.set":
        _need_one_of(params, ("name", "enabled", "label"), op_name)

    if op_name == "clip.speed":
        if "rate" not in params and not params.get("reverse"):
            raise ValidationError(
                "clip.speed needs a 'rate' (or reverse=true)",
                hint="rate 2.0 doubles speed, 0.5 halves it",
            )

    if op_name == "clip.speed_ramp":
        points = params.get("points", [])
        times = [p["time"] for p in points]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValidationError(
                "clip.speed_ramp: points must be in increasing time order",
                path="points",
            )

    if op_name == "track.set":
        _need_one_of(params, ("name", "muted", "locked", "solo", "targeted"), op_name)

    if op_name == "track.add":
        if not params.get("video") and not params.get("audio"):
            raise ValidationError(
                "track.add needs a video and/or audio count above zero"
            )

    if op_name == "track.remove":
        if not params.get("empty_only") and "track" not in params:
            raise ValidationError(
                "track.remove needs a 'track' (or empty_only=true)"
            )

    if op_name == "keyframe.remove":
        _need_one_of(params, ("at", "range"), op_name)
        _check_range(params, "range", op_name)

    if op_name == "keyframe.move":
        _need_one_of(params, ("by", "scale"), op_name)
        _check_range(params, "range", op_name)

    if op_name == "keyframe.interpolation":
        _check_range(params, "range", op_name)

    if op_name == "keyframe.set":
        times = [k["time"] for k in params.get("keyframes", [])]
        if len(set(times)) != len(times):
            raise ValidationError(
                "keyframe.set: two keyframes share the same time",
                hint="each keyframe needs a distinct time",
                path="keyframes",
            )

    if op_name == "keyframe.bake":
        if params["end"] <= params["start"]:
            raise ValidationError(
                f"keyframe.bake: end ({params['end']:g}s) must be after "
                f"start ({params['start']:g}s)",
                path="end",
            )

    if op_name == "effect.remove":
        if not params.get("all") and "effect" not in params and "index" not in params:
            raise ValidationError(
                "effect.remove needs an 'effect' name, an 'index', or all=true"
            )

    if op_name == "audio.fade":
        _need_one_of(params, ("in", "out"), op_name,
                     hint="give a fade-in and/or fade-out duration")

    if op_name == "audio.duck":
        for i, window in enumerate(params.get("under", [])):
            if window["end"] <= window["start"]:
                raise ValidationError(
                    f"audio.duck: speech range {i} ends before it starts",
                    path=f"under[{i}]",
                )
        if params.get("duck_db", -14.0) > params.get("base_db", 0.0):
            raise ValidationError(
                "audio.duck: duck_db should be quieter than base_db",
                hint="duck_db is the level while speech plays",
                path="duck_db",
            )

    if op_name == "captions.build":
        segments = params.get("segments", [])
        for i, segment in enumerate(segments):
            if segment["end"] <= segment["start"]:
                raise ValidationError(
                    f"captions.build: segment {i} ends before it starts",
                    path=f"segments[{i}]",
                )
            if not segment["text"].strip():
                raise ValidationError(
                    f"captions.build: segment {i} has no text",
                    path=f"segments[{i}].text",
                )

    if op_name == "graphic.shape":
        shape = params.get("shape")
        if shape == "polygon" and not params.get("points") and not params.get("sides"):
            raise ValidationError(
                "graphic.shape: a polygon needs 'points' or 'sides'",
                path="shape",
            )

    if op_name == "sequence.activate":
        _need_one_of(params, ("name", "id"), op_name)

    if op_name == "timeline.playhead":
        _need_one_of(params, ("time", "in", "out"), op_name)

    if op_name == "timeline.snapshot":
        _check_range(params, "range", op_name)

    if op_name == "color.grade":
        knobs = set(params) - {"clip"}
        if not knobs:
            raise ValidationError(
                "color.grade needs at least one grading parameter",
                hint="e.g. exposure, contrast, saturation, temperature",
            )


def validate_operation(entry, *, fps: float = 30.0, index: Optional[int] = None) -> dict:
    """Validate one ``{"op": ..., ...}`` object into a normalised operation."""
    where = f"ops[{index}]" if index is not None else "operation"

    if not isinstance(entry, dict):
        raise ValidationError(
            f"each operation must be an object, got {type(entry).__name__}",
            hint='e.g. {"op": "property.set", "clip": {"track": "V1", "index": 0}, '
                 '"property": "Opacity", "value": 50}',
            path=where,
        )

    name = entry.get("op") or entry.get("operation") or entry.get("action")
    if not name:
        raise ValidationError(
            "operation is missing its 'op' name",
            hint=f"valid ops: {', '.join(sorted(catalog.OPS)[:8])}, ... "
                 f"(call caps.operations for all {len(catalog.OPS)})",
            path=where,
        )
    if not isinstance(name, str):
        raise ValidationError(
            f"'op' must be a string, got {type(name).__name__}", path=where
        )

    spec = catalog.get(name)
    if spec is None:
        from premiere.schema import _suggest
        near = _suggest(name, catalog.OPS.keys())
        raise ValidationError(
            f"unknown operation {name!r}",
            hint=(f"did you mean {near!r}?" if near else
                  "call caps.operations to list the editing interface"),
            path=where,
        )

    if not spec.supported:
        from premiere.errors import UnsupportedError
        raise UnsupportedError(
            f"{name} is not achievable through Premiere's scripting API",
            alternative=spec.alternative,
            path=where,
            detail={"reason": spec.unsupported_reason},
        )

    params = {k: v for k, v in entry.items()
              if k not in ("op", "operation", "action", "note", "_note")}

    try:
        validated = spec.validate(params, fps=fps)
        _semantic(name, validated)
    except ValidationError as exc:
        raise ValidationError(
            exc.message,
            hint=exc.hint,
            path=f"{where}.{exc.path}" if exc.path else where,
        ) from None

    out = {"op": name, "params": validated}
    if entry.get("note"):
        # Carried through to history so a human reading the log can see what
        # the model thought it was doing, not just what it did.
        out["note"] = str(entry["note"])
    return out


def validate_plan(plan, *, fps: float = 30.0) -> dict:
    """Validate a whole EditPlan.

    Accepts either a full plan object or a bare list of operations (models
    reliably produce both, and rejecting the list form buys nothing).
    """
    if isinstance(plan, list):
        plan = {"ops": plan}
    if not isinstance(plan, dict):
        raise ValidationError(
            f"an EditPlan must be an object or a list of operations, "
            f"got {type(plan).__name__}",
            hint='e.g. {"ops": [{"op": "timeline.snapshot"}]}',
        )

    # Checked by presence, not truthiness: an empty list is a different
    # mistake from a missing key and deserves a different message.
    ops = None
    for key in ("ops", "operations", "edits"):
        if key in plan:
            ops = plan[key]
            break
    if ops is None:
        raise ValidationError(
            "EditPlan has no 'ops'",
            hint='e.g. {"ops": [{"op": "timeline.snapshot"}]}',
        )
    if not isinstance(ops, list):
        raise ValidationError(
            f"'ops' must be a list, got {type(ops).__name__}", path="ops"
        )
    if not ops:
        raise ValidationError("EditPlan contains no operations", path="ops")

    on_error = str(plan.get("on_error", "abort")).lower()
    if on_error not in ERROR_MODES:
        raise ValidationError(
            f"on_error must be one of {', '.join(ERROR_MODES)}, got {on_error!r}",
            hint="'abort' stops at the first failure, 'continue' runs the rest, "
                 "'rollback' undoes everything the plan did",
            path="on_error",
        )

    validated = [validate_operation(entry, fps=fps, index=i)
                 for i, entry in enumerate(ops)]

    out = {
        "ops": validated,
        "on_error": on_error,
        "mutating": any(catalog.OPS[o["op"]].mutating for o in validated),
    }
    if plan.get("revision"):
        out["revision"] = str(plan["revision"])
    if plan.get("sequence"):
        out["sequence"] = str(plan["sequence"])
    if plan.get("label"):
        out["label"] = str(plan["label"])
    if plan.get("dry_run"):
        out["dry_run"] = True
    return out


def explain(plan) -> list:
    """Human-readable one-line summaries of a validated plan.

    Used by the dry-run path and by history, so a person can audit what an
    autonomous edit was about to do before it does it.
    """
    lines = []
    for i, op in enumerate(plan.get("ops", [])):
        spec = catalog.get(op["op"])
        summary = spec.summary.split(".")[0] if spec else op["op"]
        detail = []
        params = op.get("params", {})
        if "clip" in params:
            detail.append(_describe_selector(params["clip"]))
        if "property" in params:
            # Only name the component when the plan named one -- guessing
            # "Motion" here would mislabel Volume and effect parameters, and
            # the real resolution happens in the engine.
            component = params.get("component")
            detail.append(f"{component} > {params['property']}" if component
                          else str(params["property"]))
        if "effect" in params:
            detail.append(str(params["effect"]))
        suffix = f" [{'; '.join(detail)}]" if detail else ""
        lines.append(f"{i + 1}. {op['op']}{suffix} -- {summary}")
    return lines


def _describe_selector(selector: dict) -> str:
    track = selector.get("track")
    label = ""
    if track:
        prefix = "V" if track["type"] == "video" else "A"
        label = f"{prefix}{track['index'] + 1}"
    if "index" in selector:
        return f"{label}#{selector['index']}"
    if "at" in selector:
        return f"{label}@{selector['at']:g}s"
    if "range" in selector:
        return f"{label}[{selector['range'][0]:g}-{selector['range'][1]:g}s]"
    if selector.get("all"):
        return f"{label} (all)"
    if selector.get("selected"):
        return "selection"
    if "id" in selector:
        return f"id:{selector['id']}"
    if "name" in selector:
        return f"name:{selector['name']}"
    return label or "?"
