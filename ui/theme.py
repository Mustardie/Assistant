"""
Nova design tokens — the single source of truth for every color, radius,
spacing, font and motion value in the application.

The language is the Material-3 "Nova" redesign:

    deep neutral backgrounds, glass cards, a soft periwinkle AI accent
    (#c0c1ff), lilac secondary (#ddb7ff), peach tertiary (#ffb783),
    rounded-2xl surfaces, Inter-first typography.

Two palettes (dark / light) are selectable from Settings. The active
palette is chosen by the NOVA_THEME env var at import time (main.py
applies the persisted choice before the UI is imported) and can be
swapped live via apply_theme() — every custom-painted widget reads these
module-level tokens at paint time, and QSS-based widgets re-apply their
stylesheets through the refresh() callback registry.
"""
from __future__ import annotations

import os
import time
from typing import Callable

from PySide6.QtGui import QColor

# --------------------------------------------------------------------------- #
# Assistant name — driven by settings (env is set by main.py before import)
# --------------------------------------------------------------------------- #
ASSISTANT_NAME = (os.getenv("ASSISTANT_NAME", "Nova").strip() or "Nova")

# --------------------------------------------------------------------------- #
# Palettes — Material-3 dark / light.
# --------------------------------------------------------------------------- #
THEMES = {
    "dark": {
        "BG_0": "#0E0E0E", "BG_1": "#131313", "BG_2": "#1E1E1E",
        "BG_3": "#272727", "BG_4": "#353534",
        "SURFACE": "#1E1E1E", "SURFACE_HIGH": "#272727",
        "SURFACE_HIGHEST": "#353534", "SURFACE_CONTAINER": "#1E1E1E",
        "BORDER": "rgba(255, 255, 255, 0.08)",
        "BORDER_STRONG": "rgba(255, 255, 255, 0.14)",
        "BORDER_FOCUS": "rgba(192, 193, 255, 0.65)",
        "TEXT": "#E5E2E1", "TEXT_SOFT": "#C2BFBD", "TEXT_FAINT": "#8F8D8B",
        "ACCENT": "#C0C1FF", "ACCENT_DEEP": "#A6A7FF", "ACCENT_2": "#DDB7FF",
        "ACCENT_GLOW": "rgba(192, 193, 255, 0.35)",
        "ACCENT_GLOW_2": "rgba(221, 183, 255, 0.22)",
        "TERTIARY": "#FFB783",
        "STATUS_LISTENING": "#C0C1FF", "STATUS_THINKING": "#DDB7FF",
        "STATUS_SPEAKING": "#7CE08A", "STATUS_IDLE": "#8F8D8B",
        "CODE_BG": "#0D0D0D",
        "GRADIENT_1": "#A5B4FC", "GRADIENT_2": "#C084FC", "GRADIENT_3": "#F9A8D4",
        "ON_ACCENT": "#1B1B21",
        "SURFACE_GLASS": "rgba(255, 255, 255, 0.04)",
        "GLASS": "rgba(255, 255, 255, 0.04)",
        "HOVER": "rgba(255, 255, 255, 0.09)",
        "HOVER_STRONG": "rgba(255, 255, 255, 0.16)",
        "SCROLL_HANDLE": "rgba(255, 255, 255, 0.10)",
        "SCROLL_HANDLE_HOVER": "rgba(255, 255, 255, 0.20)",
    },
    "light": {
        "BG_0": "#E9E7E2", "BG_1": "#FFFFFF", "BG_2": "#F5F3EF",
        "BG_3": "#EAE8E3", "BG_4": "#DDDBD5",
        "SURFACE": "#F5F3EF", "SURFACE_HIGH": "#EAE8E3",
        "SURFACE_HIGHEST": "#DDDBD5", "SURFACE_CONTAINER": "#F5F3EF",
        "BORDER": "rgba(28, 27, 31, 0.10)",
        "BORDER_STRONG": "rgba(28, 27, 31, 0.18)",
        "BORDER_FOCUS": "rgba(85, 88, 210, 0.55)",
        "TEXT": "#000000", "TEXT_SOFT": "#000000", "TEXT_FAINT": "#000000",
        "ACCENT": "#5558D2", "ACCENT_DEEP": "#4547C8", "ACCENT_2": "#9459E0",
        "ACCENT_GLOW": "rgba(85, 88, 210, 0.28)",
        "ACCENT_GLOW_2": "rgba(148, 89, 224, 0.20)",
        "TERTIARY": "#B3550A",
        "STATUS_LISTENING": "#5558D2", "STATUS_THINKING": "#9459E0",
        "STATUS_SPEAKING": "#2E9E4F", "STATUS_IDLE": "#000000",
        "CODE_BG": "#F4F3F0",
        "GRADIENT_1": "#4F46E5", "GRADIENT_2": "#9333EA", "GRADIENT_3": "#DB2777",
        "ON_ACCENT": "#FFFFFF",
        "SURFACE_GLASS": "#F5F3EF",
        "GLASS": "#F5F3EF",
        "HOVER": "rgba(28, 27, 31, 0.05)",
        "HOVER_STRONG": "rgba(28, 27, 31, 0.10)",
        "SCROLL_HANDLE": "rgba(28, 27, 31, 0.18)",
        "SCROLL_HANDLE_HOVER": "rgba(28, 27, 31, 0.30)",
    },
}

