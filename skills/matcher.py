"""Semantic element matching for skill playback.

The matcher compares expected UI elements (stored in a skill) against the
UI objects detected on the current screen using TEXT, TYPE and POSITION
similarity -- never raw pixels. It adapts automatically when:

* the monitor resolution / scaling changed  -> relative coordinates
* the window moved                          -> relative coordinates
* buttons/text shifted slightly             -> nearby search
* the element is missing entirely           -> vision locate / coordinate fallback

Confidence bands: >= HIGH_CONFIDENCE is a direct hit, >= MEDIUM_CONFIDENCE
triggers an expanded nearby search, below that falls through to
vision-based locating and finally recorded-coordinate fallback.
"""

import logging
import math

from skills.vision import UIObject, bbox_contains

logger = logging.getLogger(__name__)

HIGH_CONFIDENCE = 0.7
MEDIUM_CONFIDENCE = 0.45

_WEIGHT_TEXT = 0.55
_WEIGHT_TYPE = 0.25
_WEIGHT_POS = 0.20

# Minimum text similarity for a labelled element to ever be considered a
# match. Prevents clicking an element whose label is unrelated.
_TEXT_FLOOR = 0.5

# Type names that count as equivalent for matching purposes.
_TYPE_GROUPS = {
    "button": {"button", "menuitem", "tab", "link", "badge"},
    "input": {"input", "dropdown", "text", "search"},
    "title": {"title", "window", "tab"},
    "menu": {"menu", "menuitem", "dropdown"},
}


def normalize_text(text: str) -> str:
    return " ".join((text or "").lower().split())


def text_similarity(expected: str, actual: str) -> float:
    """Fuzzy string similarity in [0, 1]. Uses RapidFuzz when available,
    with a pure-Python fallback so tests and offline runs never break."""
    a, b = normalize_text(expected), normalize_text(actual)
    if not a and not b:
        return 0.0
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    try:
        from rapidfuzz import fuzz

        return float(fuzz.token_set_ratio(a, b)) / 100.0
    except Exception:
        return _difflib_similarity(a, b)


def _difflib_similarity(a: str, b: str) -> float:
    import difflib

    return difflib.SequenceMatcher(None, a, b).ratio()


def type_similarity(expected_type: str, actual_type: str) -> float:
    a, b = (expected_type or "").lower(), (actual_type or "").lower()
    if not a or not b:
        return 0.5
    if a == b:
        return 1.0
    if b in _TYPE_GROUPS.get(a, ()):
        return 0.9
    if a in _TYPE_GROUPS.get(b, ()):
        return 0.9
    # Anything interactive is at least loosely compatible with anything
    # clickable; pure text elements score lower against interactive types.
    interactive = {"button", "menuitem", "tab", "link", "input", "checkbox", "radio"}
    if a in interactive and b in interactive:
        return 0.6
    return 0.2


def position_similarity(relative_a, relative_b, *, tolerance: float = 0.08) -> float:
    """Similarity of two normalized (x, y) points. Tolerance is a fraction
    of the screen; a drop-off curve keeps near-misses meaningful."""
    if relative_a is None or relative_b is None:
        return 0.5
    ax, ay = relative_a
    bx, by = relative_b
    distance = math.hypot(ax - bx, ay - by)
    return max(0.0, 1.0 - distance / tolerance)


def relative_point(point, frame_size) -> tuple[float, float] | None:
    if not point or not frame_size:
        return None
    width, height = frame_size.get("width"), frame_size.get("height")
    if not width or not height:
        return None
    x, y = point
    return (x / width, y / height)


def scaled_absolute(relative, frame_size) -> tuple[float, float] | None:
    """Convert a normalized point back to absolute pixels for the current
    screen -- the core adaptation for resolution/scaling changes."""
    if not relative or not frame_size:
        return None
    width, height = frame_size.get("width"), frame_size.get("height")
    if not width or not height:
        return None
    rx, ry = relative
    return (rx * width, ry * height)


def scaling_factor(recorded_size: dict, current_size: dict) -> float:
    rw = (recorded_size or {}).get("width") or 0
    cw = (current_size or {}).get("width") or 0
    if rw and cw:
        return cw / rw
    return 1.0


def monitor_scaling() -> float:
    """Windows DPI scaling (logical vs physical pixels). Screenshots from
    mss are physical; pyautogui works in logical coordinates, so clicks
    must be divided by this factor."""
    try:
        import ctypes

        factor = ctypes.windll.shcore.GetScaleFactorForDevice(0)
        if factor and factor > 0:
            return factor / 100.0
    except Exception:
        pass
    try:
        import ctypes

        # Fallback: per-monitor DPI via user32.GetDpiForSystem (Win10 1607+)
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi and dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def to_logical(point, scale: float | None = None) -> tuple[float, float]:
    if not point:
        return point
    factor = scale if scale is not None else monitor_scaling()
    return (point[0] / factor, point[1] / factor)


def expand_bbox(bbox, factor: float = 0.5, limit=None) -> tuple[int, int, int, int]:
    """Grow a bounding box by `factor` of its own size -- used for the
    'search nearby' recovery strategy."""
    x, y, w, h = bbox
    grow_x, grow_y = int(w * factor), int(h * factor)
    new = (max(0, x - grow_x), max(0, y - grow_y), w + 2 * grow_x, h + 2 * grow_y)
    if limit:
        lw, lh = limit
        new = (new[0], new[1], min(new[2], lw - new[0]), min(new[3], lh - new[1]))
    return new


