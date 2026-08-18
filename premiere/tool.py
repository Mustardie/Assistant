"""Agent-facing tools for Premiere editing.

Deliberately five tools, not fifty. The brief's rule -- prefer generic
abstractions over a pile of named verbs -- applies to the tool surface itself:
the expressive power lives in the EditPlan passed to ``premiere_edit``, not in
the number of function names the model has to memorise. Adding a creative
capability means adding a row to ``premiere/catalog.py``; it does not mean
adding a tool here.

    premiere_status        is Premiere reachable, and what is open
    premiere_capabilities  what this install can actually do
    premiere_inspect       read the timeline / assets / parameters
    premiere_edit          execute a validated EditPlan   <- the real surface
    premiere_undo          revert the last batch
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from premiere import catalog
from premiere.engine import EditEngine, engine as default_engine
from premiere.errors import PremiereError

logger = logging.getLogger("nova.premiere.tool")


def _engine() -> EditEngine:
    return default_engine


def _as_plan(plan: Any) -> Any:
    """Accept a plan as an object, a list, or a JSON string.

    Models emit all three, and rejecting the string form would cost a retry
    for something we can resolve unambiguously.
    """
    if isinstance(plan, str):
        text = plan.strip()
        if not text:
            raise PremiereError("The edit plan is empty")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PremiereError(
                f"The edit plan is not valid JSON: {exc}",
                hint='Send an object like {"ops": [{"op": "timeline.snapshot"}]}',
            ) from None
    return plan


# --------------------------------------------------------------------------

def premiere_status() -> dict:
    """Is Premiere reachable, and what project/sequence is open?

    Call this first when an edit fails for unclear reasons -- it distinguishes
    "Premiere isn't running", "the panel isn't open" and "no project is open",
    which need three different fixes.
    """
    engine = _engine()
    health = engine.bridge.health()
    if not health.get("connected"):
        return {
            "success": False,
            "connected": False,
            "error": "Cannot reach Premiere",
            "reason": health.get("error"),
            "hint": "Open Premiere, then Window > Extensions > Nova Premiere "
                    "Bridge. If the panel is missing, run "
                    "`python -m premiere.install` and restart Premiere.",
        }

    out = {
        "success": True,
        "connected": True,
        "project": health.get("project"),
        "project_open": health.get("project_open", False),
        "premiere_version": health.get("version"),
        "sequence": health.get("sequence"),
    }
    try:
        out["sequence_settings"] = engine.sequence_info(refresh=True)
    except PremiereError as exc:
        out["sequence_settings"] = None
        out["note"] = exc.message
    return out


def premiere_capabilities(topic: str = "summary", search: str = "",
                          refresh: bool = False) -> dict:
    """What this Premiere install can genuinely do.

    topic:
      "summary"    -- measured probe: version, QE availability, which keyframe
                      interpolation modes work, what is unsupported and why
      "operations" -- the full editing interface (every op and its parameters)
      "effects"    -- installed effect names, as accepted by effect.apply
      "transitions"-- installed transition names
      "easings"    -- named easing curves for animate / keyframe.bake
      "fonts"      -- font families available for text rendering
      "styles"     -- defined reusable text styles
      "limits"     -- only the things that are impossible, and the alternatives

    Read this instead of guessing at effect names or Premiere features.
    """
    engine = _engine()
    topic = (topic or "summary").strip().lower()

    try:
        if topic in ("summary", "probe"):
            return {"success": True, **engine._expand_caps_probe({"refresh": refresh})}

        if topic in ("operations", "ops", "catalog"):
            return {"success": True, **catalog.describe(category=search)}

        if topic in ("effects", "effect"):
            return {"success": True,
                    **(engine.bridge.call("caps.effects",
                                          {"search": search, "kind": "both"}) or {})}

        if topic in ("transitions", "transition"):
            return {"success": True,
                    **(engine.bridge.call("caps.transitions",
                                          {"search": search, "kind": "both"}) or {})}

        if topic in ("easings", "easing", "curves"):
            return {"success": True, **engine._expand_caps_easings({})}

        if topic in ("fonts", "font"):
            return {"success": True, **engine._expand_caps_fonts({"search": search})}

        if topic == "styles":
            from premiere.styles import list_styles
            return {"success": True, "styles": list_styles()}

        if topic in ("limits", "unsupported"):
            from premiere.capabilities import _HOST_LIMITS
            return {"success": True,
                    "unsupported": catalog.unsupported(),
                    "api_limits": _HOST_LIMITS}

        return {
            "success": False,
            "error": f"Unknown capability topic {topic!r}",
            "hint": "Use: summary, operations, effects, transitions, easings, "
                    "fonts, styles, limits",
        }
    except PremiereError as exc:
        return exc.to_dict()


def premiere_inspect(target: str = "timeline", clip: Optional[dict] = None,
                     component: str = "", track: Optional[str] = None,
                     include_keyframes: bool = False,
                     include_effects: bool = True) -> dict:
    """Read the current state of the edit. Never changes anything.

    target:
      "timeline"  -- every track and clip, with ids, timings and effects
      "sequence"  -- active sequence settings (fps, resolution, duration)
      "sequences" -- all sequences in the project
      "assets"    -- media available in the project panel
      "params"    -- the REAL parameters of a clip (pass `clip`), including
                     which ones can be keyframed. Use this before animating
                     anything you have not animated before.
      "effects"   -- effects currently applied to a clip (pass `clip`)
      "keyframes" -- keyframes on a clip's parameters (pass `clip`)
      "history"   -- what this layer has done recently
    """
    engine = _engine()
    target = (target or "timeline").strip().lower()

    try:
        if target == "timeline":
            params: dict = {"include_effects": include_effects,
                            "include_keyframes": include_keyframes}
            if track:
                from premiere.schema import parse_track
                params["track"] = parse_track(track)
            return {"success": True,
                    **(engine.bridge.call("timeline.snapshot", params) or {})}

        if target in ("sequence", "settings"):
            return {"success": True, **engine.sequence_info(refresh=True)}

        if target == "sequences":
            return {"success": True,
                    **(engine.bridge.call("sequence.list", {}) or {})}

        if target in ("assets", "media", "project"):
            return {"success": True,
                    **(engine.bridge.call("project.assets", {}) or {})}

        if target in ("params", "parameters", "properties"):
            if not clip:
                return _needs_clip("params")
            payload = {"clip": _clip(clip)}
            if component:
                payload["component"] = component
            return {"success": True,
                    **(engine.bridge.call("caps.params", payload) or {})}

        if target in ("effects", "effect"):
            if not clip:
                return _needs_clip("effects")
            return {"success": True,
                    **(engine.bridge.call("effect.list",
                                          {"clip": _clip(clip)}) or {})}

        if target == "keyframes":
            if not clip:
                return _needs_clip("keyframes")
            snapshot = engine.bridge.call("timeline.snapshot", {
                "include_effects": True, "include_keyframes": True,
            }) or {}
            return {"success": True, **snapshot}

        if target == "history":
            return {"success": True, **engine._expand_history_list({"limit": 25})}

        return {
            "success": False,
            "error": f"Unknown inspect target {target!r}",
            "hint": "Use: timeline, sequence, sequences, assets, params, "
                    "effects, keyframes, history",
        }
    except PremiereError as exc:
        return exc.to_dict()


def _needs_clip(target: str) -> dict:
    return {
        "success": False,
        "error": f"inspect target '{target}' needs a clip",
        "hint": 'pass clip={"track": "V1", "index": 0}',
    }


def _clip(selector: Any) -> dict:
    from premiere.schema import parse_clip_selector
    return parse_clip_selector(selector)


def premiere_edit(plan: Any = None, on_error: str = "", dry_run: bool = False,
                  label: str = "") -> dict:
    """Execute a structured EditPlan against the Premiere timeline.

    This is the main editing surface. A plan is a list of operations:

        {"ops": [
          {"op": "clip.split", "clip": {"track": "V1", "at": 12.0}, "at": [12.0]},
          {"op": "animate", "clip": {"track": "V1", "index": 2},
           "property": "Scale", "to": 130, "duration": 1.2,
           "easing": "ease_out"},
          {"op": "audio.duck", "clip": {"track": "A2", "index": 0},
           "under": [{"start": 3.0, "end": 9.5}]}
        ]}

    Call premiere_capabilities("operations") for the full interface. Every
    operation is validated before anything runs, so a malformed plan changes
    nothing.

    on_error:
      "abort"    (default) stop at the first failure, keep what already applied
      "continue" run the rest of the plan regardless
      "rollback" undo the whole plan if any operation fails

    dry_run=True validates and explains the plan without touching the timeline.
    """
    if plan is None:
        return {
            "success": False,
            "error": "premiere_edit needs a plan",
            "hint": 'e.g. {"ops": [{"op": "timeline.snapshot"}]} -- call '
                    'premiere_capabilities("operations") for the interface',
        }

    try:
        parsed = _as_plan(plan)
    except PremiereError as exc:
        return exc.to_dict()

    if isinstance(parsed, dict):
        if on_error:
            parsed["on_error"] = on_error
        if dry_run:
            parsed["dry_run"] = True
        if label:
            parsed["label"] = label
    elif isinstance(parsed, list):
        parsed = {"ops": parsed}
        if on_error:
            parsed["on_error"] = on_error
        if dry_run:
            parsed["dry_run"] = True
        if label:
            parsed["label"] = label

    try:
        return _engine().run(parsed)
    except PremiereError as exc:
        return exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - the tool contract is a dict
        logger.exception("premiere_edit crashed")
        return {"success": False, "code": "internal_error", "error": str(exc)}


def premiere_undo(steps: int = 1) -> dict:
    """Revert the last edit batch (restores the automatic pre-edit checkpoint).

    Premiere's scripting API has no undo call, so this restores the project
    state captured before the batch ran. It undoes whole batches, not
    individual operations.
    """
    try:
        return {"success": True,
                **_engine()._expand_history_undo({"steps": int(steps)})}
    except PremiereError as exc:
        return exc.to_dict()