THEME_NAMES = {
    "dark": "Dark",
    "light": "Light",
}

# Settings written by the old UI (or env vars from older configs) map to
# the new two-theme system.
_LEGACY_THEME_MAP = {
    "space_black": "dark",
    "space_mint": "dark",
    "light_brown": "dark",
}


def _resolve_theme(name: str) -> str:
    name = (name or "").strip().lower()
    name = _LEGACY_THEME_MAP.get(name, name)
    return name if name in THEMES else "dark"


THEME_NAME = _resolve_theme(os.getenv("NOVA_THEME", "dark"))
_PALETTE = THEMES[THEME_NAME]

_apply_listeners: list[Callable[[str], None]] = []


def rgba(hex_or_rgba: str, alpha: int) -> str:
    """Return an rgba() CSS string for a hex token at the given alpha (0-255)."""
    color = QColor(hex_or_rgba)
    if not color.isValid():
        return hex_or_rgba
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


def apply_theme(name: str) -> str:
    """Switch the active palette live and notify every registered widget."""
    global THEME_NAME, _PALETTE
    name = _resolve_theme(name)
    if name == THEME_NAME and _PALETTE is not None:
        return name
    THEME_NAME = name
    _PALETTE = THEMES[name]
    _reassign_tokens()
    for cb in list(_apply_listeners):
        try:
            cb(name)
        except Exception:
            pass
    return name


def on_apply(cb: Callable[[str], None]):
    """Register a callback fired after every apply_theme() call."""
    _apply_listeners.append(cb)


def current() -> dict:
    return _PALETTE


# --------------------------------------------------------------------------- #
# Surfaces
# --------------------------------------------------------------------------- #
BG_0 = _PALETTE["BG_0"]     # deepest backdrop (outer frame)
BG_1 = _PALETTE["BG_1"]     # window base
BG_2 = _PALETTE["BG_2"]     # elevated surface (cards, docks)
BG_3 = _PALETTE["BG_3"]     # hover / raised elements
BG_4 = _PALETTE["BG_4"]     # active / pressed elements

SURFACE = _PALETTE["SURFACE"]
SURFACE_HIGH = _PALETTE["SURFACE_HIGH"]
SURFACE_HIGHEST = _PALETTE["SURFACE_HIGHEST"]
SURFACE_CONTAINER = _PALETTE["SURFACE_CONTAINER"]
CODE_BG = _PALETTE["CODE_BG"]

SURFACE_GLASS = _PALETTE["SURFACE_GLASS"]       # glass wash over the backdrop
GLASS = _PALETTE["GLASS"]                        # elevated glass card fill
HOVER = _PALETTE["HOVER"]                        # hover wash
HOVER_STRONG = _PALETTE["HOVER_STRONG"]          # pressed wash
SCROLL_HANDLE = _PALETTE["SCROLL_HANDLE"]
SCROLL_HANDLE_HOVER = _PALETTE["SCROLL_HANDLE_HOVER"]
BORDER = _PALETTE["BORDER"]
BORDER_STRONG = _PALETTE["BORDER_STRONG"]
BORDER_FOCUS = _PALETTE["BORDER_FOCUS"]

# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
TEXT = _PALETTE["TEXT"]
TEXT_SOFT = _PALETTE["TEXT_SOFT"]
TEXT_FAINT = _PALETTE["TEXT_FAINT"]
TEXT_INVERSE = "#FFFFFF"

# --------------------------------------------------------------------------- #
# Accent
# --------------------------------------------------------------------------- #
ACCENT = _PALETTE["ACCENT"]
ACCENT_DEEP = _PALETTE["ACCENT_DEEP"]
ACCENT_2 = _PALETTE["ACCENT_2"]
ACCENT_GLOW = _PALETTE["ACCENT_GLOW"]
ACCENT_GLOW_2 = _PALETTE["ACCENT_GLOW_2"]
TERTIARY = _PALETTE["TERTIARY"]
ON_ACCENT = _PALETTE["ON_ACCENT"]
GRADIENT_1 = _PALETTE["GRADIENT_1"]
GRADIENT_2 = _PALETTE["GRADIENT_2"]
GRADIENT_3 = _PALETTE["GRADIENT_3"]

# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
STATUS_LISTENING = _PALETTE["STATUS_LISTENING"]
STATUS_THINKING = _PALETTE["STATUS_THINKING"]
STATUS_SPEAKING = _PALETTE["STATUS_SPEAKING"]
STATUS_IDLE = _PALETTE["STATUS_IDLE"]
STATUS_ERROR = "#E5484D"

# --------------------------------------------------------------------------- #
# Radius
# --------------------------------------------------------------------------- #
R_XS = 8
R_SM = 10
R_MD = 14
R_LG = 18
R_XL = 24
R_PILL = 999

# --------------------------------------------------------------------------- #
# Spacing
# --------------------------------------------------------------------------- #
S_XS = 4
S_SM = 8
S_MD = 12
S_LG = 16
S_XL = 20
S_XXL = 28
S_XXXL = 40

# --------------------------------------------------------------------------- #
# Typography
# --------------------------------------------------------------------------- #
FONT_FAMILY = (
    '"Inter", "Segoe UI Variable Text", "Segoe UI", '
    '"Helvetica Neue", "Arial", sans-serif'
)
FONT_CODE = '"Cascadia Code", "Consolas", "SF Mono", "Menlo", monospace'
FONT_H1 = 26
FONT_H2 = 21
FONT_H3 = 16
FONT_BODY = 14
FONT_SMALL = 12.5
FONT_TINY = 11

# --------------------------------------------------------------------------- #
# Motion (ms)
# --------------------------------------------------------------------------- #
MOTION_FAST = 120
MOTION_NORMAL = 220
MOTION_SLOW = 320
MOTION_SIDEBAR = 260
MOTION_FADE = 180

# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
WINDOW_W = 1280
WINDOW_H = 800
WINDOW_MIN_W = 1080
WINDOW_MIN_H = 660

SHELL_MARGIN = 12          # outer glow margin around the window body
SHELL_RADIUS = R_LG

TITLEBAR_H = 64            # the top app bar
SIDEBAR_W = 256
SIDEBAR_RADIUS = 0
NAV_ITEM_H = 42

INPUT_H = 58
INPUT_MAX_H = 140

STATUS_LABELS = {
    "idle": "System ready",
    "listening": "Listening…",
    "thinking": "Thinking…",
    "speaking": "Speaking…",
}

# --------------------------------------------------------------------------- #
# Token reassignment (live theme switching)
# --------------------------------------------------------------------------- #
_TOKEN_NAMES = (
    "BG_0", "BG_1", "BG_2", "BG_3", "BG_4",
    "SURFACE", "SURFACE_HIGH", "SURFACE_HIGHEST", "SURFACE_CONTAINER",
    "SURFACE_GLASS", "GLASS", "HOVER", "HOVER_STRONG",
    "SCROLL_HANDLE", "SCROLL_HANDLE_HOVER",
    "CODE_BG", "BORDER", "BORDER_STRONG", "BORDER_FOCUS",
    "TEXT", "TEXT_SOFT", "TEXT_FAINT",
    "ACCENT", "ACCENT_DEEP", "ACCENT_2", "ACCENT_GLOW", "ACCENT_GLOW_2",
    "TERTIARY", "ON_ACCENT", "GRADIENT_1", "GRADIENT_2", "GRADIENT_3",
    "STATUS_LISTENING", "STATUS_THINKING", "STATUS_SPEAKING", "STATUS_IDLE",
)


