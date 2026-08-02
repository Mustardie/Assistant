"""Matcher tests: text/type/position scoring, confidence bands, nearby
search, resolution scaling helpers."""

import pytest

from skills import matcher


def _obj(text, otype, bbox, confidence=1.0):
    x, y, w, h = bbox
    return {
        "text": text, "type": otype, "bbox": [x, y, w, h],
        "cx": x + w / 2, "cy": y + h / 2, "confidence": confidence,
    }


FRAME = {"width": 1920, "height": 1080}


def test_text_similarity():
    assert matcher.text_similarity("Login", "Login") == 1.0
    assert matcher.text_similarity("Login", "login") == 1.0
    assert matcher.text_similarity("Sign In", "Sign in") == 1.0
    assert matcher.text_similarity("Login", "Sign Out") < 0.5
    assert matcher.text_similarity("", "x") == 0.0


def test_type_compatibility():
    assert matcher.type_similarity("button", "button") == 1.0
    assert matcher.type_similarity("button", "link") == 0.9
    assert matcher.type_similarity("input", "text") == 0.9
    assert matcher.type_similarity("button", "image") < 0.5


def test_position_similarity():
    assert matcher.position_similarity((0.5, 0.5), (0.5, 0.5)) == 1.0
    near = matcher.position_similarity((0.5, 0.5), (0.52, 0.5))
    far = matcher.position_similarity((0.5, 0.5), (0.9, 0.9))
    assert near > 0.5 > far


def test_match_element_exact_hit():
    objects = [_obj("Login", "button", (500, 300, 100, 40))]
    result = matcher.match_element(
        {"text": "Login", "type": "button"},
        objects,
        expected_rel=(0.28, 0.30),
        frame_size=FRAME,
    )
    assert result["found"] is True
    assert result["strategy"] == "matched"
    assert result["confidence"] >= matcher.HIGH_CONFIDENCE
    assert result["element"]["text"] == "Login"


def test_match_element_prefers_text_over_position():
    objects = [
        _obj("Login", "button", (500, 300, 100, 40)),
        _obj("Sign In", "button", (280, 280, 100, 40)),  # near expected position
    ]
    result = matcher.match_element(
        {"text": "Login", "type": "button"},
        objects,
        expected_rel=(0.15, 0.28),
        frame_size=FRAME,
    )
    assert result["element"]["text"] == "Login"


def test_match_element_position_shift_still_finds():
    """UI shifted across the screen -- text match keeps it findable."""
    objects = [_obj("Login", "button", (1500, 700, 100, 40))]
    result = matcher.match_element(
        {"text": "Login", "type": "button"},
        objects,
        expected_rel=(0.1, 0.1),
        frame_size=FRAME,
    )
    assert result["found"] is True
    assert result["strategy"] in ("matched", "nearby")


def test_match_element_missing():
    objects = [_obj("Cancel", "button", (500, 300, 100, 40))]
    result = matcher.match_element(
        {"text": "Login", "type": "button"},
        objects,
        expected_rel=(0.28, 0.30),
        frame_size=FRAME,
    )
    assert result["found"] is False
    assert result["strategy"] == "none"


def test_find_nearby_recovers_with_text_only():
    objects = [
        _obj("Save Document", "button", (1800, 900, 100, 40)),
        _obj("Login", "button", (1500, 700, 100, 40)),
    ]
    result = matcher.find_nearby(
        {"text": "Login", "type": "button"},
        objects,
        point=(0.1, 0.1),
        frame_size=FRAME,
    )
    assert result["found"] is True
    assert result["strategy"] == "expanded_search"
    assert result["element"]["text"] == "Login"


def test_scaling_adaptation():
    assert matcher.scaling_factor({"width": 1920, "height": 1080},
                                  {"width": 2560, "height": 1440}) == pytest.approx(1.3333, abs=0.01)
    assert matcher.scaling_factor({}, {}) == 1.0


def test_relative_and_scaled_absolute_roundtrip():
    point = matcher.relative_point((960, 540), FRAME)
    assert point == (0.5, 0.5)
    back = matcher.scaled_absolute(point, {"width": 3840, "height": 2160})
    assert back == (1920.0, 1080.0)


def test_element_at_uses_bbox():
    objects = [_obj("Search", "input", (10, 10, 100, 30))]
    hit = matcher.element_at(objects, (50, 20))
    assert hit is not None and hit["text"] == "Search"
    assert matcher.element_at(objects, (500, 500)) is None


def test_compare_frames_structural_similarity():
    expected = [_obj("OK", "button", (0, 0, 10, 10)), _obj("Cancel", "button", (0, 0, 10, 10))]
    current = [_obj("OK", "button", (100, 100, 10, 10)), _obj("Help", "button", (0, 0, 10, 10))]
    assert matcher.compare_frames(expected, current) == 0.5


def test_expand_bbox():
    assert matcher.expand_bbox((100, 100, 100, 40)) == (50, 80, 200, 80)
