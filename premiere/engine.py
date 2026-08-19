"""The Premiere editing engine: validated EditPlan in, timeline changes out.

    VIDEO + STORY -> AI EDITOR -> EditPlan -> validation -> [engine] -> timeline

This module is the last stage before the host. Its jobs:

* **Expansion.** The catalog exposes rich, intention-shaped operations
  (``animate``, ``audio.duck``, ``text.create``, ``captions.build``). The
  ExtendScript side implements a much smaller set of primitives. Expanding one
  into the other happens here, in Python, where the maths is unit-testable
  without a Premiere host -- which is why easing curves, ducking envelopes and
  caption layout all live on this side of the bridge.

* **Batching.** Everything an expansion produces is sent as one batch, so a
  200-keyframe animation is one round trip and one Premiere undo group rather
  than 200 of each.

* **Safety.** Revision checks before, checkpoints around, history after, and a
  partial-failure report that says exactly which operation failed and what had
  already been applied when it did.

The engine never generates ExtendScript. It emits primitive operation objects;
the host decides how to execute them. That boundary is what keeps a future
editing model from being able to run arbitrary code inside Premiere.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from premiere import catalog, easing as easing_mod, validator
from premiere.assets import (
    parse_color, render_shape, render_text, split_segment_text, write_srt,
)
from premiere.bridge import PremiereBridge, bridge as default_bridge
from premiere.errors import (
    BridgeError, HostError, PremiereError, UnsupportedError, ValidationError,
)
from premiere.history import History, history as default_history

logger = logging.getLogger("nova.premiere.engine")

#: Component that carries a clip's intrinsic transform parameters.
MOTION = "Motion"
OPACITY_COMPONENT = "Opacity"
VOLUME_COMPONENT = "Volume"

#: Friendly parameter aliases. A model writes "opacity"; Premiere calls the
#: parameter "Opacity" and puts it on its own component. Mapping the handful of
#: intrinsic properties here means the generic property ops work with the names
#: a model naturally reaches for, without inventing per-property operations.
PROPERTY_ALIASES = {
    "position": (MOTION, "Position"),
    "scale": (MOTION, "Scale"),
    "scale_width": (MOTION, "Scale Width"),
    "rotation": (MOTION, "Rotation"),
    "anchor": (MOTION, "Anchor Point"),
    "anchor_point": (MOTION, "Anchor Point"),
    "anti-flicker": (MOTION, "Anti-flicker Filter"),
    "opacity": (OPACITY_COMPONENT, "Opacity"),
    "blend_mode": (OPACITY_COMPONENT, "Blend Mode"),
    "volume": (VOLUME_COMPONENT, "Level"),
    "level": (VOLUME_COMPONENT, "Level"),
    "gain": (VOLUME_COMPONENT, "Level"),
    "pan": ("Panner", "Balance"),
}

#: Lumetri parameter names, keyed by the friendly names ``color.grade`` accepts.
#: The host verifies each against the real component and reports any it cannot
#: find, so a rename in a future Premiere build surfaces as a clear message
#: rather than a silent no-op.
LUMETRI_PARAMS = {
    "exposure": "Exposure",
    "contrast": "Contrast",
    "highlights": "Highlights",
    "shadows": "Shadows",
    "whites": "Whites",
    "blacks": "Blacks",
    "temperature": "Temperature",
    "tint": "Tint",
    "saturation": "Saturation",
    "vibrance": "Vibrance",
    "sharpen": "Sharpen",
    "fade": "Fade Film",
}

LUMETRI_EFFECT = "Lumetri Color"

MAX_BAKED_KEYS = 600


class EditEngine:
    """Executes validated EditPlans against a Premiere host."""

    def __init__(
        self,
        bridge: Optional[PremiereBridge] = None,
        history: Optional[History] = None,
    ):
        self.bridge = bridge or default_bridge
        self.history = history or default_history
        self._sequence_cache: Optional[dict] = None
        self._cache_time = 0.0

    # ------------------------------------------------------------------
    # Sequence context
    # ------------------------------------------------------------------

    def sequence_info(self, refresh: bool = False) -> dict:
        """Active sequence settings, cached briefly.

        fps and frame size are needed to validate timecode, bake keyframes at
        frame resolution and render full-frame overlays -- so almost every plan
        needs them, and re-fetching per operation would triple the round trips.
        The cache is short because the user can switch sequences at any moment.
        """
        if (not refresh and self._sequence_cache
                and time.time() - self._cache_time < 3.0):
            return self._sequence_cache
        info = self.bridge.call("sequence.info") or {}
        self._sequence_cache = info
        self._cache_time = time.time()
        return info

    def _fps(self) -> float:
        """Sequence frame rate, or 30 if the host cannot be asked.

        Deliberately swallows everything. fps is only needed to interpret
        timecode and frame-count spellings; failing a whole plan because the
        frame rate could not be read would turn a cosmetic problem into an
        outage, and a plan written in seconds does not depend on it at all.
        """
        try:
            return float(self.sequence_info().get("fps") or 30.0) or 30.0
        except Exception:  # noqa: BLE001 - fps is advisory, never fatal
            return 30.0

    def _frame_size(self) -> tuple:
        info = self.sequence_info()
        return (int(info.get("width") or 1920), int(info.get("height") or 1080))

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run(self, plan, *, checkpoint: Optional[bool] = None) -> dict:
        """Validate and execute an EditPlan. Never raises for edit failures.

        Returns a result dict with ``success``, per-operation ``results`` and,
        when something went wrong, exactly how far execution got. Raising would
        lose the partial-progress information the caller needs to recover.
        """
        # A dry run must not need Premiere at all -- validating a plan offline
        # is most of its value, and asking the host for a frame rate first
        # would make it fail exactly when it is most useful.
        wants_dry_run = bool(
            isinstance(plan, dict) and plan.get("dry_run")
        )
        fps = 30.0 if wants_dry_run else self._fps()

        try:
            validated = validator.validate_plan(plan, fps=fps)
        except PremiereError as exc:
            return exc.to_dict()

        if validated.get("dry_run"):
            return {
                "success": True,
                "dry_run": True,
                "operations": len(validated["ops"]),
                "plan": validator.explain(validated),
            }

        return self.execute(validated, checkpoint=checkpoint)

    def execute(self, validated: dict, *, checkpoint: Optional[bool] = None) -> dict:
        """Execute an already-validated plan."""
        ops = validated["ops"]
        on_error = validated.get("on_error", "abort")
        mutating = validated.get("mutating", True)

        try:
            self.bridge.require_connected()
        except PremiereError as exc:
            return exc.to_dict()

        if validated.get("sequence"):
            try:
                self.bridge.call("sequence.activate", {"name": validated["sequence"]})
                self.sequence_info(refresh=True)
            except PremiereError as exc:
                return exc.to_dict()

        # Revision protection: refuse to apply a plan written against a
        # timeline that has since moved under us.
        try:
            current = self._current_revision()
            self.history.check_revision(validated.get("revision"), current)
        except PremiereError as exc:
            return exc.to_dict()

        checkpoint_path = None
        want_checkpoint = mutating if checkpoint is None else bool(checkpoint)
        if want_checkpoint or on_error == "rollback":
            checkpoint_path = self._checkpoint(f"auto-{int(time.time())}")

        results: list = []
        failures: list = []

        for index, op in self._scheduled(ops):
            if isinstance(op, list):
                # A run of primitive operations, sent as one round trip.
                batch_results = self._execute_batch(op, index, on_error)
                results.extend(batch_results)
                failures.extend([r for r in batch_results if not r["success"]])
                if failures and on_error in ("abort", "rollback"):
                    break
                continue
            try:
                result = self._execute_one(op)
                results.append({"op": op["op"], "index": index, "success": True,
                                "result": result})
            except PremiereError as exc:
                record = exc.to_dict()
                record.update({"op": op["op"], "index": index, "success": False})
                results.append(record)
                failures.append(record)
                logger.warning("Premiere op %s (#%d) failed: %s", op["op"], index, exc)
                if on_error == "abort":
                    break
                if on_error == "rollback":
                    break
            except Exception as exc:  # noqa: BLE001 - a bug must not corrupt state
                logger.exception("Unhandled error executing %s", op["op"])
                record = {"op": op["op"], "index": index, "success": False,
                          "code": "internal_error", "error": str(exc)}
                results.append(record)
                failures.append(record)
                if on_error in ("abort", "rollback"):
                    break

        rolled_back = False
        if failures and on_error == "rollback" and checkpoint_path:
            rolled_back = self._restore(checkpoint_path)

        revision = self._current_revision()
        self.history.record(
            ops=ops,
            results=results,
            revision=revision,
            label=validated.get("label", ""),
            status="ok" if not failures else ("rolled_back" if rolled_back else "partial"),
            undo_steps=0 if (rolled_back or not mutating) else len(
                [r for r in results if r.get("success")]
            ),
        )
        if checkpoint_path and mutating and not rolled_back:
            self.history.save_checkpoint(f"auto-{revision or int(time.time())}",
                                         {"path": checkpoint_path, "auto": True})

        applied = len([r for r in results if r.get("success")])
        out: dict = {
            "success": not failures,
            "applied": applied,
            "total": len(ops),
            "revision": revision,
            "results": results,
        }
        if failures:
            first = failures[0]
            out["error"] = (
                f"{first['op']} (operation {first['index'] + 1} of {len(ops)}) "
                f"failed: {first.get('error')}"
            )
            if first.get("hint"):
                out["hint"] = first["hint"]
            out["failed"] = len(failures)
            out["partial"] = applied > 0 and not rolled_back
            if rolled_back:
                out["rolled_back"] = True
                out["note"] = "All changes from this plan were reverted."
            elif applied:
                out["note"] = (
                    f"{applied} operation(s) were applied before the failure and "
                    f"are still on the timeline. Read timeline.snapshot before "
                    f"retrying so you do not double-apply them."
                )
        return out

    # ------------------------------------------------------------------
    # Single operation dispatch
    # ------------------------------------------------------------------

    def _expander_for(self, name: str):
        return getattr(self, f"_expand_{name.replace('.', '_')}", None)

    #: Below this, one round trip per operation is simpler and gives sharper
    #: error attribution; above it, the HTTP + evalScript overhead dominates.
    BATCH_THRESHOLD = 4

    def _scheduled(self, ops: list):
        """Yield ``(index, op)``, grouping runs of primitives into batches.

        Operations that need Python-side expansion (animate, audio.duck,
        text.create...) may issue several host calls and sometimes need a reply
        before deciding the next one, so they always run alone. Everything else
        is a straight 1:1 pass-through and can be sent together.
        """
        run: list = []
        run_start = 0

        def flush():
            if not run:
                return None
            if len(run) < self.BATCH_THRESHOLD:
                out = [(run_start + offset, item) for offset, item in enumerate(run)]
            else:
                out = [(run_start, list(run))]
            return out

        for index, op in enumerate(ops):
            if self._expander_for(op["op"]) is None:
                if not run:
                    run_start = index
                run.append(op)
                continue
            flushed = flush()
            if flushed:
                for entry in flushed:
                    yield entry
            run = []
            yield (index, op)

        flushed = flush()
        if flushed:
            for entry in flushed:
                yield entry

    def _execute_batch(self, ops: list, start_index: int, on_error: str) -> list:
        """Send a run of primitive operations in one request."""
        payload = [{"op": op["op"], "params": self._normalise_target(
            dict(op.get("params", {})))} for op in ops]

        try:
            response = self.bridge.call_batch(
                payload, on_error="abort" if on_error != "continue" else "continue"
            ) or {}
        except PremiereError as exc:
            # The whole batch failed in transport. Attribute it to the first
            # operation rather than silently reporting nothing.
            record = exc.to_dict()
            record.update({"op": ops[0]["op"], "index": start_index,
                           "success": False})
            return [record]

        out: list = []
        for offset, entry in enumerate(response.get("results", [])):
            index = start_index + offset
            if entry.get("ok"):
                out.append({"op": entry.get("op", ops[offset]["op"]),
                            "index": index, "success": True,
                            "result": entry.get("result")})
            else:
                error = entry.get("error") or {}
                out.append({
                    "op": entry.get("op", ops[offset]["op"]),
                    "index": index,
                    "success": False,
                    "code": error.get("code", "host_error"),
                    "error": error.get("message", "Premiere reported a failure"),
                    "hint": error.get("hint", ""),
                    "detail": error.get("detail"),
                })
        return out

    def _execute_one(self, op: dict) -> Any:
        name = op["op"]
        params = dict(op.get("params", {}))
        expander = self._expander_for(name)
        if expander is not None:
            return expander(params)
        return self.bridge.call(name, self._normalise_target(params))

    @staticmethod
    def _normalise_target(params: dict) -> dict:
        """Resolve friendly property aliases into (component, property)."""
        if "property" not in params:
            return params
        out = dict(params)
        key = str(out["property"]).strip().lower().replace(" ", "_")
        if key in PROPERTY_ALIASES and not out.get("component"):
            component, prop = PROPERTY_ALIASES[key]
            out["component"] = component
            out["property"] = prop
        return out

    def _current_revision(self) -> Optional[str]:
        try:
            info = self.bridge.call("timeline.revision") or {}
            return info.get("revision")
        except PremiereError:
            return None

    # ------------------------------------------------------------------
    # Local (no host) operations
    # ------------------------------------------------------------------

    def _expand_caps_operations(self, params: dict) -> Any:
        return catalog.describe(params.get("category", ""), params.get("op", ""))

    def _expand_caps_easings(self, params: dict) -> Any:
        return {
            "named": sorted(easing_mod.EASINGS),
            "custom": "pass [x1, y1, x2, y2] for a CSS-style cubic-bezier",
            "native_interpolation": sorted(set(easing_mod.NATIVE_INTERP.values())),
            "note": "Easings not expressible as a native Premiere interpolation "
                    "mode are baked into dense keyframes automatically.",
        }

    def _expand_caps_fonts(self, params: dict) -> Any:
        from premiere.assets import list_fonts
        names = list_fonts(params.get("search", ""))
        return {"count": len(names), "fonts": names[:400]}

    def _expand_caps_probe(self, params: dict) -> Any:
        from premiere.capabilities import probe
        return probe(self, refresh=params.get("refresh", False))

    def _expand_history_list(self, params: dict) -> Any:
        return {"entries": self.history.recent(params.get("limit", 25)),
                "revision": self.history.revision,
                "checkpoints": sorted(self.history.list_checkpoints())}

    def _expand_history_checkpoint(self, params: dict) -> Any:
        path = self._checkpoint(params["name"])
        if not path:
            raise HostError(
                "Could not create a checkpoint",
                hint="Premiere must be able to save the project first",
            )
        self.history.save_checkpoint(params["name"], {"path": path, "auto": False})
        return {"name": params["name"], "path": path}

    def _expand_history_restore(self, params: dict) -> Any:
        entry = self.history.get_checkpoint(params["name"])
        if not entry:
            raise ValidationError(
                f"no checkpoint named {params['name']!r}",
                hint=f"available: {', '.join(sorted(self.history.list_checkpoints())) or 'none'}",
            )
        if not self._restore(entry["path"]):
            raise HostError(f"Could not restore checkpoint {params['name']!r}")
        self.sequence_info(refresh=True)
        return {"restored": params["name"], "path": entry["path"]}

    def _expand_history_undo(self, params: dict) -> Any:
        """Undo by restoring the checkpoint taken before the last batch.

        Premiere's scripting API exposes no ``app.undo()``. Rather than fake it
        with synthetic keystrokes -- which is unreliable and can land in the
        wrong panel -- undo is implemented as a project-state restore, which is
        deterministic and covers operations Premiere's own undo stack groups
        unpredictably.
        """
        steps = params.get("steps", 1)
        entries = self.history.pop_undoable(steps)
        if not entries:
            return {"undone": 0, "note": "Nothing this layer has done is undoable."}

        checkpoints = self.history.list_checkpoints()
        auto = sorted(
            ((name, data) for name, data in checkpoints.items() if data.get("auto")),
            key=lambda kv: kv[1].get("at", ""),
        )
        if not auto:
            raise UnsupportedError(
                "No checkpoint is available to undo to",
                alternative="press Ctrl+Z in Premiere, or use history.checkpoint "
                            "before future batches",
            )
        target = auto[-min(len(auto), steps)][1]
        if not self._restore(target["path"]):
            raise HostError("Could not restore the pre-edit project state")
        self.sequence_info(refresh=True)
        return {
            "undone": len(entries),
            "restored_from": target["path"],
            "operations": [op["op"] for entry in entries for op in entry["ops"]],
        }

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------

    def _checkpoint(self, name: str) -> Optional[str]:
        try:
            result = self.bridge.call("project.checkpoint", {"name": name})
            return (result or {}).get("path")
        except PremiereError as exc:
            logger.warning("Checkpoint failed (%s); continuing without one", exc)
            return None

    def _restore(self, path: str) -> bool:
        if not path:
            return False
        try:
            self.bridge.call("project.restore", {"path": path}, timeout=180.0)
            return True
        except PremiereError as exc:
            logger.error("Restore from %s failed: %s", path, exc)
            return False

    # ------------------------------------------------------------------
    # Clip resolution helper
    # ------------------------------------------------------------------

    def _resolve_clips(self, selector: dict) -> list:
        """Ask the host which real clips a selector matches."""
        clips = self.bridge.call("clip.resolve", {"clip": selector}) or []
        if not clips:
            raise HostError(
                "No clip matched that selector",
                hint="Read timeline.snapshot to see the tracks and clip indices "
                     "that actually exist",
                detail={"selector": selector},
            )
        return clips

    # ------------------------------------------------------------------
    # Animation expansions -- the core creative primitives
    # ------------------------------------------------------------------

    def _expand_animate(self, params: dict) -> Any:
        """``animate`` -> two native keyframes, or a baked curve."""
        target = self._normalise_target(params)
        easing = target.get("easing", "linear")
        native = easing_mod.native_interp_for(easing)

        start = target.get("start", 0.0)
        duration = target["duration"]
        relative_to = target.get("relative_to", "clip")
        from_value = target.get("from")

        if native:
            keyframes = []
            if from_value is not None:
                keyframes.append({"time": start, "value": from_value,
                                  "interp": native})
            else:
                # No explicit start value: pin the current value at the start
                # time so the move begins from wherever the parameter already
                # is. The host fills the value in.
                keyframes.append({"time": start, "hold_current": True,
                                  "interp": native})
            keyframes.append({"time": start + duration, "value": target["to"],
                              "interp": native})
            return self._send_keyframes(target, keyframes, relative_to,
                                        replace=False)

        if from_value is None:
            from_value = self._read_value(target, at=start,
                                          relative_to=relative_to)

        keys = easing_mod.bake(
            start, start + duration, from_value, target["to"], easing,
            fps=self._fps(), max_keys=MAX_BAKED_KEYS,
        )
        return self._send_keyframes(target, keys, relative_to, replace=False,
                                    baked=True)

    def _expand_keyframe_bake(self, params: dict) -> Any:
        target = self._normalise_target(params)
        relative_to = target.get("relative_to", "clip")
        from_value = target.get("from_value")
        if from_value is None:
            from_value = self._read_value(target, at=target["start"],
                                          relative_to=relative_to)

        keys = easing_mod.bake(
            target["start"], target["end"], from_value, target["to_value"],
            target.get("easing", "linear"),
            fps=self._fps(),
            resolution=target.get("resolution", 0),
            max_keys=MAX_BAKED_KEYS,
        )
        return self._send_keyframes(target, keys, relative_to,
                                    replace=target.get("replace", True),
                                    baked=True)

    def _read_value(self, target: dict, *, at: float, relative_to: str):
        """Current value of a parameter, for animations with no explicit start."""
        payload = {k: target[k] for k in ("clip", "component", "property", "index")
                   if k in target}
        payload["at"] = at
        payload["relative_to"] = relative_to
        result = self.bridge.call("property.get", payload) or {}
        value = result.get("value")
        if value is None:
            raise HostError(
                f"Could not read the current value of "
                f"{target.get('component', MOTION)} > {target['property']}",
                hint="Pass an explicit 'from' value, or check caps.params for the "
                     "real parameter name on this clip",
            )
        return value

    def _send_keyframes(self, target: dict, keyframes: list, relative_to: str,
                        *, replace: bool, baked: bool = False) -> Any:
        payload = {k: target[k] for k in ("clip", "component", "property", "index")
                   if k in target}
        payload["keyframes"] = keyframes
        payload["relative_to"] = relative_to
        payload["replace"] = replace
        result = self.bridge.call("keyframe.set", payload)
        if baked and isinstance(result, dict):
            result["baked"] = len(keyframes)
            result["note"] = (
                "Curve baked into keyframes -- Premiere's scripting API does not "
                "expose graph-editor velocity handles."
            )
        return result

    # ------------------------------------------------------------------
    # Audio expansions
    # ------------------------------------------------------------------

    def _expand_audio_gain(self, params: dict) -> Any:
        payload = {
            "clip": params["clip"],
            "component": VOLUME_COMPONENT,
            "property": "Level",
            "value": params["db"],
        }
        if "at" in params:
            payload["at"] = params["at"]
            payload["relative_to"] = params.get("relative_to", "clip")
        return self.bridge.call("property.set", payload)

    def _expand_audio_pan(self, params: dict) -> Any:
        payload = {
            "clip": params["clip"],
            "component": "Panner",
            "property": "Balance",
            "value": params["pan"],
        }
        if "at" in params:
            payload["at"] = params["at"]
        return self.bridge.call("property.set", payload)

    def _expand_audio_fade(self, params: dict) -> Any:
        """Fades are level keyframes at the clip edges.

        Doing this by resolving the clip's real duration (rather than assuming)
        is what makes "fade out the last second" correct on a clip that has
        been trimmed or retimed.
        """
        clips = self._resolve_clips(params["clip"])
        floor_db = params.get("floor_db", -60.0)
        curve = params.get("easing", "ease_in_out")
        fade_in = params.get("in")
        fade_out = params.get("out")
        results = []

        for clip in clips:
            duration = float(clip.get("duration") or 0.0)
            if duration <= 0:
                continue
            keys: list = []
            if fade_in:
                length = min(float(fade_in), duration)
                keys += easing_mod.db_ramp(0.0, length, floor_db, 0.0, curve,
                                           fps=self._fps())
            if fade_out:
                length = min(float(fade_out), duration)
                start = max(0.0, duration - length)
                if keys and start <= keys[-1]["time"]:
                    start = keys[-1]["time"] + 1.0 / max(1.0, self._fps())
                keys += easing_mod.db_ramp(start, duration, 0.0, floor_db, curve,
                                           fps=self._fps())
            if not keys:
                continue
            results.append(self.bridge.call("keyframe.set", {
                "clip": {"id": clip["id"]},
                "component": VOLUME_COMPONENT,
                "property": "Level",
                "keyframes": keys,
                "relative_to": "clip",
                "replace": False,
            }))
        return {"clips": len(results), "results": results}

    def _expand_audio_duck(self, params: dict) -> Any:
        """Write a ducking envelope under a set of speech ranges."""
        clips = self._resolve_clips(params["clip"])
        clip = clips[0]
        clip_start = float(clip.get("start") or 0.0)
        duration = float(clip.get("duration") or 0.0)

        windows = _merge_windows(
            [(w["start"], w["end"]) for w in params["under"]],
            params.get("merge_gap", 0.6),
        )
        base_db = params.get("base_db", 0.0)
        duck_db = params.get("duck_db", -14.0)
        attack = params.get("attack", 0.25)
        release = params.get("release", 0.45)

        keys: list = [{"time": 0.0, "value": base_db}]
        for start, end in windows:
            # Convert sequence time to clip-relative, and drop windows that do
            # not overlap this clip at all.
            local_start = start - clip_start
            local_end = end - clip_start
            if local_end <= 0 or (duration and local_start >= duration):
                continue
            dip_start = max(0.0, local_start - attack)
            dip_end = local_start
            rise_start = local_end
            rise_end = local_end + release
            if duration:
                rise_end = min(rise_end, duration)
                rise_start = min(rise_start, duration)
            keys += [
                {"time": dip_start, "value": base_db},
                {"time": max(dip_end, dip_start + 0.01), "value": duck_db},
                {"time": max(rise_start, dip_end + 0.01), "value": duck_db},
                {"time": max(rise_end, rise_start + 0.01), "value": base_db},
            ]
        if duration:
            keys.append({"time": duration, "value": base_db})

        keys = _dedupe_keys(keys)
        result = self.bridge.call("keyframe.set", {
            "clip": {"id": clip["id"]},
            "component": VOLUME_COMPONENT,
            "property": "Level",
            "keyframes": keys,
            "relative_to": "clip",
            "replace": True,
        })
        return {"ducked_windows": len(windows), "keyframes": len(keys),
                "result": result}

    # ------------------------------------------------------------------
    # Speed
    # ------------------------------------------------------------------

    def _expand_clip_speed_ramp(self, params: dict) -> Any:
        """Speed ramps via Time Remapping keyframes.

        ``smooth`` widens each speed change into a short blend by inserting
        intermediate keyframes -- the scriptable stand-in for dragging the
        split-keyframe handles the graph editor offers.
        """
        points = params["points"]
        smooth = params.get("smooth", True)
        blend = params.get("transition", 0.4)

        keys: list = []
        for i, point in enumerate(points):
            time_at = point["time"]
            # Premiere expresses Time Remapping > Speed as a percentage.
            value = float(point["rate"]) * 100.0
            if smooth and i > 0:
                half = blend / 2.0
                previous = float(points[i - 1]["rate"]) * 100.0
                ramp_start = max(0.0, time_at - half)
                ramp_end = time_at + half
                keys += easing_mod.bake(
                    ramp_start, ramp_end, previous, value, "ease_in_out",
                    fps=self._fps(), resolution=12, max_keys=60,
                )
            else:
                keys.append({"time": time_at, "value": value})

        keys = _dedupe_keys(keys)
        result = self.bridge.call("keyframe.set", {
            "clip": params["clip"],
            "component": "Time Remapping",
            "property": "Speed",
            "keyframes": keys,
            "relative_to": "clip",
            "replace": True,
        })
        return {"points": len(points), "keyframes": len(keys), "result": result}

    # ------------------------------------------------------------------
    # Colour
    # ------------------------------------------------------------------

    def _expand_color_grade(self, params: dict) -> Any:
        values: dict = {}
        for key, premiere_name in LUMETRI_PARAMS.items():
            if key in params:
                values[premiere_name] = params[key]
        values.update(params.get("extra", {}) or {})
        if not values:
            raise ValidationError("color.grade needs at least one parameter")
        return self.bridge.call("effect.apply", {
            "clip": params["clip"],
            "effect": LUMETRI_EFFECT,
            "params": values,
            "reuse": True,
        })

    def _expand_color_lut(self, params: dict) -> Any:
        path = params["path"]
        if not str(path).lower().endswith(".cube"):
            raise ValidationError(
                "LUTs must be .cube files", path="path"
            )
        payload = {
            "clip": params["clip"],
            "effect": LUMETRI_EFFECT,
            "lut": {"path": path, "slot": params.get("slot", "creative")},
            "reuse": True,
        }
        if "intensity" in params:
            payload["params"] = {"Intensity": params["intensity"]}
        return self.bridge.call("color.lut", payload)

    # ------------------------------------------------------------------
    # Text and graphics
    # ------------------------------------------------------------------

    def _style_params(self, params: dict) -> dict:
        """Merge a named style with per-call overrides (overrides win)."""
        from premiere.styles import get_style
        style_name = params.get("style")
        merged: dict = dict(get_style(style_name) if style_name else {})
        for key in catalog.TYPOGRAPHY:
            if key in params:
                merged[key] = params[key]
        return merged

    def _expand_text_style(self, params: dict) -> Any:
        from premiere.styles import define_style, delete_style, list_styles
        name = params.get("name")
        if not name:
            return {"styles": list_styles()}
        if params.get("delete"):
            return {"deleted": delete_style(name)}
        style = {k: params[k] for k in catalog.TYPOGRAPHY if k in params}
        define_style(name, style)
        return {"defined": name, "style": style}

    def _expand_text_create(self, params: dict) -> Any:
        engine = params.get("engine", "auto")
        if engine == "mogrt":
            return self._text_via_mogrt(params)
        if engine == "auto":
            from premiere.styles import default_mogrt
            if default_mogrt():
                try:
                    return self._text_via_mogrt(params)
                except PremiereError as exc:
                    logger.warning("MOGRT text failed (%s); rendering instead", exc)
        return self._text_via_render(params)

    def _text_via_mogrt(self, params: dict) -> Any:
        from premiere.styles import default_mogrt
        template = default_mogrt()
        if not template:
            raise UnsupportedError(
                "No .mogrt template is configured for live text",
                alternative="use engine='render' for a rendered text overlay, or "
                            "register a template with premiere.styles.set_default_mogrt()",
            )
        payload = {
            "path": template,
            "time": params.get("time", 0.0),
            "duration": params.get("duration", 4.0),
            "params": {"Text": params["text"]},
        }
        if "track" in params:
            payload["track"] = params["track"]
        return self.bridge.call("mogrt.import", payload)

    def _text_via_render(self, params: dict) -> Any:
        style = self._style_params(params)
        rendered = render_text(
            params["text"],
            frame_size=self._frame_size(),
            font=style.get("font", "Arial"),
            size=style.get("size", 72.0),
            weight=style.get("weight", "bold"),
            italic=style.get("italic", False),
            color=style.get("color", "#FFFFFF"),
            align=style.get("align", "center"),
            line_spacing=style.get("line_spacing", 1.15),
            letter_spacing=style.get("letter_spacing", 0.0),
            max_width=style.get("max_width"),
            stroke_color=style.get("stroke_color"),
            stroke_width=style.get("stroke_width", 0.0),
            shadow=style.get("shadow"),
            background=style.get("background"),
        )
        result = self._place_overlay(
            rendered["path"],
            params,
            kind="text",
            name=_overlay_name(params["text"]),
        )
        result["rendered"] = rendered
        result["engine"] = "render"
        result["note"] = (
            "Rendered as an image overlay: Premiere's scripting API cannot create "
            "a native Essential Graphics text layer. The clip animates like any "
            "other -- use animate / keyframe.* on it."
        )
        return result

    def _expand_text_set(self, params: dict) -> Any:
        """Re-render an existing rendered text clip in place."""
        clips = self._resolve_clips(params["clip"])
        clip = clips[0]
        if not params.get("text"):
            raise ValidationError(
                "text.set needs the new 'text'",
                hint="styling-only edits require re-creating the overlay",
            )
        style = self._style_params(params)
        rendered = render_text(
            params["text"], frame_size=self._frame_size(),
            font=style.get("font", "Arial"), size=style.get("size", 72.0),
            weight=style.get("weight", "bold"), italic=style.get("italic", False),
            color=style.get("color", "#FFFFFF"), align=style.get("align", "center"),
            line_spacing=style.get("line_spacing", 1.15),
            letter_spacing=style.get("letter_spacing", 0.0),
            max_width=style.get("max_width"),
            stroke_color=style.get("stroke_color"),
            stroke_width=style.get("stroke_width", 0.0),
            shadow=style.get("shadow"), background=style.get("background"),
        )
        return self.bridge.call("clip.replace_source", {
            "clip": {"id": clip["id"]},
            "path": rendered["path"],
            "name": _overlay_name(params["text"]),
        })

    def _expand_graphic_shape(self, params: dict) -> Any:
        rendered = render_shape(
            params["shape"],
            frame_size=self._frame_size(),
            size=params.get("size", [0.3, 0.1]),
            fill=params.get("fill", "#FFFFFF"),
            stroke_color=params.get("stroke_color"),
            stroke_width=params.get("stroke_width", 0.0),
            radius=params.get("radius", 0.0),
            rotation=params.get("rotation", 0.0),
            points=params.get("points"),
            sides=params.get("sides"),
        )
        result = self._place_overlay(rendered["path"], params, kind="shape",
                                     name=f"shape-{params['shape']}")
        result["rendered"] = rendered
        return result

    def _expand_graphic_image(self, params: dict) -> Any:
        return self._place_overlay(params["path"], params, kind="image",
                                   name=os.path.basename(str(params["path"])))

    def _place_overlay(self, path: str, params: dict, *, kind: str,
                       name: str) -> dict:
        """Import an image and drop it on the timeline with its transform set.

        One host round trip: importing, placing and transforming in a single
        primitive keeps the clip from ever being visible in a half-configured
        state, which matters when the user is watching the timeline.
        """
        payload = {
            "path": path,
            "name": name,
            "kind": kind,
            "time": params.get("time", 0.0),
            "duration": params.get("duration", 4.0),
        }
        if "track" in params:
            payload["track"] = params["track"]

        transform: dict = {}
        if params.get("position"):
            transform["position"] = params["position"]
        if params.get("scale") is not None:
            transform["scale"] = params["scale"]
        if params.get("rotation") and kind != "shape":
            transform["rotation"] = params["rotation"]
        if params.get("opacity") is not None:
            transform["opacity"] = params["opacity"]
        if transform:
            payload["transform"] = transform

        return self.bridge.call("overlay.place", payload) or {}

    def _expand_adjustment_create(self, params: dict) -> Any:
        payload = {
            "time": params.get("time", 0.0),
            "duration": params["duration"],
        }
        if "track" in params:
            payload["track"] = params["track"]
        result = self.bridge.call("adjustment.create", payload) or {}

        effects = params.get("effects") or []
        if effects and result.get("clip_id"):
            applied = []
            for effect in effects:
                applied.append(self.bridge.call("effect.apply", {
                    "clip": {"id": result["clip_id"]},
                    "effect": effect["effect"],
                    "params": effect.get("params", {}),
                }))
            result["effects"] = applied
        elif effects and result.get("fallback") == "per_clip":
            # The host could not synthesise an adjustment layer, so apply the
            # same effects to every clip in the range instead and say so.
            applied = []
            for effect in effects:
                applied.append(self.bridge.call("effect.apply", {
                    "clip": {"track": params.get("track", {"type": "video", "index": 0}),
                             "range": [params.get("time", 0.0),
                                       params.get("time", 0.0) + params["duration"]]},
                    "effect": effect["effect"],
                    "params": effect.get("params", {}),
                }))
            result["effects"] = applied
            result["note"] = (
                "This Premiere build could not create an adjustment layer by "
                "script; the effects were applied to each clip in the range "
                "instead."
            )
        return result

    # ------------------------------------------------------------------
    # Captions
    # ------------------------------------------------------------------

    def _expand_captions_build(self, params: dict) -> Any:
        segments = params["segments"]
        mode = params.get("mode", "caption")
        max_chars = params.get("max_chars", 42)

        if mode == "caption":
            path = write_srt(segments, max_chars=max_chars)
            try:
                result = self.bridge.call("captions.import",
                                          {"path": path, "time": 0.0})
                return {"mode": "caption", "segments": len(segments),
                        "srt": path, "result": result}
            except UnsupportedError as exc:
                raise UnsupportedError(
                    "This Premiere build cannot create a caption track by script",
                    alternative="use mode='graphic' for styled subtitle overlays "
                                f"(the .srt was still written to {path} and can be "
                                "imported by hand)",
                    detail={"srt": path, "host_reason": exc.message},
                ) from None

        style = self._style_params(params)
        position = params.get("position", [0.5, 0.82])
        track = params.get("track")
        placed = []
        for segment in segments:
            text = "\n".join(split_segment_text(segment["text"], max_chars))
            rendered = render_text(
                text, frame_size=self._frame_size(),
                font=style.get("font", "Arial"),
                size=style.get("size", 54.0),
                weight=style.get("weight", "bold"),
                italic=style.get("italic", False),
                color=style.get("color", "#FFFFFF"),
                align=style.get("align", "center"),
                line_spacing=style.get("line_spacing", 1.15),
                letter_spacing=style.get("letter_spacing", 0.0),
                max_width=style.get("max_width", 0.8),
                stroke_color=style.get("stroke_color", "#000000"),
                stroke_width=style.get("stroke_width", 3.0),
                shadow=style.get("shadow"),
                background=style.get("background"),
            )
            payload = {
                "path": rendered["path"],
                "name": _overlay_name(segment["text"]),
                "kind": "caption",
                "time": segment["start"],
                "duration": max(0.05, segment["end"] - segment["start"]),
                "transform": {"position": position},
            }
            if track:
                payload["track"] = track
            placed.append(self.bridge.call("overlay.place", payload))
        return {"mode": "graphic", "segments": len(segments), "placed": len(placed)}

    def _expand_captions_import(self, params: dict) -> Any:
        path = params["path"]
        if not os.path.isabs(path):
            raise ValidationError(
                "subtitle path must be absolute", path="path"
            )
        return self.bridge.call("captions.import", params)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _merge_windows(windows: list, gap: float) -> list:
    """Merge overlapping/near speech ranges so ducking does not flutter."""
    ordered = sorted((float(a), float(b)) for a, b in windows)
    merged: list = []
    for start, end in ordered:
        if merged and start - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _dedupe_keys(keys: list) -> list:
    """Sort keyframes and drop ones that collide in time.

    Premiere silently overwrites a keyframe written at an existing time, so
    duplicates turn into "my animation lost a step" bugs that are painful to
    diagnose from the timeline. Cheaper to resolve here.
    """
    ordered = sorted(keys, key=lambda k: k["time"])
    out: list = []
    for key in ordered:
        if out and abs(key["time"] - out[-1]["time"]) < 1e-6:
            out[-1] = key
        else:
            out.append(key)
    return out


def _overlay_name(text: str, limit: int = 40) -> str:
    flat = " ".join(str(text).split())
    return (flat[:limit] + "...") if len(flat) > limit else (flat or "overlay")


engine = EditEngine()
