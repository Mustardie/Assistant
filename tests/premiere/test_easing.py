"""Easing curves and keyframe baking.

This is the maths that makes real animation curves possible despite Premiere
not exposing graph-editor handles, so it gets tested properly rather than
smoke-tested.
"""
from __future__ import annotations

import pytest

from premiere.easing import (
    EASINGS, NATIVE_INTERP, bake, cubic_bezier, db_ramp, native_interp_for,
    resolve,
)
from premiere.errors import ValidationError


class TestCurveProperties:
    @pytest.mark.parametrize("name", sorted(EASINGS))
    def test_every_easing_is_normalised(self, name):
        curve = EASINGS[name]
        assert curve(0.0) == pytest.approx(0.0, abs=1e-6)
        assert curve(1.0) == pytest.approx(1.0, abs=1e-6)

    @pytest.mark.parametrize("name", [
        "linear", "ease_in", "ease_out", "ease_in_out", "sine_in_out",
        "quad_in", "expo_out", "circ_in_out",
    ])
    def test_monotonic_easings_never_go_backwards(self, name):
        curve = EASINGS[name]
        previous = -1e9
        for i in range(101):
            value = curve(i / 100.0)
            assert value >= previous - 1e-9, f"{name} reversed at t={i / 100}"
            previous = value

    def test_ease_in_starts_slow(self):
        # The defining property: at the midpoint an ease-in has covered less
        # than half the distance.
        assert EASINGS["ease_in"](0.5) < 0.5

    def test_ease_out_starts_fast(self):
        assert EASINGS["ease_out"](0.5) > 0.5

    def test_back_overshoots(self):
        # back_out is supposed to exceed 1 before settling -- that overshoot is
        # the whole point of the curve.
        assert max(EASINGS["back_out"](i / 100.0) for i in range(101)) > 1.0

    def test_hold_is_a_step(self):
        assert EASINGS["hold"](0.99) == 0.0
        assert EASINGS["hold"](1.0) == 1.0


class TestCubicBezier:
    def test_linear_control_points_give_linear(self):
        curve = cubic_bezier(0.33, 0.33, 0.66, 0.66)
        for i in range(0, 101, 10):
            t = i / 100.0
            assert curve(t) == pytest.approx(t, abs=0.02)

    def test_css_ease_in_out_is_symmetric(self):
        curve = cubic_bezier(0.42, 0.0, 0.58, 1.0)
        assert curve(0.5) == pytest.approx(0.5, abs=1e-3)
        assert curve(0.25) + curve(0.75) == pytest.approx(1.0, abs=1e-3)

    def test_endpoints_are_exact(self):
        curve = cubic_bezier(0.1, 0.9, 0.2, 1.0)
        assert curve(0.0) == 0.0
        assert curve(1.0) == 1.0

    def test_out_of_range_input_clamps(self):
        curve = cubic_bezier(0.42, 0.0, 0.58, 1.0)
        assert curve(-1.0) == 0.0
        assert curve(2.0) == 1.0


class TestResolve:
    def test_by_name(self):
        assert resolve("ease_out") is EASINGS["ease_out"]

    def test_name_is_case_insensitive(self):
        assert resolve("EASE_OUT")(0.5) == EASINGS["ease_out"](0.5)

    def test_bezier_list(self):
        curve = resolve([0.42, 0.0, 0.58, 1.0])
        assert curve(0.5) == pytest.approx(0.5, abs=1e-3)

    def test_unknown_name_is_rejected_with_a_pointer(self):
        with pytest.raises(ValidationError, match="caps.easings"):
            resolve("swooshy")

    def test_wrong_length_bezier_rejected(self):
        with pytest.raises(ValidationError, match="exactly 4"):
            resolve([0.1, 0.2])

    def test_none_defaults_to_linear(self):
        assert resolve(None)(0.5) == 0.5


