"""Time, selector and field validation."""
from __future__ import annotations

import pytest

from premiere.errors import ValidationError
from premiere.schema import (
    Field, format_timecode, parse_clip_selector, parse_time, parse_track,
    seconds_to_ticks, selector_is_multi, validate_fields,
)


class TestParseTime:
    def test_numbers_pass_through(self):
        assert parse_time(12.5) == 12.5
        assert parse_time(0) == 0.0

    def test_suffixed_forms(self):
        assert parse_time("12.5s") == 12.5
        assert parse_time("500ms") == 0.5
        assert parse_time("90f", fps=30) == 3.0
        assert parse_time("45 frames", fps=30) == 1.5

    def test_timecode(self):
        assert parse_time("00:00:10:15", fps=30) == pytest.approx(10.5)
        assert parse_time("01:02:03:00", fps=30) == pytest.approx(3723.0)

    def test_drop_frame_separator_accepted(self):
        assert parse_time("00:00:10;15", fps=30) == pytest.approx(10.5)

    def test_frame_field_must_be_in_range(self):
        with pytest.raises(ValidationError, match="out of range"):
            parse_time("00:00:01:45", fps=30)

    def test_bare_numeric_string(self):
        assert parse_time("7") == 7.0

    def test_rejects_nonsense(self):
        with pytest.raises(ValidationError, match="unrecognised time"):
            parse_time("soon")

    def test_rejects_booleans(self):
        # bool is an int subclass in Python; silently reading True as 1 second
        # would be a nasty way to lose a keyframe.
        with pytest.raises(ValidationError):
            parse_time(True)

    def test_rejects_infinity(self):
        with pytest.raises(ValidationError, match="finite"):
            parse_time(float("inf"))


class TestTicks:
    def test_round_trip(self):
        assert seconds_to_ticks(1.0) == 254016000000

    def test_timecode_formatting(self):
        assert format_timecode(10.5, 30) == "00:00:10:15"


class TestParseTrack:
    def test_ui_spelling_is_one_based(self):
        assert parse_track("V1") == {"type": "video", "index": 0}
        assert parse_track("A3") == {"type": "audio", "index": 2}

    def test_lowercase(self):
        assert parse_track("v2") == {"type": "video", "index": 1}

    def test_explicit_form_is_zero_based(self):
        assert parse_track({"type": "audio", "index": 0}) == {
            "type": "audio", "index": 0}

    def test_rejects_track_zero(self):
        with pytest.raises(ValidationError, match="start at 1"):
            parse_track("V0")

    def test_rejects_garbage(self):
        with pytest.raises(ValidationError, match="unrecognised track"):
            parse_track("video one")


class TestClipSelector:
    def test_index_form(self):
        out = parse_clip_selector({"track": "V1", "index": 2})
        assert out == {"track": {"type": "video", "index": 0}, "index": 2}

    def test_at_form_parses_time(self):
        out = parse_clip_selector({"track": "V1", "at": "00:00:02:00"}, fps=30)
        assert out["at"] == pytest.approx(2.0)

    def test_range_form(self):
        out = parse_clip_selector({"track": "V1", "range": [1, 5]})
        assert out["range"] == [1.0, 5.0]

    def test_range_must_be_ordered(self):
        with pytest.raises(ValidationError, match="must be after"):
            parse_clip_selector({"track": "V1", "range": [5, 1]})

    def test_conflicting_keys_rejected(self):
        with pytest.raises(ValidationError, match="conflicting"):
            parse_clip_selector({"track": "V1", "index": 0, "at": 3})

    def test_positional_selector_requires_track(self):
        with pytest.raises(ValidationError, match="needs a track"):
            parse_clip_selector({"index": 0})

    def test_id_needs_no_track(self):
        assert parse_clip_selector({"id": "abc"}) == {"id": "abc"}

    def test_unknown_key_suggests_valid_ones(self):
        with pytest.raises(ValidationError, match="unknown clip selector"):
            parse_clip_selector({"track": "V1", "postion": 2})

    def test_empty_selector_rejected(self):
        with pytest.raises(ValidationError, match="needs one of"):
            parse_clip_selector({"track": "V1"})

    def test_multi_detection(self):
        assert selector_is_multi({"range": [0, 5]})
        assert selector_is_multi({"all": True})
        assert selector_is_multi({"selected": True})
        assert not selector_is_multi({"index": 0})
        assert not selector_is_multi({"id": "x"})


class TestFields:
    def test_number_bounds(self):
        field = Field("number", min=0, max=100)
        assert field.validate(50, fps=30, path="x") == 50.0
        with pytest.raises(ValidationError, match="above the maximum"):
            field.validate(150, fps=30, path="x")
        with pytest.raises(ValidationError, match="below the minimum"):
            field.validate(-1, fps=30, path="x")

    def test_integer_rejects_fractions(self):
        with pytest.raises(ValidationError, match="expected an integer"):
            Field("integer").validate(1.5, fps=30, path="x")

    def test_enum_is_case_insensitive(self):
        field = Field("enum", choices=["center", "start"])
        assert field.validate("CENTER", fps=30, path="x") == "center"

    def test_enum_rejects_unknown(self):
        with pytest.raises(ValidationError, match="expected one of"):
            Field("enum", choices=["a", "b"]).validate("c", fps=30, path="x")

    def test_value_accepts_vectors_and_scalars(self):
        field = Field("value")
        assert field.validate([0.5, 0.5], fps=30, path="v") == [0.5, 0.5]
        assert field.validate(80, fps=30, path="v") == 80
        assert field.validate("#FFF", fps=30, path="v") == "#FFF"

    def test_value_rejects_mixed_lists(self):
        with pytest.raises(ValidationError, match="all numbers"):
            Field("value").validate([1, "x"], fps=30, path="v")

    def test_duration_must_be_positive(self):
        with pytest.raises(ValidationError, match="must be positive"):
            Field("duration").validate(0, fps=30, path="d")

    def test_missing_required_field(self):
        with pytest.raises(ValidationError, match="missing required"):
            validate_fields({"a": Field("number", required=True)}, {}, fps=30)

    def test_defaults_are_applied(self):
        out = validate_fields({"a": Field("number", default=5)}, {}, fps=30)
        assert out["a"] == 5

    def test_unknown_parameter_suggests_near_match(self):
        fields = {"duration": Field("duration")}
        with pytest.raises(ValidationError) as excinfo:
            validate_fields(fields, {"duraton": 1}, fps=30)
        assert "duration" in str(excinfo.value)

    def test_none_is_treated_as_absent(self):
        # Models frequently emit explicit nulls for optional fields.
        out = validate_fields({"a": Field("number", default=3)},
                              {"a": None}, fps=30)
        assert out["a"] == 3
