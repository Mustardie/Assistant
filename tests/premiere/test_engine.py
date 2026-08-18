"""Engine expansion, batching and failure handling.

These assert on the exact primitive operations the CEP panel would receive.
They cannot prove Premiere accepts them -- only a real host can -- but they do
prove that our half of the contract is right: correct times, correct values,
correct ordering, correct error classification.
"""
from __future__ import annotations

import pytest

from premiere.errors import HostError, RevisionError, UnsupportedError


class TestAnimate:
    def test_simple_easing_stays_native(self, engine, bridge):
        """ease_out maps onto a Premiere interpolation mode, so no baking."""
        result = engine.run({"ops": [{
            "op": "animate", "clip": {"track": "V1", "index": 0},
            "property": "Scale", "to": 130, "duration": 1.0, "easing": "ease_out",
        }]})
        assert result["success"], result
        payload = bridge.payload("keyframe.set")
        assert len(payload["keyframes"]) == 2
        assert payload["keyframes"][-1]["value"] == 130
        assert payload["keyframes"][-1]["interp"] == "ease_out"

    def test_exotic_easing_is_baked(self, engine, bridge):
        """bounce_out has no native mode, so it must become dense keyframes."""
        result = engine.run({"ops": [{
            "op": "animate", "clip": {"track": "V1", "index": 0},
            "property": "Scale", "to": 130, "duration": 1.0,
            "easing": "bounce_out", "from": 100,
        }]})
        assert result["success"]
        payload = bridge.payload("keyframe.set")
        assert len(payload["keyframes"]) > 20
        assert payload["keyframes"][0]["value"] == pytest.approx(100)
        assert payload["keyframes"][-1]["value"] == pytest.approx(130)

    def test_custom_bezier_is_baked(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "animate", "clip": {"track": "V1", "index": 0},
            "property": "Opacity", "to": 0, "from": 100, "duration": 0.5,
            "easing": [0.6, 0.0, 0.4, 1.0],
        }]})
        assert result["success"]
        assert len(bridge.payload("keyframe.set")["keyframes"]) > 5

    def test_missing_from_value_is_read_from_the_host(self, engine, bridge):
        engine.run({"ops": [{
            "op": "animate", "clip": {"track": "V1", "index": 0},
            "property": "Scale", "to": 200, "duration": 1.0,
            "easing": "bounce_in",
        }]})
        assert "property.get" in bridge.ops()
        # 100.0 is what the fake host reports as the current value.
        assert bridge.payload("keyframe.set")["keyframes"][0]["value"] == \
            pytest.approx(100.0)

    def test_property_alias_resolves_to_a_component(self, engine, bridge):
        engine.run({"ops": [{
            "op": "animate", "clip": {"track": "V1", "index": 0},
            "property": "opacity", "to": 0, "duration": 1.0,
        }]})
        payload = bridge.payload("keyframe.set")
        assert payload["component"] == "Opacity"
        assert payload["property"] == "Opacity"

    def test_position_alias_maps_to_motion(self, engine, bridge):
        engine.run({"ops": [{
            "op": "animate", "clip": {"track": "V1", "index": 0},
            "property": "position", "to": [0.5, 0.2], "from": [0.5, 0.5],
            "duration": 1.0, "easing": "ease_in_out",
        }]})
        assert bridge.payload("keyframe.set")["component"] == "Motion"

    def test_explicit_component_is_not_overridden(self, engine, bridge):
        engine.run({"ops": [{
            "op": "animate", "clip": {"track": "V1", "index": 0},
            "component": "Gaussian Blur", "property": "Blurriness",
            "to": 40, "from": 0, "duration": 1.0,
        }]})
        assert bridge.payload("keyframe.set")["component"] == "Gaussian Blur"


class TestBake:
    def test_bake_reports_that_it_baked(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "keyframe.bake", "clip": {"track": "V1", "index": 0},
            "property": "Rotation", "start": 0.0, "end": 1.0,
            "from_value": 0, "to_value": 360, "easing": "elastic_out",
        }]})
        assert result["success"]
        detail = result["results"][0]["result"]
        assert detail["baked"] > 10
        assert "graph-editor" in detail["note"]

    def test_bake_honours_resolution(self, engine, bridge):
        engine.run({"ops": [{
            "op": "keyframe.bake", "clip": {"track": "V1", "index": 0},
            "property": "Opacity", "start": 0.0, "end": 2.0,
            "from_value": 0, "to_value": 100, "easing": "ease_in",
            "resolution": 5,
        }]})
        assert len(bridge.payload("keyframe.set")["keyframes"]) == 11


