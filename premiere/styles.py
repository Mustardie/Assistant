"""Reusable text styles and template registration.

A video looks edited rather than generated when its titles, lower thirds and
subtitles share one look. Rather than making the model repeat a dozen
typography fields on every ``text.create`` (and drift on the twentieth one),
styles are defined once by name and referenced.

Stored as plain JSON in ``data/`` alongside the repo's other state, so styles
survive restarts and can be inspected or hand-edited.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("nova.premiere.styles")

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "premiere_styles.json",
)

_lock = threading.RLock()
_cache: Optional[dict] = None

#: Sensible starting points so a model has something usable before anyone has
#: defined a style. Chosen to read well at 1080p on YouTube.
BUILTIN_STYLES = {
    "title": {
        "font": "Arial", "size": 96, "weight": "black", "color": "#FFFFFF",
        "align": "center", "stroke_color": "#000000", "stroke_width": 4,
        "shadow": {"color": "#000000B0", "offset": [4, 6], "blur": 12},
    },
    "subtitle": {
        "font": "Arial", "size": 54, "weight": "bold", "color": "#FFFFFF",
        "align": "center", "stroke_color": "#000000", "stroke_width": 3,
        "max_width": 0.8,
    },
    "caption_box": {
        "font": "Arial", "size": 48, "weight": "semibold", "color": "#FFFFFF",
        "align": "center", "max_width": 0.75,
        "background": {"color": "#000000B0", "padding": 22, "radius": 10},
    },
    "lower_third": {
        "font": "Arial", "size": 44, "weight": "semibold", "color": "#FFFFFF",
        "align": "left", "letter_spacing": 1.0,
        "shadow": {"color": "#00000090", "offset": [2, 3], "blur": 6},
    },
    "emphasis": {
        "font": "Arial", "size": 110, "weight": "black", "color": "#FFD600",
        "align": "center", "stroke_color": "#000000", "stroke_width": 6,
    },
}


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data = {"styles": {}, "mogrt": None}
        try:
            with open(_PATH, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data.update(loaded)
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Premiere styles unreadable (%s); using built-ins", exc)
        _cache = data
        return _cache


def _save() -> None:
    data = _load()
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = f"{_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, _PATH)
    except OSError as exc:
        logger.warning("Could not save Premiere styles: %s", exc)


def get_style(name: str) -> dict:
    """A defined style by name, falling back to the built-ins."""
    if not name:
        return {}
    styles = _load()["styles"]
    if name in styles:
        return dict(styles[name])
    return dict(BUILTIN_STYLES.get(name, {}))


def define_style(name: str, style: dict) -> dict:
    with _lock:
        _load()["styles"][name] = dict(style)
        _save()
    return style


def delete_style(name: str) -> bool:
    with _lock:
        removed = _load()["styles"].pop(name, None) is not None
        if removed:
            _save()
    return removed


def list_styles() -> dict:
    data = _load()
    combined = dict(BUILTIN_STYLES)
    combined.update(data["styles"])
    return combined


def default_mogrt() -> Optional[str]:
    """Path to a .mogrt template used for live (non-rendered) text, if set."""
    path = _load().get("mogrt")
    if path and os.path.isfile(path):
        return path
    return None


def set_default_mogrt(path: Optional[str]) -> Optional[str]:
    """Register a .mogrt so ``text.create`` can produce live, editable text.

    Without one, text falls back to rendered PNG overlays -- which look
    identical on export but are not editable inside Premiere.
    """
    with _lock:
        _load()["mogrt"] = path
        _save()
    return path