def _element_score(expected: dict, obj: dict, expected_rel, frame_size) -> float:
    text_score = text_similarity(expected.get("text", ""), obj.get("text", ""))
    type_score = type_similarity(expected.get("type", ""), obj.get("type", ""))
    pos_score = position_similarity(expected_rel, relative_point(
        (obj.get("cx", 0), obj.get("cy", 0)), frame_size
    ))

    # A labelled element whose text doesn't resemble the expected label at all
    # is never acceptable, no matter how well position/type match.
    if normalize_text(expected.get("text", "")) and text_score < _TEXT_FLOOR:
        return -1.0

    # Elements without a text label (icons) rely on position + type more.
    if not normalize_text(expected.get("text", "")):
        return 0.30 * text_score + 0.40 * type_score + 0.30 * pos_score
    return _WEIGHT_TEXT * text_score + _WEIGHT_TYPE * type_score + _WEIGHT_POS * pos_score


def match_element(
    expected: dict,
    objects: list[dict],
    *,
    expected_rel=None,
    frame_size=None,
    prev_point=None,
) -> dict:
    """Match an expected UI element against detected objects.

    Returns {"found": bool, "element": obj|None, "confidence": float,
             "strategy": "matched"|"nearby"|"none", "scores": {...}}.
    """
    candidates = []
    anchor_rel = expected_rel
    if anchor_rel is None and prev_point is not None and frame_size:
        anchor_rel = relative_point(prev_point, frame_size)

    for obj in objects or []:
        obj = dict(obj)
        cx, cy = obj.get("cx"), obj.get("cy")
        if cx is None and obj.get("bbox"):
            bbox = obj["bbox"]
            cx, cy = bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2
        obj["cx"], obj["cy"] = cx, cy
        score = _element_score(expected, obj, anchor_rel, frame_size)
        if score > 0.0:
            candidates.append((score, obj))

    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return {"found": False, "element": None, "confidence": 0.0,
                "strategy": "none", "scores": {}}

    best_score, best = candidates[0]
    if best_score >= HIGH_CONFIDENCE:
        strategy = "matched"
    elif best_score >= MEDIUM_CONFIDENCE:
        strategy = "nearby"
    else:
        strategy = "none"
    return {
        "found": strategy != "none",
        "element": best,
        "confidence": round(best_score, 3),
        "strategy": strategy,
        "scores": {"top": round(best_score, 3), "count": len(candidates)},
    }


def find_nearby(
    expected: dict,
    objects: list[dict],
    *,
    point=None,
    frame_size=None,
    radius: float = 0.15,
    previous_element=None,
) -> dict:
    """Recovery search: re-run matching giving the original position no
    weight at all -- pure text+type matching across the whole screen, then
    prefer candidates within a radius of the expected position."""
    results = []
    for obj in objects or []:
        obj = dict(obj)
        bbox = obj["bbox"]
        cx, cy = bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2
        obj["cx"], obj["cy"] = cx, cy
        text_score = text_similarity(expected.get("text", ""), obj.get("text", ""))
        type_score = type_similarity(expected.get("type", ""), obj.get("type", ""))
        if not normalize_text(expected.get("text", "")):
            score = 0.55 * type_score + 0.45 * text_score
        else:
            score = 0.75 * text_score + 0.25 * type_score
        if point and frame_size and radius:
            rel = relative_point((cx, cy), frame_size)
            if rel:
                distance = math.hypot(rel[0] - point[0], rel[1] - point[1])
                if distance <= radius:
                    score += 0.08  # small bonus for proximity
        results.append((score, obj))

    results.sort(key=lambda item: item[0], reverse=True)
    if not results:
        return {"found": False, "element": None, "confidence": 0.0, "strategy": "none"}
    best_score, best = results[0]
    if best_score < MEDIUM_CONFIDENCE:
        return {"found": False, "element": None, "confidence": round(best_score, 3),
                "strategy": "none"}
    return {
        "found": True,
        "element": best,
        "confidence": round(best_score, 3),
        "strategy": "expanded_search",
    }


def element_at(objects: list[dict], point) -> dict | None:
    """The detected object whose bbox contains the point (used during
    recording to attach a semantic target to a raw click)."""
    for obj in objects or []:
        bbox = obj.get("bbox")
        if bbox and bbox_contains(bbox, point[0], point[1]):
            return obj
    return None


def compare_frames(expected_objects: list[dict], current_objects: list[dict]) -> float:
    """Structural similarity between a stored UI frame and the current one
    (used for post-action verification). Matches objects by text; returns
    the fraction of expected objects confidently present."""
    expected_objects = list(expected_objects or [])
    if not expected_objects:
        return 0.0
    current = list(current_objects or [])
    matched = 0
    for expected in expected_objects:
        for obj in current:
            if text_similarity(expected.get("text", ""), obj.get("text", "")) >= 0.9 and (
                type_similarity(expected.get("type", ""), obj.get("type", "")) >= 0.9
            ):
                matched += 1
                break
    return matched / len(expected_objects)