class TestAudio:
    def test_gain_is_a_volume_level_set(self, engine, bridge):
        engine.run({"ops": [{
            "op": "audio.gain", "clip": {"track": "A1", "index": 0}, "db": -6,
        }]})
        payload = bridge.payload("property.set")
        assert payload["component"] == "Volume"
        assert payload["property"] == "Level"
        assert payload["value"] == -6

    def test_fade_uses_the_clips_real_duration(self, engine, bridge):
        """The fake clip is 10s long, so a 2s fade-out starts at 8s."""
        engine.run({"ops": [{
            "op": "audio.fade", "clip": {"track": "A1", "index": 0},
            "in": 1.0, "out": 2.0,
        }]})
        keys = bridge.payload("keyframe.set")["keyframes"]
        times = [k["time"] for k in keys]
        assert min(times) == pytest.approx(0.0)
        assert max(times) == pytest.approx(10.0)
        assert any(t == pytest.approx(8.0, abs=0.05) for t in times)

    def test_fade_in_rises_from_the_floor(self, engine, bridge):
        engine.run({"ops": [{
            "op": "audio.fade", "clip": {"track": "A1", "index": 0},
            "in": 1.0, "floor_db": -60,
        }]})
        keys = bridge.payload("keyframe.set")["keyframes"]
        assert keys[0]["value"] == pytest.approx(-60)
        assert keys[-1]["value"] == pytest.approx(0.0)

    def test_duck_writes_an_envelope_around_speech(self, engine, bridge):
        engine.run({"ops": [{
            "op": "audio.duck", "clip": {"track": "A2", "index": 0},
            "under": [{"start": 12.0, "end": 15.0}],
            "duck_db": -18, "base_db": 0,
        }]})
        keys = bridge.payload("keyframe.set")["keyframes"]
        values = [k["value"] for k in keys]
        assert -18 in values, "should dip to the duck level"
        assert 0 in values, "should return to the base level"

    def test_duck_converts_sequence_time_to_clip_time(self, engine, bridge):
        """The fake clip starts at 10s, so speech at 12s is 2s into the clip."""
        engine.run({"ops": [{
            "op": "audio.duck", "clip": {"track": "A2", "index": 0},
            "under": [{"start": 12.0, "end": 15.0}],
            "attack": 0.5, "release": 0.5,
        }]})
        keys = bridge.payload("keyframe.set")["keyframes"]
        ducked = [k["time"] for k in keys if k["value"] < -1]
        assert min(ducked) == pytest.approx(2.0, abs=0.01)

    def test_duck_merges_nearby_speech(self, engine, bridge):
        """Two ranges 0.2s apart must not produce a level flutter between them."""
        engine.run({"ops": [{
            "op": "audio.duck", "clip": {"track": "A2", "index": 0},
            "under": [{"start": 11.0, "end": 12.0},
                      {"start": 12.2, "end": 13.0}],
            "merge_gap": 0.6,
        }]})
        result = bridge.payload("keyframe.set")
        rises = [k for k in result["keyframes"] if k["value"] == 0]
        # One recovery at the end of the merged window, plus the boundary keys.
        assert len(rises) <= 4

    def test_duck_keyframes_are_strictly_ordered(self, engine, bridge):
        engine.run({"ops": [{
            "op": "audio.duck", "clip": {"track": "A2", "index": 0},
            "under": [{"start": 11.0, "end": 12.0}, {"start": 14.0, "end": 15.0}],
        }]})
        times = [k["time"] for k in bridge.payload("keyframe.set")["keyframes"]]
        assert times == sorted(times)
        assert len(set(times)) == len(times), "duplicate keyframe times"


