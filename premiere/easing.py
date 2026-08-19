"""Easing curves and keyframe baking.

Why this exists in Python rather than in ExtendScript: Premiere's scripting API
lets you choose a keyframe's *interpolation mode*, but it does not expose the
Bezier velocity handles that the graph editor draws. So "ease this scale push
with a custom curve" is not directly expressible.

The honest workaround -- and the one implemented here -- is to sample the
desired curve and write dense keyframes. That reproduces any curve shape
exactly, at the cost of more keyframes on the timeline. Doing the sampling in
Python keeps the math testable without a Premiere host, and keeps the JSX side
a thin executor.

``resolve`` accepts either a named easing or a raw CSS-style cubic-bezier
``[x1, y1, x2, y2]``, so the model can specify a curve precisely when it wants
to and by name when it does not care.
"""
from __future__ import annotations

import math
from typing import Callable, Sequence

from premiere.errors import ValidationError

# --------------------------------------------------------------------------
# Normalised easing functions: f(0) == 0, f(1) == 1
# --------------------------------------------------------------------------


def _flip(fn: Callable[[float], float]) -> Callable[[float], float]:
    return lambda t: 1.0 - fn(1.0 - t)


def _in_out(fn: Callable[[float], float]) -> Callable[[float], float]:
    def eased(t: float) -> float:
        if t < 0.5:
            return fn(t * 2.0) / 2.0
        return 1.0 - fn((1.0 - t) * 2.0) / 2.0
    return eased


def _power(exponent: float) -> Callable[[float], float]:
    return lambda t: t ** exponent


def _sine_in(t: float) -> float:
    return 1.0 - math.cos((t * math.pi) / 2.0)


def _expo_in(t: float) -> float:
    return 0.0 if t <= 0.0 else 2.0 ** (10.0 * (t - 1.0))


def _circ_in(t: float) -> float:
    return 1.0 - math.sqrt(max(0.0, 1.0 - t * t))


def _back_in(t: float) -> float:
    c1 = 1.70158
    return (c1 + 1.0) * t * t * t - c1 * t * t


