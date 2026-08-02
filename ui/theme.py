"""
Nova design tokens — the single source of truth for every color, radius,
spacing, font and motion value in the application.

Every widget pulls its values from here instead of hardcoding, so a full
re-skin is a one-file change. The language is premium dark glassmorphism:

    deep charcoal backgrounds, elevated dark cards, soft white/gray text,
    a quiet blue->purple AI accent, gentle glows, rounded corners.

If Inter is installed on the system it is used first; otherwise Qt falls
back through Segoe UI Variable / Segoe UI automatically (QSS supports
comma-separated font-family lists).
"""
from __future__ import annotations

import os

from PySide6.QtGui import QColor

# --------------------------------------------------------------------------- #
# Assistant name — driven by settings (env is set by main.py before import)
# --------------------------------------------------------------------------- #
ASSISTANT_NAME = (os.getenv("ASSISTANT_NAME", "Nova").strip() or "Nova")

# --------------------------------------------------------------------------- #
# Themes — three complete palettes selectable from Settings.
# NOVA_THEME env picks the palette at import time (main.py applies the
# persisted choice before the UI is imported).
# --------------------------------------------------------------------------- #
THEMES = {
    "space_black": {
        "BG_0": "#07090C", "BG_1": "#0B0E13", "BG_2": "#10141B",
        "BG_3": "#161B25", "BG_4": "#1D2330",
        "BORDER": "rgba(255, 255, 255, 0.07)",
        "BORDER_STRONG": "rgba(255, 255, 255, 0.13)",
        "BORDER_FOCUS": "rgba(124, 130, 255, 0.55)",
        "TEXT": "#EDF0F6", "TEXT_SOFT": "#9AA6B8", "TEXT_FAINT": "#5E6A7D",
        "ACCENT": "#6E8BFF", "ACCENT_DEEP": "#5A6CFF", "ACCENT_2": "#9B6DFF",
        "ACCENT_GLOW": "rgba(110, 122, 255, 0.34)",
        "ACCENT_GLOW_2": "rgba(155, 109, 255, 0.22)",
        "STATUS_LISTENING": "#6E8BFF", "STATUS_THINKING": "#9B6DFF",
        "STATUS_SPEAKING": "#4CD6A5", "STATUS_IDLE": "#5E6A7D",
    },
    "light_brown": {
        "BG_0": "#1C140C", "BG_1": "#271C10", "BG_2": "#332517",
        "BG_3": "#3E2E1D", "BG_4": "#4A3826",
        "BORDER": "rgba(255, 236, 200, 0.10)",
        "BORDER_STRONG": "rgba(255, 236, 200, 0.18)",
        "BORDER_FOCUS": "rgba(224, 160, 92, 0.60)",
        "TEXT": "#F5EDDD", "TEXT_SOFT": "#CBB297", "TEXT_FAINT": "#8E7760",
        "ACCENT": "#E8A24D", "ACCENT_DEEP": "#D98A33", "ACCENT_2": "#C46F52",
        "ACCENT_GLOW": "rgba(232, 162, 77, 0.34)",
        "ACCENT_GLOW_2": "rgba(196, 111, 82, 0.22)",
        "STATUS_LISTENING": "#E8A24D", "STATUS_THINKING": "#C46F52",
        "STATUS_SPEAKING": "#6FBF8F", "STATUS_IDLE": "#8E7760",
    },
    "space_mint": {
        "BG_0": "#03151B", "BG_1": "#06202A", "BG_2": "#0A2A37",
        "BG_3": "#0F3746", "BG_4": "#154558",
        "BORDER": "rgba(178, 255, 228, 0.08)",
        "BORDER_STRONG": "rgba(178, 255, 228, 0.15)",
        "BORDER_FOCUS": "rgba(76, 214, 165, 0.55)",
        "TEXT": "#E7F6F1", "TEXT_SOFT": "#9DC8BD", "TEXT_FAINT": "#5F8A80",
        "ACCENT": "#2FBF9F", "ACCENT_DEEP": "#1FA98B", "ACCENT_2": "#4FB3E8",
        "ACCENT_GLOW": "rgba(47, 191, 159, 0.34)",
        "ACCENT_GLOW_2": "rgba(79, 179, 232, 0.22)",
        "STATUS_LISTENING": "#2FBF9F", "STATUS_THINKING": "#4FB3E8",
        "STATUS_SPEAKING": "#4CD6A5", "STATUS_IDLE": "#5F8A80",
    },
}

THEME_NAMES = {
    "space_black": "Space Black",
    "light_brown": "Light Brown",
    "space_mint": "Space Mint",
}


def _resolve_theme() -> str:
    name = os.getenv("NOVA_THEME", "space_black").strip().lower()
    return name if name in THEMES else "space_black"


THEME_NAME = _resolve_theme()
_PALETTE = THEMES[THEME_NAME]


def rgba(hex_or_rgba: str, alpha: int) -> str:
    """Return an rgba() CSS string for a hex token at the given alpha (0-255)."""
    color = QColor(hex_or_rgba)
    if not color.isValid():
        return hex_or_rgba
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


# --------------------------------------------------------------------------- #
# Surfaces
# --------------------------------------------------------------------------- #
BG_0 = _PALETTE["BG_0"]     # deepest backdrop (outer frame)
BG_1 = _PALETTE["BG_1"]     # window base
BG_2 = _PALETTE["BG_2"]     # elevated surface (cards, docks)
BG_3 = _PALETTE["BG_3"]     # hover / raised elements
BG_4 = _PALETTE["BG_4"]     # active / pressed elements

SURFACE_GLASS = "rgba(255, 255, 255, 0.04)"     # glass wash over dark
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
WINDOW_W = 1200
WINDOW_H = 780
WINDOW_MIN_W = 960
WINDOW_MIN_H = 620

SHELL_MARGIN = 18          # outer glow margin around the window body
SHELL_RADIUS = R_LG

TITLEBAR_H = 52
SIDEBAR_W = 252
SIDEBAR_W_COLLAPSED = 68
SIDEBAR_RADIUS = R_LG

INPUT_H = 58
INPUT_MAX_H = 150

STATUS_LABELS = {
    "idle": "Ready",
    "listening": "Listening…",
    "thinking": "Thinking…",
    "speaking": "Speaking…",
}