class TestSpeedRamp:
    def test_ramp_targets_time_remapping(self, engine, bridge):
        engine.run({"ops": [{
            "op": "clip.speed_ramp", "clip": {"track": "V1", "index": 0},
            "points": [{"time": 0.0, "rate": 1.0}, {"time": 2.0, "rate": 0.25}],
        }]})
        payload = bridge.payload("keyframe.set")
        assert payload["component"] == "Time Remapping"
        assert payload["property"] == "Speed"

    def test_rates_become_percentages(self, engine, bridge):
        engine.run({"ops": [{
            "op": "clip.speed_ramp", "clip": {"track": "V1", "index": 0},
            "points": [{"time": 0.0, "rate": 1.0}, {"time": 2.0, "rate": 2.0}],
            "smooth": False,
        }]})
        values = [k["value"] for k in bridge.payload("keyframe.set")["keyframes"]]
        assert 100.0 in values
        assert 200.0 in values

    def test_smooth_ramp_blends(self, engine, bridge):
        engine.run({"ops": [{
            "op": "clip.speed_ramp", "clip": {"track": "V1", "index": 0},
            "points": [{"time": 0.0, "rate": 1.0}, {"time": 2.0, "rate": 0.25}],
            "smooth": True, "transition": 0.6,
        }]})
        keys = bridge.payload("keyframe.set")["keyframes"]
        assert len(keys) > 3, "a smooth ramp needs intermediate keyframes"
        values = [k["value"] for k in keys]
        assert any(30 < v < 99 for v in values), "expected a blend region"


class TestColor:
    def test_grade_maps_friendly_names_to_lumetri(self, engine, bridge):
        engine.run({"ops": [{
            "op": "color.grade", "clip": {"track": "V1", "index": 0},
            "exposure": 0.3, "contrast": 12, "saturation": 115,
        }]})
        payload = bridge.payload("effect.apply")
        assert payload["effect"] == "Lumetri Color"
        assert payload["params"]["Exposure"] == 0.3
        assert payload["params"]["Contrast"] == 12
        assert payload["params"]["Saturation"] == 115
        assert payload["reuse"] is True, "must not stack a second Lumetri"

    def test_extra_params_pass_through(self, engine, bridge):
        engine.run({"ops": [{
            "op": "color.grade", "clip": {"track": "V1", "index": 0},
            "exposure": 0.1, "extra": {"Sharpen": 20},
        }]})
        assert bridge.payload("effect.apply")["params"]["Sharpen"] == 20

    def test_lut_requires_a_cube_file(self, engine):
        result = engine.run({"ops": [{
            "op": "color.lut", "clip": {"track": "V1", "index": 0},
            "path": "/luts/look.png",
        }]})
        assert not result["success"]
        assert ".cube" in result["results"][0]["error"]


class TestTextAndGraphics:
    def test_text_renders_and_places_an_overlay(self, engine, bridge, tmp_path):
        result = engine.run({"ops": [{
            "op": "text.create", "text": "Hello World", "time": 1.0,
            "duration": 3.0, "size": 80, "color": "#FFCC00",
        }]})
        assert result["success"], result
        payload = bridge.payload("overlay.place")
        assert payload["kind"] == "text"
        assert payload["time"] == 1.0
        assert payload["duration"] == 3.0
        assert payload["path"].endswith(".png")

    def test_text_reports_the_engine_it_used(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "text.create", "text": "Title", "duration": 2.0,
        }]})
        detail = result["results"][0]["result"]
        assert detail["engine"] == "render"
        assert "Essential Graphics" in detail["note"]

    def test_named_style_is_applied(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "text.create", "text": "Styled", "style": "title",
            "duration": 2.0,
        }]})
        assert result["success"]
        assert result["results"][0]["result"]["rendered"]["path"].endswith(".png")

    def test_shape_places_an_overlay(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "graphic.shape", "shape": "rounded_rect",
            "size": [0.4, 0.12], "fill": "#101010", "radius": 16,
            "position": [0.5, 0.85], "duration": 3.0,
        }]})
        assert result["success"], result
        payload = bridge.payload("overlay.place")
        assert payload["kind"] == "shape"
        assert payload["transform"]["position"] == [0.5, 0.85]

    def test_captions_graphic_mode_places_one_clip_per_segment(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "captions.build", "mode": "graphic",
            "segments": [
                {"start": 0.0, "end": 1.5, "text": "first line"},
                {"start": 1.5, "end": 3.0, "text": "second line"},
                {"start": 3.0, "end": 4.2, "text": "third line"},
            ],
        }]})
        assert result["success"], result
        assert len(bridge.payloads("overlay.place")) == 3

    def test_captions_caption_mode_writes_srt(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "captions.build", "mode": "caption",
            "segments": [{"start": 0.0, "end": 2.0, "text": "hello"}],
        }]})
        assert result["success"], result
        assert bridge.payload("captions.import")["path"].endswith(".srt")

    def test_caption_mode_falls_back_with_a_usable_srt(self, engine, bridge):
        """When the host cannot make a caption track, the .srt must survive."""
        bridge.fail("captions.import",
                    UnsupportedError("no caption API on this build"))
        result = engine.run({"ops": [{
            "op": "captions.build", "mode": "caption",
            "segments": [{"start": 0.0, "end": 2.0, "text": "hello"}],
        }]})
        assert not result["success"]
        failure = result["results"][0]
        assert failure["code"] == "unsupported"
        assert failure["detail"]["srt"].endswith(".srt")
        assert "graphic" in failure["alternative"]


