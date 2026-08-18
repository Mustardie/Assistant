"""EditPlan validation -- the gate that stands between a model and the timeline."""
from __future__ import annotations

import pytest

from premiere import catalog
from premiere.errors import UnsupportedError, ValidationError
from premiere.validator import explain, validate_operation, validate_plan


class TestPlanShape:
    def test_bare_list_is_accepted(self):
        plan = validate_plan([{"op": "timeline.snapshot"}])
        assert len(plan["ops"]) == 1

    def test_operations_alias(self):
        plan = validate_plan({"operations": [{"op": "sequence.info"}]})
        assert plan["ops"][0]["op"] == "sequence.info"

    def test_empty_plan_rejected(self):
        with pytest.raises(ValidationError, match="no operations"):
            validate_plan({"ops": []})

    def test_missing_ops_rejected(self):
        with pytest.raises(ValidationError, match="no 'ops'"):
            validate_plan({"label": "x"})

    def test_bad_on_error_mode(self):
        with pytest.raises(ValidationError, match="on_error"):
            validate_plan({"ops": [{"op": "sequence.info"}], "on_error": "panic"})

    def test_mutating_flag(self):
        readonly = validate_plan({"ops": [{"op": "timeline.snapshot"}]})
        assert readonly["mutating"] is False
        mutating = validate_plan({"ops": [
            {"op": "clip.remove", "clip": {"track": "V1", "index": 0}}]})
        assert mutating["mutating"] is True

    def test_revision_carried_through(self):
        plan = validate_plan({"ops": [{"op": "sequence.info"}], "revision": "r7"})
        assert plan["revision"] == "r7"


class TestOperationNames:
    def test_unknown_op_rejected(self):
        with pytest.raises(ValidationError, match="unknown operation"):
            validate_operation({"op": "clip.explode"})

    def test_typo_gets_a_suggestion(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_operation({"op": "clip.spilt", "clip": {"track": "V1", "index": 0},
                                "at": [1.0]})
        assert "clip.split" in str(excinfo.value)

    def test_missing_op_name(self):
        with pytest.raises(ValidationError, match="missing its 'op'"):
            validate_operation({"clip": {"track": "V1", "index": 0}})

    def test_unsupported_op_reports_alternative(self):
        with pytest.raises(UnsupportedError) as excinfo:
            validate_operation({"op": "color.curves", "clip": {"track": "V1", "index": 0}})
        assert "color.grade" in excinfo.value.alternative

    def test_note_is_preserved(self):
        out = validate_operation({"op": "sequence.info", "note": "check fps"})
        assert out["note"] == "check fps"


class TestSemanticChecks:
    def test_move_needs_a_destination(self):
        with pytest.raises(ValidationError, match="needs one of"):
            validate_operation({"op": "clip.move", "clip": {"track": "V1", "index": 0}})

    def test_trim_needs_by_or_to(self):
        with pytest.raises(ValidationError, match="needs one of"):
            validate_operation({"op": "clip.trim", "clip": {"track": "V1", "index": 0},
                                "edge": "out"})

    def test_trim_both_edges_rejects_absolute_time(self):
        with pytest.raises(ValidationError, match="cannot use an absolute"):
            validate_operation({"op": "clip.trim", "clip": {"track": "V1", "index": 0},
                                "edge": "both", "to": 5.0})

    def test_packed_property_path_is_caught(self):
        # "Motion > Scale" in one string is the most common model mistake.
        with pytest.raises(ValidationError) as excinfo:
            validate_operation({"op": "property.set",
                                "clip": {"track": "V1", "index": 0},
                                "property": "Motion > Scale", "value": 120})
        assert '"component": "Motion"' in str(excinfo.value)

    def test_duplicate_keyframe_times_rejected(self):
        with pytest.raises(ValidationError, match="same time"):
            validate_operation({
                "op": "keyframe.set", "clip": {"track": "V1", "index": 0},
                "property": "Opacity",
                "keyframes": [{"time": 1.0, "value": 0}, {"time": 1.0, "value": 100}],
            })

    def test_bake_window_must_be_forward(self):
        with pytest.raises(ValidationError, match="must be after"):
            validate_operation({
                "op": "keyframe.bake", "clip": {"track": "V1", "index": 0},
                "property": "Opacity", "start": 5.0, "end": 2.0, "to_value": 100,
            })

    def test_duck_range_must_be_ordered(self):
        with pytest.raises(ValidationError, match="ends before it starts"):
            validate_operation({
                "op": "audio.duck", "clip": {"track": "A2", "index": 0},
                "under": [{"start": 9.0, "end": 3.0}],
            })

    def test_duck_level_sanity(self):
        with pytest.raises(ValidationError, match="quieter"):
            validate_operation({
                "op": "audio.duck", "clip": {"track": "A2", "index": 0},
                "under": [{"start": 1.0, "end": 3.0}],
                "duck_db": 0.0, "base_db": -10.0,
            })

    def test_speed_ramp_points_must_ascend(self):
        with pytest.raises(ValidationError, match="increasing time order"):
            validate_operation({
                "op": "clip.speed_ramp", "clip": {"track": "V1", "index": 0},
                "points": [{"time": 2.0, "rate": 1.0}, {"time": 1.0, "rate": 2.0}],
            })

    def test_caption_segment_needs_text(self):
        with pytest.raises(ValidationError, match="no text"):
            validate_operation({
                "op": "captions.build",
                "segments": [{"start": 0.0, "end": 1.0, "text": "   "}],
            })

    def test_effect_remove_needs_a_target(self):
        with pytest.raises(ValidationError, match="needs an 'effect'"):
            validate_operation({"op": "effect.remove",
                                "clip": {"track": "V1", "index": 0}})

    def test_grade_needs_a_knob(self):
        with pytest.raises(ValidationError, match="at least one grading"):
            validate_operation({"op": "color.grade",
                                "clip": {"track": "V1", "index": 0}})

    def test_polygon_needs_points_or_sides(self):
        with pytest.raises(ValidationError, match="needs 'points' or 'sides'"):
            validate_operation({"op": "graphic.shape", "shape": "polygon"})


class TestMultiClipGuard:
    def test_single_clip_op_rejects_broad_selector(self):
        # clip.duplicate acts on one clip; a range selector would silently
        # duplicate only the first match.
        with pytest.raises(ValidationError, match="can match several"):
            validate_operation({"op": "clip.duplicate",
                                "clip": {"track": "V1", "range": [0, 10]},
                                "time": 20.0})

    def test_multi_clip_op_accepts_broad_selector(self):
        out = validate_operation({"op": "property.set",
                                  "clip": {"track": "V1", "all": True},
                                  "property": "Opacity", "value": 80})
        assert out["params"]["clip"]["all"] is True


class TestCatalogIntegrity:
    def test_every_op_describes_itself(self):
        described = catalog.describe()
        assert described["count"] == len(catalog.OPS)
        for entry in described["operations"]:
            assert entry["summary"], f"{entry['op']} has no summary"
            assert "params" in entry

    def test_unsupported_ops_all_name_an_alternative(self):
        for entry in catalog.unsupported():
            assert entry["reason"], entry["op"]
            assert entry["alternative"], entry["op"]

    def test_describe_one_op(self):
        described = catalog.describe(op="animate")
        assert described["op"] == "animate"
        assert "duration" in described["params"]

    def test_explain_is_readable(self):
        plan = validate_plan({"ops": [
            {"op": "animate", "clip": {"track": "V1", "index": 0},
             "property": "Scale", "to": 120, "duration": 1.0},
        ]})
        lines = explain(plan)
        assert "animate" in lines[0]
        assert "V1#0" in lines[0]