class TestNativeMapping:
    def test_simple_easings_map_to_premiere_modes(self):
        assert native_interp_for("linear") == "linear"
        assert native_interp_for("ease_in_out") == "ease_both"

    def test_exotic_easings_have_no_native_mode(self):
        # These MUST return "" so the engine bakes them instead of silently
        # substituting a different curve.
        assert native_interp_for("bounce_out") == ""
        assert native_interp_for("elastic_in") == ""
        assert native_interp_for([0.2, 0.9, 0.3, 1.0]) == ""

    def test_every_native_target_is_a_real_mode(self):
        from premiere.catalog import INTERP_MODES
        for mode in NATIVE_INTERP.values():
            assert mode in INTERP_MODES


class TestBake:
    def test_endpoints_are_exact(self):
        keys = bake(0.0, 1.0, 0.0, 100.0, "ease_in_out", fps=30)
        assert keys[0]["time"] == 0.0
        assert keys[0]["value"] == pytest.approx(0.0)
        assert keys[-1]["time"] == pytest.approx(1.0)
        assert keys[-1]["value"] == pytest.approx(100.0)

    def test_linear_collapses_to_two_keyframes(self):
        # Baking a straight line into 30 keyframes would be pointless clutter.
        keys = bake(0.0, 2.0, 0.0, 100.0, "linear", fps=30)
        assert len(keys) == 2

    def test_curved_easing_produces_many_keyframes(self):
        keys = bake(0.0, 2.0, 0.0, 100.0, "bounce_out", fps=30)
        assert len(keys) > 20

    def test_frame_resolution_by_default(self):
        keys = bake(0.0, 1.0, 0.0, 100.0, "ease_in", fps=24)
        assert len(keys) == 25          # 24 intervals + 1

    def test_explicit_resolution(self):
        keys = bake(0.0, 2.0, 0.0, 100.0, "ease_in", fps=30, resolution=5)
        assert len(keys) == 11          # 10 intervals + 1

    def test_vector_values_interpolate_componentwise(self):
        keys = bake(0.0, 1.0, [0.0, 0.0], [1.0, 0.5], "linear", fps=30)
        assert keys[-1]["value"] == [1.0, 0.5]

    def test_curved_vector_bake(self):
        keys = bake(0.0, 1.0, [0.0, 0.0], [100.0, 50.0], "ease_in", fps=30)
        midpoint = keys[len(keys) // 2]["value"]
        assert midpoint[0] < 50.0       # ease_in lags at the midpoint
        assert midpoint[1] < 25.0

    def test_mismatched_vector_shapes_rejected(self):
        with pytest.raises(ValidationError, match="same shape"):
            bake(0.0, 1.0, [0.0, 0.0], [1.0, 1.0, 1.0], "linear", fps=30)

    def test_backwards_window_rejected(self):
        with pytest.raises(ValidationError, match="must be after"):
            bake(2.0, 1.0, 0.0, 100.0, "linear", fps=30)

    def test_keyframe_count_is_capped(self):
        # A 10-minute ramp at 60fps must not emit 36000 keyframes.
        keys = bake(0.0, 600.0, 0.0, 100.0, "ease_in", fps=60, max_keys=600)
        assert len(keys) <= 601

    def test_times_are_strictly_increasing(self):
        keys = bake(1.0, 3.0, 0.0, 100.0, "elastic_out", fps=30)
        times = [k["time"] for k in keys]
        assert times == sorted(times)
        assert len(set(times)) == len(times)


class TestDbRamp:
    def test_fade_in_goes_from_floor_to_zero(self):
        keys = db_ramp(0.0, 1.0, -60.0, 0.0, "ease_in_out", fps=30)
        assert keys[0]["value"] == pytest.approx(-60.0)
        assert keys[-1]["value"] == pytest.approx(0.0)

    def test_ramp_is_sparser_than_a_frame_bake(self):
        # Audio envelopes do not need per-frame keyframes.
        assert len(db_ramp(0.0, 2.0, -60.0, 0.0, "ease_in_out", fps=60)) < 40