def _elastic_in(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    c4 = (2.0 * math.pi) / 3.0
    return -(2.0 ** (10.0 * t - 10.0)) * math.sin((t * 10.0 - 10.75) * c4)


def _bounce_out(t: float) -> float:
    n1, d1 = 7.5625, 2.75
    if t < 1.0 / d1:
        return n1 * t * t
    if t < 2.0 / d1:
        t -= 1.5 / d1
        return n1 * t * t + 0.75
    if t < 2.5 / d1:
        t -= 2.25 / d1
        return n1 * t * t + 0.9375
    t -= 2.625 / d1
    return n1 * t * t + 0.984375


_bounce_in = _flip(_bounce_out)

EASINGS: dict = {
    "linear": lambda t: t,

    "quad_in": _power(2), "quad_out": _flip(_power(2)), "quad_in_out": _in_out(_power(2)),
    "cubic_in": _power(3), "cubic_out": _flip(_power(3)), "cubic_in_out": _in_out(_power(3)),
    "quart_in": _power(4), "quart_out": _flip(_power(4)), "quart_in_out": _in_out(_power(4)),
    "quint_in": _power(5), "quint_out": _flip(_power(5)), "quint_in_out": _in_out(_power(5)),

    "sine_in": _sine_in, "sine_out": _flip(_sine_in), "sine_in_out": _in_out(_sine_in),
    "expo_in": _expo_in, "expo_out": _flip(_expo_in), "expo_in_out": _in_out(_expo_in),
    "circ_in": _circ_in, "circ_out": _flip(_circ_in), "circ_in_out": _in_out(_circ_in),
    "back_in": _back_in, "back_out": _flip(_back_in), "back_in_out": _in_out(_back_in),

    "elastic_in": _elastic_in,
    "elastic_out": _flip(_elastic_in),
    "elastic_in_out": _in_out(_elastic_in),

    "bounce_in": _bounce_in,
    "bounce_out": _bounce_out,
    "bounce_in_out": _in_out(_bounce_in),
}

# Friendly aliases -- these are the names a model reaches for first.
EASINGS["ease_in"] = EASINGS["cubic_in"]
EASINGS["ease_out"] = EASINGS["cubic_out"]
EASINGS["ease_in_out"] = EASINGS["cubic_in_out"]
EASINGS["ease"] = EASINGS["cubic_in_out"]
EASINGS["smooth"] = EASINGS["sine_in_out"]
EASINGS["hold"] = lambda t: 0.0 if t < 1.0 else 1.0
EASINGS["step"] = EASINGS["hold"]

#: Easings that Premiere's own keyframe interpolation modes can express well
#: enough that baking is unnecessary. ``animate`` uses this to stay native (and
#: keep the timeline tidy) whenever it can.
NATIVE_INTERP: dict = {
    "linear": "linear",
    "hold": "hold",
    "step": "hold",
    "ease_in": "ease_in",
    "ease_out": "ease_out",
    "ease_in_out": "ease_both",
    "ease": "ease_both",
}


def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """CSS-style cubic-bezier easing, solved by Newton with a bisection fallback.

    Control points are clamped on x (as CSS does) because an x outside [0, 1]
    makes the curve non-monotonic in time and therefore not an easing at all.
    y is left free so overshoot curves still work.
    """
    x1 = min(1.0, max(0.0, float(x1)))
    x2 = min(1.0, max(0.0, float(x2)))
    y1, y2 = float(y1), float(y2)

    def sample(a: float, b: float, t: float) -> float:
        # Expanded Bezier with P0=0, P3=1.
        inv = 1.0 - t
        return 3.0 * inv * inv * t * a + 3.0 * inv * t * t * b + t * t * t

    def slope(a: float, b: float, t: float) -> float:
        inv = 1.0 - t
        return (3.0 * inv * inv * (a)
                + 6.0 * inv * t * (b - a)
                + 3.0 * t * t * (1.0 - b))

    def eased(x: float) -> float:
        if x <= 0.0:
            return 0.0
        if x >= 1.0:
            return 1.0
        t = x
        for _ in range(8):
            error = sample(x1, x2, t) - x
            if abs(error) < 1e-7:
                return sample(y1, y2, t)
            derivative = slope(x1, x2, t)
            if abs(derivative) < 1e-9:
                break
            t -= error / derivative
        low, high, t = 0.0, 1.0, x
        for _ in range(32):
            value = sample(x1, x2, t)
            if abs(value - x) < 1e-7:
                break
            if value < x:
                low = t
            else:
                high = t
            t = (low + high) / 2.0
        return sample(y1, y2, t)

    return eased


def resolve(easing, *, path: str = "easing") -> Callable[[float], float]:
    """Turn an easing spec (name or [x1,y1,x2,y2]) into a callable."""
    if easing is None:
        return EASINGS["linear"]
    if callable(easing):
        return easing
    if isinstance(easing, str):
        key = easing.strip().lower()
        if key in EASINGS:
            return EASINGS[key]
        raise ValidationError(
            f"unknown easing {easing!r}",
            hint="call caps.easings for the list, or pass a cubic-bezier "
                 "[x1, y1, x2, y2]",
            path=path,
        )
    if isinstance(easing, (list, tuple)):
        if len(easing) != 4:
            raise ValidationError(
                "a cubic-bezier easing needs exactly 4 numbers [x1, y1, x2, y2]",
                path=path,
            )
        try:
            return cubic_bezier(*(float(v) for v in easing))
        except (TypeError, ValueError):
            raise ValidationError(
                "cubic-bezier control points must be numbers", path=path
            ) from None
    raise ValidationError(
        f"unusable easing {easing!r}",
        hint="use a name or [x1, y1, x2, y2]",
        path=path,
    )


def native_interp_for(easing) -> str:
    """The Premiere interpolation mode that matches this easing, or "" if none.

    Returning "" is the signal to bake -- it is how ``animate`` decides between
    two native keyframes and a sampled curve.
    """
    if isinstance(easing, str):
        return NATIVE_INTERP.get(easing.strip().lower(), "")
    return ""


def _lerp_value(a, b, t: float):
    """Interpolate scalars and vectors alike.

    Property values in Premiere are sometimes scalars (Opacity), sometimes
    2-vectors (Position), sometimes 4-vectors (colour). Keeping one
    interpolator means the animation ops do not care which they were handed.
    """
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        left = list(a) if isinstance(a, (list, tuple)) else [a]
        right = list(b) if isinstance(b, (list, tuple)) else [b]
        if len(left) != len(right):
            raise ValidationError(
                f"cannot interpolate between a {len(left)}-value and a "
                f"{len(right)}-value property value",
                hint="'from' and 'to' must have the same shape "
                     "(e.g. both [x, y])",
            )
        return [float(l) + (float(r) - float(l)) * t for l, r in zip(left, right)]
    return float(a) + (float(b) - float(a)) * t


def bake(
    start: float,
    end: float,
    from_value,
    to_value,
    easing,
    *,
    fps: float = 30.0,
    resolution: int = 0,
    max_keys: int = 600,
) -> list:
    """Sample an easing curve into ``[{"time", "value"}]`` keyframes.

    ``resolution`` is keyframes per second; 0 means one per frame, which makes
    the baked animation frame-exact. ``max_keys`` is a guard so a long
    slow-motion ramp cannot dump ten thousand keyframes onto a clip.
    """
    if end <= start:
        raise ValidationError(
            f"bake end ({end:g}s) must be after start ({start:g}s)", path="end"
        )

    curve = resolve(easing)
    duration = end - start
    rate = float(resolution) if resolution else (fps if fps > 0 else 30.0)
    count = int(math.ceil(duration * rate))
    count = max(2, min(count, max_keys))

    keys = []
    for i in range(count + 1):
        progress = i / count
        keys.append({
            "time": start + duration * progress,
            "value": _lerp_value(from_value, to_value, curve(progress)),
        })

    # A curve that is exactly linear needs two keyframes, not two hundred. This
    # keeps baked output tidy when the model asks for linear via the bake path.
    if _is_linear(keys):
        return [keys[0], keys[-1]]
    return keys


def _is_linear(keys: Sequence[dict], tolerance: float = 1e-6) -> bool:
    if len(keys) < 3:
        return True
    first, last = keys[0], keys[-1]
    span = last["time"] - first["time"]
    if span <= 0:
        return True
    for key in keys[1:-1]:
        t = (key["time"] - first["time"]) / span
        expected = _lerp_value(first["value"], last["value"], t)
        actual = key["value"]
        if isinstance(expected, list):
            if any(abs(e - a) > tolerance for e, a in zip(expected, actual)):
                return False
        elif abs(expected - float(actual)) > tolerance:
            return False
    return True


def db_ramp(
    start: float,
    end: float,
    from_db: float,
    to_db: float,
    easing="ease_in_out",
    *,
    fps: float = 30.0,
    resolution: int = 12,
) -> list:
    """Keyframes for an audio level move.

    Audio gets its own helper because fades sound wrong when interpolated
    linearly in dB over long spans; sampling the curve at a modest rate and
    letting Premiere interpolate between samples matches how an editor would
    actually draw the envelope, without burying the clip in keyframes.
    """
    return bake(start, end, from_db, to_db, easing, fps=fps,
                resolution=resolution, max_keys=200)
