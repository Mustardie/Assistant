"""
Design tokens for the assistant UI.

IMPORTANT CONTEXT FOR MAINTAINERS:
No Figma design spec was available for this build (the supplied .fig file's
node tree did not contain an assistant UI — see project README). Every value
below is therefore a deliberate, professional default modeled on modern
floating-assistant patterns (macOS Spotlight/Siri, Windows Copilot), NOT an
extracted design token. If a real Figma spec becomes available later, this
is the only file that should need to change for a full re-skin — every
widget in ui/widgets pulls its colors, radii, spacing and typography from
here rather than hardcoding values.
"""
from __future__ import annotations

from dataclasses import dataclass
from PySide6.QtGui import QColor, QFont


@dataclass(frozen=True)
class Palette:
    # Core surfaces
    bubble_bg: str = "#1C1C1F"
    bubble_bg_hover: str = "#242428"
    panel_bg: str = "#17171A"
    panel_border: str = "#2A2A2F"

    # Accent (listening / active state)
    accent: str = "#5B8CFF"
    accent_soft: str = "#3D5FCC"
    accent_glow: str = "#5B8CFF"

    # Status colors
    listening: str = "#5B8CFF"
    thinking: str = "#B979E8"
    speaking: str = "#33C481"
    idle_dot: str = "#6B6B72"
    error: str = "#E8555B"

    # Text
    text_primary: str = "#F2F2F5"
    text_secondary: str = "#9A9AA2"
    text_disabled: str = "#5B5B62"

    # Controls
    control_bg: str = "#232327"
    control_bg_hover: str = "#2C2C31"
    control_bg_pressed: str = "#18181B"
    control_icon: str = "#C7C7CE"
    control_icon_hover: str = "#F2F2F5"

    input_bg: str = "#1F1F23"
    input_border: str = "#33333A"
    input_border_focus: str = "#5B8CFF"

    scrollbar: str = "#38383F"


@dataclass(frozen=True)
class Metrics:
    bubble_diameter: int = 64
    bubble_diameter_hover: int = 68

    panel_width: int = 420
    panel_min_height: int = 96
    panel_max_height: int = 480
    panel_radius: int = 22
    bubble_radius: int = 32

    header_height: int = 44
    icon_button_size: int = 32
    icon_button_radius: int = 10

    padding_sm: int = 8
    padding_md: int = 14
    padding_lg: int = 20

    waveform_bar_count: int = 5
    waveform_bar_width: int = 4
    waveform_bar_gap: int = 5
    waveform_height: int = 28

    input_height: int = 44
    input_radius: int = 14


@dataclass(frozen=True)
class Motion:
    """Durations in ms, matched to Qt's QEasingCurve names."""
    expand_ms: int = 260
    collapse_ms: int = 200
    fade_ms: int = 160
    pulse_ms: int = 1100
    hover_ms: int = 120
    press_ms: int = 90
    bar_ms: int = 420


class Fonts:
    @staticmethod
    def base(size: int = 13, weight: int = QFont.Weight.Normal) -> QFont:
        font = QFont("Segoe UI", size)
        font.setWeight(weight)
        return font

    @staticmethod
    def status(size: int = 12) -> QFont:
        return Fonts.base(size, QFont.Weight.DemiBold)

    @staticmethod
    def body(size: int = 14) -> QFont:
        return Fonts.base(size, QFont.Weight.Normal)


PALETTE = Palette()
METRICS = Metrics()
MOTION = Motion()


def qcolor(hex_value: str, alpha: int = 255) -> QColor:
    color = QColor(hex_value)
    color.setAlpha(alpha)
    return color