class TestAdjustmentLayer:
    def test_effects_are_applied_to_the_new_layer(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "adjustment.create", "time": 0.0, "duration": 10.0,
            "effects": [{"effect": "Lumetri Color", "params": {"Exposure": 0.2}}],
        }]})
        assert result["success"]
        assert bridge.payload("effect.apply")["clip"] == {"id": "adj-1"}

    def test_per_clip_fallback_when_no_adjustment_layer_exists(self, engine, bridge):
        bridge.responses["adjustment.create"] = {
            "created": False, "fallback": "per_clip",
        }
        result = engine.run({"ops": [{
            "op": "adjustment.create", "time": 0.0, "duration": 10.0,
            "track": "V2",
            "effects": [{"effect": "Lumetri Color", "params": {"Exposure": 0.2}}],
        }]})
        assert result["success"]
        detail = result["results"][0]["result"]
        assert "could not create an adjustment layer" in detail["note"]
        # The fallback must target the whole time range, not one clip.
        assert bridge.payload("effect.apply")["clip"]["range"] == [0.0, 10.0]


class TestFailureHandling:
    def test_abort_stops_at_the_first_failure(self, engine, bridge):
        bridge.fail("clip.remove", HostError("track is locked"))
        result = engine.run({"ops": [
            {"op": "property.set", "clip": {"track": "V1", "index": 0},
             "property": "Opacity", "value": 50},
            {"op": "clip.remove", "clip": {"track": "V1", "index": 1}},
            {"op": "property.set", "clip": {"track": "V1", "index": 2},
             "property": "Opacity", "value": 50},
        ]})
        assert not result["success"]
        assert result["applied"] == 1
        assert result["total"] == 3
        assert len(result["results"]) == 2, "third op must not have run"

    def test_partial_failure_warns_about_double_applying(self, engine, bridge):
        bridge.fail("clip.remove", HostError("track is locked"))
        result = engine.run({"ops": [
            {"op": "property.set", "clip": {"track": "V1", "index": 0},
             "property": "Opacity", "value": 50},
            {"op": "clip.remove", "clip": {"track": "V1", "index": 1}},
        ]})
        assert result["partial"] is True
        assert "double-apply" in result["note"]

    def test_continue_runs_everything(self, engine, bridge):
        bridge.fail("clip.remove", HostError("track is locked"))
        result = engine.run({"ops": [
            {"op": "clip.remove", "clip": {"track": "V1", "index": 0}},
            {"op": "property.set", "clip": {"track": "V1", "index": 1},
             "property": "Opacity", "value": 50},
        ], "on_error": "continue"})
        assert not result["success"]
        assert result["applied"] == 1
        assert len(result["results"]) == 2

    def test_rollback_restores_the_checkpoint(self, engine, bridge):
        bridge.fail("clip.remove", HostError("track is locked"))
        result = engine.run({"ops": [
            {"op": "property.set", "clip": {"track": "V1", "index": 0},
             "property": "Opacity", "value": 50},
            {"op": "clip.remove", "clip": {"track": "V1", "index": 1}},
        ], "on_error": "rollback"})
        assert result["rolled_back"] is True
        assert "project.restore" in bridge.ops()

    def test_error_names_the_failing_operation_position(self, engine, bridge):
        bridge.fail("clip.remove", HostError("track is locked"))
        result = engine.run({"ops": [
            {"op": "sequence.info"},
            {"op": "clip.remove", "clip": {"track": "V1", "index": 0}},
        ]})
        assert "operation 2 of 2" in result["error"]

    def test_validation_failure_touches_nothing(self, engine, bridge):
        result = engine.run({"ops": [{"op": "clip.nonexistent"}]})
        assert not result["success"]
        assert result["code"] == "validation_error"
        assert "clip.nonexistent" not in bridge.ops()

    def test_disconnected_host_reports_setup_help(self, engine, bridge):
        bridge.connected = False
        result = engine.run({"ops": [{"op": "sequence.info"}]})
        assert not result["success"]
        assert "Premiere" in result["error"]