def _reassign_tokens():
    for name in _TOKEN_NAMES:
        globals()[name] = _PALETTE[name]


# --------------------------------------------------------------------------- #
# QSS helpers — read the CURRENT palette at call time.
# --------------------------------------------------------------------------- #

def text_qss(size: float = FONT_BODY, weight: int = 400, color: str | None = None,
             family: str | None = None, spacing: float = 0.0) -> str:
    return (
        f"color: {color or TEXT}; font-family: {family or FONT_FAMILY};"
        f" font-size: {size}px; font-weight: {weight};"
        f" letter-spacing: {spacing}px; background: transparent;"
    )


def hint_qss(size: float = FONT_TINY, color: str | None = None) -> str:
    return text_qss(size=size, weight=400, color=color or TEXT_FAINT)


def scroll_qss() -> str:
    return (
        "QScrollArea { background: transparent; border: none; }"
        "QScrollArea > QWidget > QWidget { background: transparent; }"
        "QScrollBar:vertical { background: transparent; width: 8px; border: none; margin: 2px; }"
        "QScrollBar::handle:vertical { background: {SCROLL_HANDLE}; border-radius: 4px; min-height: 28px; }"
        "QScrollBar::handle:vertical:hover { background: {SCROLL_HANDLE_HOVER}; }"
        "QScrollBar:horizontal { background: transparent; height: 8px; border: none; margin: 2px; }"
        "QScrollBar::handle:horizontal { background: {SCROLL_HANDLE}; border-radius: 4px; min-width: 28px; }"
        "QScrollBar::handle:horizontal:hover { background: {SCROLL_HANDLE_HOVER}; }"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }"
    )


def edit_qss() -> str:
    """QSS for QLineEdit/QTextEdit inner styling (colors only; the frame is
    painted by GlassLineEdit/InputDock, so backgrounds stay transparent)."""
    return (
        f"QLineEdit, QTextEdit {{ color: {TEXT}; background: transparent;"
        f" border: none; selection-background-color: {rgba(ACCENT, 90)};"
        f" selection-color: {TEXT}; font-family: {FONT_FAMILY}; }}"
        f"QLineEdit {{ font-size: {FONT_BODY}px; }}"
        f"QLineEdit::placeholder, QTextEdit::placeholder {{ color: {TEXT_FAINT}; }}"
    )


def combo_qss() -> str:
    return (
        f"QComboBox {{ color: {TEXT}; background: transparent; border: none;"
        f" font-family: {FONT_FAMILY}; font-size: {FONT_SMALL}px; }}"
        f"QComboBox QAbstractItemView {{ background-color: {BG_3}; color: {TEXT};"
        f" border: 1px solid {BORDER_STRONG}; border-radius: 10px; outline: none;"
        f" padding: 6px; selection-background-color: {rgba(ACCENT, 70)};"
        f" selection-color: {TEXT}; font-family: {FONT_FAMILY};"
        f" font-size: {FONT_SMALL}px; }}"
        f"QComboBox QAbstractItemView::item {{ padding: 7px 10px; border-radius: 6px; }}"
    )


def menu_qss() -> str:
    return (
        f"QMenu {{ background-color: {BG_3}; color: {TEXT}; border: 1px solid"
        f" {BORDER_STRONG}; border-radius: 12px; padding: 6px;"
        f" font-family: {FONT_FAMILY}; font-size: {FONT_SMALL}px; }}"
        f"QMenu::item {{ padding: 8px 26px 8px 12px; border-radius: 8px; }}"
        f"QMenu::item:selected {{ background-color: {rgba(ACCENT, 65)}; color: {TEXT}; }}"
    )
