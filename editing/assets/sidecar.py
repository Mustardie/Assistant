"""Optional per-file metadata: ``<filename>.asset.json``.

A sidecar is how a user says the things a filename cannot: that a track loops
cleanly, that a sting is too loud by 3 dB, that a piece of music is licensed
for this channel, that a sound is *right* but should never be placed
automatically.

The rule that shapes this module is in the brief: **an invalid sidecar must not
crash anything.** A user hand-editing JSON at midnight will leave a trailing
comma, and the correct response is not a stack trace in the middle of indexing
four hundred files. So:

* every parse failure returns a ``Sidecar`` carrying the problem rather than
  raising;
* a file with an unreadable sidecar is indexed anyway, marked ``needs_review``,
  and **taken out of automatic placement** — because metadata we could not read
  is not the same as metadata that said "safe";
* individual bad *fields* are dropped with a note while the rest of the
  document is kept, so one mistyped ``intensity`` does not throw away good tags.

That last point is the difference between a format people can edit by hand and
one they give up on.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from editing.assets.schema import (
    CATEGORIES, INTENSITIES, AssetTag,
)
from editing.schema import _slug, as_str_list

#: The suffix that makes a file a sidecar for its neighbour.
SUFFIX = ".asset.json"

#: Keys that mean something here. Anything else is preserved in ``extra`` and
#: reported, so a typo like ``looppable`` is visible rather than silent.
KNOWN_FIELDS = frozenset({
    "category", "tags", "intensity", "mood", "moods", "style", "styles",
    "bpm", "loopable", "safe_for_auto", "preferred_styles", "avoid_styles",
    "license_notes", "start_offset", "end_offset", "volume_adjust_db",
    "notes", "usage_notes", "duration", "loudness_db", "hud_risk",
})

#: Keys beginning with an underscore are comments by convention, as in the
#: generated example. Never reported as unknown.
_COMMENT_PREFIX = "_"


@dataclass
class Sidecar:
    """A parsed sidecar, or the reason there isn't one."""

    path: str = ""
    found: bool = False
    ok: bool = False
    data: dict = field(default_factory=dict)
    #: Fatal: the document could not be read at all.
    error: str = ""
    #: Non-fatal: individual fields dropped, unknown keys seen.
    problems: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        """Whether this asset should be held out of automatic placement.

        A fatal error only. Dropped fields are reported but do not disqualify
        the asset -- the rest of the document was still readable, and refusing
        to use a good sound because its BPM was spelled wrong would be worse
        than ignoring the BPM.
        """
        return self.found and not self.ok

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "found": self.found,
            "ok": self.ok,
            "error": self.error,
            "problems": list(self.problems),
        }


def sidecar_path(media_path: str | Path) -> Path:
    """The sidecar that would describe this file.

    ``impact_boom.wav`` -> ``impact_boom.asset.json``. The whole filename minus
    its extension, so ``a.b.wav`` -> ``a.b.asset.json`` rather than
    ``a.asset.json``.
    """
    target = Path(media_path)
    return target.with_name(target.stem + SUFFIX)


def load(media_path: str | Path) -> Sidecar:
    """Read the sidecar beside ``media_path``. Never raises."""
    path = sidecar_path(media_path)
    if not path.exists():
        return Sidecar(path=str(path), found=False, ok=False)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Sidecar(
            path=str(path), found=True, ok=False,
            error=f"could not be read: {exc}",
        )

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Sidecar(
            path=str(path), found=True, ok=False,
            error=f"is not valid JSON (line {exc.lineno}, column {exc.colno}): "
                  f"{exc.msg}",
        )

    if not isinstance(document, dict):
        return Sidecar(
            path=str(path), found=True, ok=False,
            error=f"must be a JSON object, not a {type(document).__name__}",
        )

    clean, problems = _clean(document)
    return Sidecar(path=str(path), found=True, ok=True, data=clean,
                   problems=problems)


def _clean(document: dict) -> tuple:
    """Coerce a raw document field by field, dropping what makes no sense."""
    out: dict = {}
    problems: list[str] = []

    for key, value in document.items():
        if key.startswith(_COMMENT_PREFIX):
            continue
        if key not in KNOWN_FIELDS:
            problems.append(
                f"'{key}' is not a field this system reads; it was ignored."
            )

    category = _slug(document.get("category"))
    if category:
        if category in CATEGORIES:
            out["category"] = category
        else:
            problems.append(
                f"category '{document.get('category')}' is not one of: "
                + ", ".join(CATEGORIES)
            )

    intensity = _slug(document.get("intensity"))
    if intensity:
        if intensity in INTENSITIES:
            out["intensity"] = intensity
        else:
            problems.append(
                f"intensity '{document.get('intensity')}' is not one of: "
                + ", ".join(INTENSITIES)
            )

    tags = _tag_list(document.get("tags"))
    if tags:
        out["tags"] = tags

    for source_key, target in (("mood", "moods"), ("moods", "moods"),
                               ("style", "styles"), ("styles", "styles")):
        values = as_str_list(document.get(source_key), limit=30)
        if values:
            merged = out.get(target, []) + [
                _slug(value) for value in values if _slug(value)
            ]
            out[target] = sorted(dict.fromkeys(merged))

    for key in ("preferred_styles", "avoid_styles"):
        values = [_slug(value) for value in as_str_list(document.get(key), limit=20)]
        values = [value for value in values if value]
        if values:
            out[key] = values

    for key in ("bpm", "duration", "loudness_db", "volume_adjust_db",
                "start_offset", "end_offset"):
        if key not in document or document[key] is None:
            continue
        number = _number(document[key])
        if number is None:
            problems.append(f"{key} '{document[key]}' is not a number.")
            continue
        if key in ("bpm", "duration", "start_offset") and number < 0:
            problems.append(f"{key} cannot be negative ({number:g}).")
            continue
        out[key] = number

    if "start_offset" in out and "end_offset" in out:
        if out["end_offset"] <= out["start_offset"]:
            problems.append(
                f"end_offset ({out['end_offset']:g}) is not after start_offset "
                f"({out['start_offset']:g}); both were ignored."
            )
            out.pop("end_offset", None)
            out.pop("start_offset", None)

    for key in ("loopable", "safe_for_auto", "hud_risk"):
        if key not in document:
            continue
        flag = _boolean(document[key])
        if flag is None:
            problems.append(
                f"{key} '{document[key]}' is not true or false; it was ignored."
            )
            continue
        out[key] = flag

    for key in ("license_notes", "notes", "usage_notes"):
        value = document.get(key)
        if value:
            out[key] = str(value)[:600]

    return out, problems


def _tag_list(value: Any) -> list:
    """Tags, as ``AssetTag`` records sourced to the sidecar.

    Sidecar tags carry the highest confidence of any source: a person typed
    them on purpose, which beats anything inferred from a folder or a filename.
    """
    out: list = []
    for entry in as_str_list(value, limit=40):
        name = _slug(entry)
        if name:
            out.append(AssetTag(name=name, source="sidecar", confidence=1.0))
    return out


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) == float("inf"):
        return None
    return number


def _boolean(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("true", "yes", "1", "on"):
            return True
        if token in ("false", "no", "0", "off"):
            return False
    return None