class TestSafety:
    def test_read_only_plan_takes_no_checkpoint(self, engine, bridge):
        engine.run({"ops": [{"op": "timeline.snapshot"}]})
        assert "project.checkpoint" not in bridge.ops()

    def test_mutating_plan_checkpoints_first(self, engine, bridge):
        engine.run({"ops": [{
            "op": "property.set", "clip": {"track": "V1", "index": 0},
            "property": "Opacity", "value": 50,
        }]})
        assert bridge.ops().index("project.checkpoint") < \
            bridge.ops().index("property.set")

    def test_stale_revision_is_refused(self, engine, bridge):
        result = engine.run({
            "revision": "an-old-revision",
            "ops": [{"op": "clip.remove", "clip": {"track": "V1", "index": 0}}],
        })
        assert not result["success"]
        assert result["code"] == "revision_mismatch"
        assert "clip.remove" not in bridge.ops()

    def test_matching_revision_proceeds(self, engine, bridge):
        result = engine.run({
            "revision": "rev1",
            "ops": [{"op": "clip.remove", "clip": {"track": "V1", "index": 0}}],
        })
        assert result["success"], result

    def test_history_records_the_batch(self, engine, bridge, history):
        engine.run({"ops": [{
            "op": "property.set", "clip": {"track": "V1", "index": 0},
            "property": "Opacity", "value": 50,
        }], "label": "opacity pass"})
        entries = history.recent()
        assert entries[0]["label"] == "opacity pass"
        assert entries[0]["ops"][0]["op"] == "property.set"

    def test_dry_run_changes_nothing(self, engine, bridge):
        result = engine.run({"dry_run": True, "ops": [
            {"op": "clip.remove", "clip": {"track": "V1", "index": 0}},
        ]})
        assert result["success"]
        assert result["dry_run"] is True
        assert result["plan"]
        assert "clip.remove" not in bridge.ops()


class TestBatching:
    def test_a_long_plan_runs_in_order(self, engine, bridge):
        ops = [{"op": "property.set", "clip": {"track": "V1", "index": i},
                "property": "Opacity", "value": 50} for i in range(12)]
        result = engine.run({"ops": ops})
        assert result["success"]
        assert result["applied"] == 12
        indices = [p["clip"]["index"] for p in bridge.payloads("property.set")]
        assert indices == list(range(12))


