"""Declarative schema layer for EditPlan operations.

The design constraint from the brief: the editing model must NOT emit
ExtendScript. It emits data. That means the trust boundary lives here -- every
field of every operation is described once, in data, and validated before a
single byte reaches Premiere. Adding a capability means adding a row to the
catalog, not a new hand-written validator and not a new tool.

Three things live here:

* ``Time``      -- one time model for the whole system (seconds internally).
* ``Selector``  -- how an operation names the thing it acts on.
* ``Field`` / ``OpSpec`` -- the mini schema DSL the catalog is written in.

Everything is plain Python with no third-party dependency so the validator can
run (and be unit-tested) without Premiere, without a panel, and without Qt.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Optional

from premiere.errors import ValidationError

# Premiere's internal time unit. 254016000000 ticks == 1 second, exactly, for
# every frame rate Premiere supports -- it is deliberately divisible by all the
# common rates (24/25/30/50/60/23.976...). We keep seconds at the API boundary
# because it is the only unit a language model reliably reasons about, and
# convert to ticks on the ExtendScript side where the DOM demands them.
TICKS_PER_SECOND = 254016000000

_TIMECODE_RE = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)[:;](\d+)$")
_FRAMES_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*f(?:rames?)?$", re.I)
_SECONDS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?$", re.I)
_MS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*ms$", re.I)


def parse_time(value: Any, *, fps: float = 30.0, path: str = "time") -> float:
    """Normalise any accepted time spelling to float seconds.

    Accepts: 12.5 -> seconds; "12.5s"; "500ms"; "90f"/"90 frames" (needs fps);
    "00:01:03:12" timecode (HH:MM:SS:FF, ';' separator for drop-frame).

    Timecode and frame forms depend on ``fps``. The engine passes the real
    sequence frame rate it read from the host, so these are exact rather than
    guessed -- but a plan that only ever uses seconds is fps-independent, which
    is why seconds is the documented default for the model.
    """
    if isinstance(value, bool):
        raise ValidationError(f"expected a time, got boolean {value!r}", path=path)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if math.isnan(seconds) or math.isinf(seconds):
            raise ValidationError("time must be a finite number", path=path)
        return seconds
    if not isinstance(value, str):
        raise ValidationError(
            f"expected a time, got {type(value).__name__}",
            hint='use seconds (12.5), "12.5s", "500ms", "90f", or "00:01:03:12"',
            path=path,
        )

    text = value.strip()
    if not text:
        raise ValidationError("time is empty", path=path)

    m = _TIMECODE_RE.match(text)
    if m:
        hh, mm, ss, ff = (int(g) for g in m.groups())
        if fps <= 0:
            raise ValidationError("cannot read timecode without a frame rate", path=path)
        if ff >= math.ceil(fps):
            raise ValidationError(
                f"frame field {ff} is out of range for a {fps:g} fps sequence",
                hint=f"frames must be 0-{math.ceil(fps) - 1}",
                path=path,
            )
        return hh * 3600 + mm * 60 + ss + (ff / fps)

    m = _MS_RE.match(text)
    if m:
        return float(m.group(1)) / 1000.0

    m = _FRAMES_RE.match(text)
    if m:
        if fps <= 0:
            raise ValidationError("cannot read a frame count without a frame rate", path=path)
        return float(m.group(1)) / fps

    m = _SECONDS_RE.match(text)
    if m:
        return float(m.group(1))

    try:
        return float(text)
    except ValueError:
        raise ValidationError(
            f"unrecognised time {value!r}",
            hint='use seconds (12.5), "12.5s", "500ms", "90f", or "00:01:03:12"',
            path=path,
        ) from None


def seconds_to_ticks(seconds: float) -> int:
    return int(round(seconds * TICKS_PER_SECOND))


def ticks_to_seconds(ticks: float) -> float:
    return float(ticks) / TICKS_PER_SECOND


def format_timecode(seconds: float, fps: float = 30.0) -> str:
    """Human-readable timecode, for errors and timeline snapshots."""
    if fps <= 0:
        fps = 30.0
    total = max(0.0, float(seconds))
    frames = int(round(total * fps))
    fps_i = int(round(fps))
    ff = frames % fps_i
    total_s = frames // fps_i
    return f"{total_s // 3600:02d}:{(total_s // 60) % 60:02d}:{total_s % 60:02d}:{ff:02d}"


# --------------------------------------------------------------------------
# Selectors -- how an operation names its target
# --------------------------------------------------------------------------

_TRACK_RE = re.compile(r"^([VA])(\d+)$", re.I)

#: Selector keys that pick clips. Documented here (rather than scattered
#: through the catalog) because the model needs one consistent answer to
#: "how do I point at a clip".
CLIP_SELECTOR_KEYS = {"track", "index", "at", "id", "name", "range", "all", "selected"}


def parse_track(value: Any, *, path: str = "track") -> dict:
    """Normalise a track reference to ``{"type": "video"|"audio", "index": n}``.

    Accepts "V1"/"A2" (1-based, matching Premiere's own UI labels), or an
    explicit ``{"type": "video", "index": 0}`` (0-based, matching the DOM).
    The UI spelling is 1-based because that is what a model sees in
    screenshots and tutorials; we convert once, here.
    """
    if isinstance(value, str):
        m = _TRACK_RE.match(value.strip())
        if not m:
            raise ValidationError(
                f"unrecognised track {value!r}",
                hint='use "V1"/"V2" for video or "A1"/"A2" for audio',
                path=path,
            )
        kind = "video" if m.group(1).upper() == "V" else "audio"
        number = int(m.group(2))
        if number < 1:
            raise ValidationError("track numbers start at 1 (V1, A1)", path=path)
        return {"type": kind, "index": number - 1}

    if isinstance(value, dict):
        kind = str(value.get("type", "video")).lower()
        if kind not in ("video", "audio"):
            raise ValidationError(
                f"track type must be 'video' or 'audio', got {kind!r}", path=path
            )
        try:
            index = int(value.get("index", 0))
        except (TypeError, ValueError):
            raise ValidationError("track index must be an integer", path=path) from None
        if index < 0:
            raise ValidationError("track index cannot be negative", path=path)
        return {"type": kind, "index": index}

    raise ValidationError(
        f"expected a track, got {type(value).__name__}",
        hint='use "V1" / "A1", or {"type": "video", "index": 0}',
        path=path,
    )


def parse_clip_selector(value: Any, *, fps: float = 30.0, path: str = "clip") -> dict:
    """Validate and normalise a clip selector.

    Supported forms (all resolved host-side against the live timeline):

    * ``{"track": "V1", "index": 2}``   -- 0-based position within the track
    * ``{"track": "V1", "at": 12.5}``   -- whichever clip covers that time
    * ``{"track": "V1", "range": [a, b]}`` -- every clip overlapping [a, b)
    * ``{"track": "V1", "all": true}``  -- every clip on the track
    * ``{"id": "..."}``                 -- stable id from timeline.snapshot
    * ``{"name": "b-roll.mp4"}``        -- by clip name (optionally + track)
    * ``{"selected": true}``            -- the user's current selection

    Multi-clip forms are legal wherever an operation is marked ``multi`` in the
    catalog; the validator enforces that, so "set opacity on every clip in the
    range" is one operation rather than the model unrolling a loop.
    """
    if not isinstance(value, dict):
        raise ValidationError(
            f"clip selector must be an object, got {type(value).__name__}",
            hint='e.g. {"track": "V1", "index": 0} or {"track": "V1", "at": 12.5}',
            path=path,
        )

    unknown = set(value) - CLIP_SELECTOR_KEYS
    if unknown:
        raise ValidationError(
            f"unknown clip selector key(s): {', '.join(sorted(unknown))}",
            hint=f"valid keys: {', '.join(sorted(CLIP_SELECTOR_KEYS))}",
            path=path,
        )

    out: dict = {}
    if "track" in value:
        out["track"] = parse_track(value["track"], path=f"{path}.track")

    modes = [k for k in ("index", "at", "range", "all", "id", "name", "selected") if k in value]
    if not modes:
        raise ValidationError(
            "clip selector needs one of: index, at, range, all, id, name, selected",
            hint='e.g. {"track": "V1", "index": 0}',
            path=path,
        )

    picks = [k for k in ("index", "at", "range", "all", "id", "selected") if k in value]
    if len(picks) > 1:
        raise ValidationError(
            f"clip selector has conflicting keys: {', '.join(picks)}",
            hint="pick exactly one way to identify the clip",
            path=path,
        )

    if "index" in value:
        try:
            index = int(value["index"])
        except (TypeError, ValueError):
            raise ValidationError("clip index must be an integer", path=f"{path}.index") from None
        if index < 0:
            raise ValidationError("clip index cannot be negative", path=f"{path}.index")
        out["index"] = index
    if "at" in value:
        out["at"] = parse_time(value["at"], fps=fps, path=f"{path}.at")
    if "range" in value:
        rng = value["range"]
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            raise ValidationError(
                "range must be [start, end]", path=f"{path}.range"
            )
        start = parse_time(rng[0], fps=fps, path=f"{path}.range[0]")
        end = parse_time(rng[1], fps=fps, path=f"{path}.range[1]")
        if end <= start:
            raise ValidationError(
                f"range end ({end:g}s) must be after start ({start:g}s)",
                path=f"{path}.range",
            )
        out["range"] = [start, end]
    if "all" in value:
        out["all"] = bool(value["all"])
    if "id" in value:
        out["id"] = str(value["id"])
    if "name" in value:
        out["name"] = str(value["name"])
    if "selected" in value:
        out["selected"] = bool(value["selected"])

    if ("index" in out or "at" in out or "range" in out or out.get("all")) and "track" not in out:
        raise ValidationError(
            "this clip selector needs a track",
            hint='add "track": "V1"',
            path=path,
        )

    return out


def selector_is_multi(selector: dict) -> bool:
    """True when the selector can resolve to more than one clip."""
    return bool(selector.get("range") or selector.get("all") or selector.get("selected")
                or ("name" in selector and "index" not in selector))


# --------------------------------------------------------------------------
# Field types
# --------------------------------------------------------------------------

class Field:
    """One parameter of one operation.

    ``kind`` drives coercion and validation. Keeping this as data (rather than
    a per-op function) is what lets ``caps.operations`` describe the entire
    editing interface back to the model at runtime -- the schema is
    introspectable because it is not code.
    """

    def __init__(
        self,
        kind: str,
        *,
        required: bool = False,
        default: Any = None,
        choices: Optional[list] = None,
        min: Optional[float] = None,
        max: Optional[float] = None,
        item: Optional["Field"] = None,
        fields: Optional[dict] = None,
        doc: str = "",
        check: Optional[Callable[[Any], Optional[str]]] = None,
    ):
        self.kind = kind
        self.required = required
        self.default = default
        self.choices = choices
        self.min = min
        self.max = max
        self.item = item
        self.fields = fields
        self.doc = doc
        self.check = check

    # -- introspection, surfaced by caps.operations ------------------------
    def describe(self) -> dict:
        out: dict = {"type": self.kind, "required": self.required}
        if self.default is not None:
            out["default"] = self.default
        if self.choices:
            out["choices"] = list(self.choices)
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.doc:
            out["doc"] = self.doc
        if self.item is not None:
            out["items"] = self.item.describe()
        if self.fields:
            out["fields"] = {k: v.describe() for k, v in self.fields.items()}
        return out

    # -- validation --------------------------------------------------------
    def validate(self, value: Any, *, fps: float, path: str) -> Any:
        kind = self.kind

        if kind == "time":
            return parse_time(value, fps=fps, path=path)

        if kind == "duration":
            seconds = parse_time(value, fps=fps, path=path)
            if seconds <= 0:
                raise ValidationError(
                    f"duration must be positive, got {seconds:g}s", path=path
                )
            return seconds

        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(
                    f"expected a number, got {type(value).__name__}", path=path
                )
            number = float(value)
            if math.isnan(number) or math.isinf(number):
                raise ValidationError("number must be finite", path=path)
            if self.min is not None and number < self.min:
                raise ValidationError(
                    f"{number:g} is below the minimum {self.min:g}", path=path
                )
            if self.max is not None and number > self.max:
                raise ValidationError(
                    f"{number:g} is above the maximum {self.max:g}", path=path
                )
            return number

        if kind == "integer":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(
                    f"expected an integer, got {type(value).__name__}", path=path
                )
            if isinstance(value, float) and not value.is_integer():
                raise ValidationError(f"expected an integer, got {value!r}", path=path)
            number = int(value)
            if self.min is not None and number < self.min:
                raise ValidationError(
                    f"{number} is below the minimum {int(self.min)}", path=path
                )
            if self.max is not None and number > self.max:
                raise ValidationError(
                    f"{number} is above the maximum {int(self.max)}", path=path
                )
            return number

        if kind == "boolean":
            if not isinstance(value, bool):
                raise ValidationError(
                    f"expected true/false, got {type(value).__name__}", path=path
                )
            return value

        if kind == "string":
            if not isinstance(value, str):
                raise ValidationError(
                    f"expected a string, got {type(value).__name__}", path=path
                )
            if self.choices and value not in self.choices:
                raise ValidationError(
                    f"{value!r} is not valid here",
                    hint=f"expected one of: {', '.join(map(str, self.choices))}",
                    path=path,
                )
            return value

        if kind == "enum":
            if not isinstance(value, str):
                raise ValidationError(
                    f"expected one of {', '.join(map(str, self.choices or []))}, "
                    f"got {type(value).__name__}",
                    path=path,
                )
            lowered = value.strip().lower()
            for choice in self.choices or []:
                if lowered == str(choice).lower():
                    return choice
            raise ValidationError(
                f"{value!r} is not valid here",
                hint=f"expected one of: {', '.join(map(str, self.choices or []))}",
                path=path,
            )

        if kind == "track":
            return parse_track(value, path=path)

        if kind == "clip":
            return parse_clip_selector(value, fps=fps, path=path)

        if kind == "value":
            # A property value: number, bool, string, or a point/colour list.
            # Deliberately permissive -- what a given Premiere parameter
            # accepts is only knowable from the host, so the real check
            # happens there and comes back as a typed host error.
            if isinstance(value, (list, tuple)):
                for i, element in enumerate(value):
                    if isinstance(element, bool) or not isinstance(element, (int, float)):
                        raise ValidationError(
                            "list property values must be all numbers "
                            "(e.g. [x, y] position or [r, g, b, a] colour)",
                            path=f"{path}[{i}]",
                        )
                return [float(v) for v in value]
            if isinstance(value, (bool, int, float, str)):
                return value
            raise ValidationError(
                f"unsupported property value type {type(value).__name__}",
                hint="use a number, boolean, string, or list of numbers",
                path=path,
            )

        if kind == "list":
            if not isinstance(value, (list, tuple)):
                raise ValidationError(
                    f"expected a list, got {type(value).__name__}", path=path
                )
            if self.min is not None and len(value) < self.min:
                raise ValidationError(
                    f"needs at least {int(self.min)} item(s), got {len(value)}", path=path
                )
            if self.max is not None and len(value) > self.max:
                raise ValidationError(
                    f"accepts at most {int(self.max)} item(s), got {len(value)}", path=path
                )
            if self.item is None:
                return list(value)
            return [
                self.item.validate(v, fps=fps, path=f"{path}[{i}]")
                for i, v in enumerate(value)
            ]

        if kind == "object":
            if not isinstance(value, dict):
                raise ValidationError(
                    f"expected an object, got {type(value).__name__}", path=path
                )
            if self.fields is None:
                return dict(value)
            return validate_fields(self.fields, value, fps=fps, path=path)

        if kind == "any":
            return value

        raise ValidationError(f"internal: unknown field kind {kind!r}", path=path)


def validate_fields(fields: dict, payload: dict, *, fps: float, path: str = "") -> dict:
    """Validate ``payload`` against a ``{name: Field}`` map."""
    if not isinstance(payload, dict):
        raise ValidationError(
            f"expected an object, got {type(payload).__name__}", path=path or "params"
        )

    prefix = f"{path}." if path else ""
    known = set(fields)
    unknown = set(payload) - known
    if unknown:
        near = _suggest(sorted(unknown)[0], known)
        raise ValidationError(
            f"unknown parameter(s): {', '.join(sorted(unknown))}",
            hint=(f"did you mean {near!r}?" if near else
                  f"valid parameters: {', '.join(sorted(known))}"),
            path=path or "params",
        )

    out: dict = {}
    for name, field in fields.items():
        if name in payload and payload[name] is not None:
            out[name] = field.validate(payload[name], fps=fps, path=f"{prefix}{name}")
        elif field.required:
            raise ValidationError(
                f"missing required parameter {name!r}",
                hint=field.doc or f"expected a {field.kind}",
                path=f"{prefix}{name}",
            )
        elif field.default is not None:
            out[name] = field.default

    for name, field in fields.items():
        if field.check and name in out:
            problem = field.check(out[name])
            if problem:
                raise ValidationError(problem, path=f"{prefix}{name}")

    return out


def _suggest(word: str, candidates) -> Optional[str]:
    """Cheap nearest-name suggestion so typos self-correct in one retry."""
    best, best_score = None, 0.0
    for candidate in candidates:
        score = _similar(word.lower(), candidate.lower())
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 0.6 else None


def _similar(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    # Dice coefficient on character bigrams -- good enough for typo hints and
    # avoids pulling in difflib for a hot validation path.
    pairs_a = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    pairs_b = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    overlap = len(pairs_a & pairs_b)
    return 2.0 * overlap / (len(pairs_a) + len(pairs_b))


class OpSpec:
    """One operation in the editing interface."""

    def __init__(
        self,
        name: str,
        *,
        summary: str,
        fields: dict,
        category: str = "general",
        mutating: bool = True,
        multi: bool = False,
        needs_sequence: bool = True,
        supported: bool = True,
        unsupported_reason: str = "",
        alternative: str = "",
        notes: str = "",
    ):
        self.name = name
        self.summary = summary
        self.fields = fields
        self.category = category
        self.mutating = mutating
        self.multi = multi
        self.needs_sequence = needs_sequence
        self.supported = supported
        self.unsupported_reason = unsupported_reason
        self.alternative = alternative
        self.notes = notes

    def validate(self, params: dict, *, fps: float) -> dict:
        validated = validate_fields(self.fields, params or {}, fps=fps, path="")
        if not self.multi:
            for key, field in self.fields.items():
                if field.kind == "clip" and key in validated:
                    if selector_is_multi(validated[key]):
                        raise ValidationError(
                            f"{self.name} acts on a single clip, but this selector "
                            f"can match several",
                            hint="use index/at/id to name one clip, or use a "
                                 "batch of operations",
                            path=key,
                        )
        return validated

    def describe(self) -> dict:
        out = {
            "op": self.name,
            "summary": self.summary,
            "category": self.category,
            "mutating": self.mutating,
            "params": {k: v.describe() for k, v in self.fields.items()},
        }
        if self.multi:
            out["multi_clip"] = True
        if self.notes:
            out["notes"] = self.notes
        if not self.supported:
            out["supported"] = False
            out["reason"] = self.unsupported_reason
            if self.alternative:
                out["alternative"] = self.alternative
        return out