class TestBatchScheduling:
    def test_long_primitive_runs_go_in_one_request(self, engine, bridge):
        """12 pass-through ops should cost one round trip, not twelve."""
        sent = {}
        original = bridge.call_batch

        def spy(operations, **kw):
            sent["count"] = sent.get("count", 0) + 1
            sent["size"] = len(operations)
            return original(operations, **kw)

        bridge.call_batch = spy
        result = engine.run({"ops": [
            {"op": "property.set", "clip": {"track": "V1", "index": i},
             "property": "Opacity", "value": 50} for i in range(12)
        ]})
        assert result["success"]
        assert sent["count"] == 1
        assert sent["size"] == 12

    def test_expanding_ops_break_the_batch(self, engine, bridge):
        """animate needs its own handling, so it must not be folded into a run."""
        result = engine.run({"ops": [
            {"op": "property.set", "clip": {"track": "V1", "index": 0},
             "property": "Opacity", "value": 10},
            {"op": "property.set", "clip": {"track": "V1", "index": 1},
             "property": "Opacity", "value": 20},
            {"op": "property.set", "clip": {"track": "V1", "index": 2},
             "property": "Opacity", "value": 30},
            {"op": "property.set", "clip": {"track": "V1", "index": 3},
             "property": "Opacity", "value": 40},
            {"op": "animate", "clip": {"track": "V1", "index": 0},
             "property": "Scale", "to": 120, "duration": 1.0},
            {"op": "property.set", "clip": {"track": "V1", "index": 4},
             "property": "Opacity", "value": 50},
        ]})
        assert result["success"], result
        assert result["applied"] == 6
        assert [r["op"] for r in result["results"]] == [
            "property.set", "property.set", "property.set", "property.set",
            "animate", "property.set",
        ]

    def test_indices_stay_correct_across_a_batch(self, engine, bridge):
        result = engine.run({"ops": [
            {"op": "animate", "clip": {"track": "V1", "index": 0},
             "property": "Scale", "to": 120, "duration": 1.0},
        ] + [
            {"op": "property.set", "clip": {"track": "V1", "index": i},
             "property": "Opacity", "value": 50} for i in range(5)
        ]})
        assert [r["index"] for r in result["results"]] == [0, 1, 2, 3, 4, 5]

    def test_batch_failure_is_attributed_to_the_right_operation(self, engine, bridge):
        bridge.fail("clip.remove", HostError("track is locked"))
        result = engine.run({"ops": [
            {"op": "property.set", "clip": {"track": "V1", "index": 0},
             "property": "Opacity", "value": 10},
            {"op": "property.set", "clip": {"track": "V1", "index": 1},
             "property": "Opacity", "value": 20},
            {"op": "property.set", "clip": {"track": "V1", "index": 2},
             "property": "Opacity", "value": 30},
            {"op": "clip.remove", "clip": {"track": "V1", "index": 3}},
            {"op": "property.set", "clip": {"track": "V1", "index": 4},
             "property": "Opacity", "value": 50},
        ]})
        assert not result["success"]
        failed = [r for r in result["results"] if not r["success"]]
        assert len(failed) == 1
        assert failed[0]["op"] == "clip.remove"
        assert failed[0]["index"] == 3
        assert result["applied"] == 3

    def test_batch_rollback_still_works(self, engine, bridge):
        bridge.fail("clip.remove", HostError("locked"))
        result = engine.run({"ops": [
            {"op": "property.set", "clip": {"track": "V1", "index": i},
             "property": "Opacity", "value": 10} for i in range(4)
        ] + [{"op": "clip.remove", "clip": {"track": "V1", "index": 9}}],
            "on_error": "rollback"})
        assert result["rolled_back"] is True
        assert "project.restore" in bridge.ops()


class TestOfflineBehaviour:
    """The layer must be useful (and honest) with no Premiere in sight."""

    def test_dry_run_needs_no_host(self, history):
        # Regression: dry_run used to fetch the sequence frame rate first, so
        # it failed exactly when offline validation mattered most.
        from premiere.bridge import PremiereBridge
        from premiere.engine import EditEngine

        engine = EditEngine(bridge=PremiereBridge(port=1), history=history)
        result = engine.run({"dry_run": True, "ops": [
            {"op": "animate", "clip": {"track": "V1", "index": 0},
             "property": "Scale", "to": 120, "duration": 1.0},
        ]})
        assert result["success"] is True
        assert result["dry_run"] is True
        assert len(result["plan"]) == 1

    def test_validation_errors_surface_without_a_host(self, history):
        from premiere.bridge import PremiereBridge
        from premiere.engine import EditEngine

        engine = EditEngine(bridge=PremiereBridge(port=1), history=history)
        result = engine.run({"dry_run": True, "ops": [
            {"op": "animate", "clip": {"track": "V1", "index": 0},
             "property": "Scale", "to": 120},
        ]})
        assert result["code"] == "validation_error"
        assert "duration" in result["error"]

    def test_unreachable_host_gives_a_typed_error_not_a_crash(self, history):
        from premiere.bridge import PremiereBridge
        from premiere.engine import EditEngine

        engine = EditEngine(bridge=PremiereBridge(port=1), history=history)
        result = engine.run({"ops": [{"op": "timeline.snapshot"}]})
        assert result["success"] is False
        assert result["code"] == "bridge_error"
        assert "hint" in result


class TestTextStyles:
    """Regression: schema defaults used to override named styles silently."""

    def test_named_style_actually_changes_the_render(self, engine, bridge):
        """The 'title' style must reach the renderer, not be defaulted away."""
        engine.run({"ops": [{"op": "text.create", "text": "Same Words",
                             "duration": 2.0}]})
        plain = bridge.payload("overlay.place")["path"]

        bridge.calls.clear()
        engine.run({"ops": [{"op": "text.create", "text": "Same Words",
                             "style": "title", "duration": 2.0}]})
        styled = bridge.payload("overlay.place")["path"]

        # Paths are content-addressed, so identical paths mean identical pixels
        # -- which would mean the style did nothing.
        assert plain != styled, "named style had no effect on the render"

    def test_style_fields_survive_validation(self):
        from premiere.validator import validate_operation
        params = validate_operation({
            "op": "text.create", "text": "Hi", "style": "title", "duration": 2.0,
        })["params"]
        for field in ("font", "size", "weight", "color", "stroke_width"):
            assert field not in params, (
                f"{field!r} was defaulted in, which would override the style")

    def test_explicit_override_beats_the_style(self, engine, bridge):
        from premiere.engine import EditEngine
        merged = EditEngine._style_params(engine, {"style": "title", "size": 40})
        assert merged["size"] == 40
        assert merged["weight"] == "black", "unset fields still come from the style"

    def test_caption_defaults_are_not_clobbered(self, engine, bridge):
        """captions.build renders at its own defaults when nothing is specified."""
        engine.run({"ops": [{
            "op": "captions.build", "mode": "graphic",
            "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}],
        }]})
        assert len(bridge.payloads("overlay.place")) == 1

    def test_defining_a_style_then_using_it(self, engine, bridge, tmp_path,
                                            monkeypatch):
        import premiere.styles as styles_mod
        monkeypatch.setattr(styles_mod, "_PATH", str(tmp_path / "styles.json"))
        monkeypatch.setattr(styles_mod, "_cache", None, raising=False)

        engine.run({"ops": [{"op": "text.style", "name": "brand",
                             "size": 120, "color": "#00FFAA"}]})
        merged = engine._style_params({"style": "brand"})
        assert merged["size"] == 120
        assert merged["color"] == "#00FFAA"


class TestMarkers:
    def test_marker_add_passes_through(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "marker.add", "time": 12.5, "name": "beat",
            "type": "chapter",
        }]})
        assert result["success"], result
        payload = bridge.payload("marker.add")
        assert payload["time"] == 12.5
        assert payload["type"] == "chapter"

    def test_marker_list_is_read_only(self, engine, bridge):
        engine.run({"ops": [{"op": "marker.list"}]})
        assert "project.checkpoint" not in bridge.ops()

    def test_timecode_marker_time_is_converted(self, engine, bridge):
        engine.run({"ops": [{"op": "marker.add", "time": "00:00:10:15"}]})
        assert bridge.payload("marker.add")["time"] == pytest.approx(10.5)

    def test_marker_remove_needs_a_target(self):
        from premiere.errors import ValidationError as VE
        from premiere.validator import validate_operation
        with pytest.raises(VE, match="needs 'at', 'range', 'name', or all"):
            validate_operation({"op": "marker.remove"})

    def test_marker_remove_all_is_explicit(self):
        from premiere.validator import validate_operation
        out = validate_operation({"op": "marker.remove", "all": True})
        assert out["params"]["all"] is True


class TestCopyAttributes:
    def test_copies_from_one_clip_to_many(self, engine, bridge):
        result = engine.run({"ops": [{
            "op": "clip.copy_attributes",
            "from": {"track": "V1", "index": 0},
            "to": {"track": "V1", "range": [10.0, 60.0]},
        }]})
        assert result["success"], result
        payload = bridge.payload("clip.copy_attributes")
        assert payload["from"]["index"] == 0
        assert payload["to"]["range"] == [10.0, 60.0]

    def test_source_must_be_a_single_clip(self):
        from premiere.errors import ValidationError as VE
        from premiere.validator import validate_operation
        with pytest.raises(VE, match="copies from a single clip"):
            validate_operation({
                "op": "clip.copy_attributes",
                "from": {"track": "V1", "all": True},
                "to": {"track": "V2", "index": 0},
            })

    def test_include_filter_is_validated(self):
        from premiere.errors import ValidationError as VE
        from premiere.validator import validate_operation
        with pytest.raises(VE):
            validate_operation({
                "op": "clip.copy_attributes",
                "from": {"track": "V1", "index": 0},
                "to": {"track": "V1", "index": 1},
                "include": ["effects", "colour"],
            })

    def test_empty_include_is_rejected(self):
        from premiere.errors import ValidationError as VE
        from premiere.validator import validate_operation
        with pytest.raises(VE, match="nothing would be copied"):
            validate_operation({
                "op": "clip.copy_attributes",
                "from": {"track": "V1", "index": 0},
                "to": {"track": "V1", "index": 1},
                "include": [],
            })
