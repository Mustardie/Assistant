"""Central QSS generation. Every widget-level stylesheet string lives here
so there is exactly one place to touch for a re-skin."""
from __future__ import annotations

from ui.styles.theme import PALETTE as P, METRICS as M


def panel_qss() -> str:
    return f"""
    QFrame#panelSurface {{
        background-color: {P.panel_bg};
        border: 1px solid {P.panel_border};
        border-radius: {M.panel_radius}px;
    }}
    """


def header_label_qss() -> str:
    return f"""
    QLabel#statusLabel {{
        color: {P.text_secondary};
        background: transparent;
    }}
    """


def icon_button_qss(diameter: int | None = None) -> str:
    d = diameter or M.icon_button_size
    r = M.icon_button_radius
    return f"""
    QToolButton {{
        background-color: transparent;
        border: none;
        border-radius: {r}px;
        min-width: {d}px;
        min-height: {d}px;
        max-width: {d}px;
        max-height: {d}px;
    }}
    QToolButton:hover {{
        background-color: {P.control_bg_hover};
    }}
    QToolButton:pressed {{
        background-color: {P.control_bg_pressed};
    }}
    """


def text_input_qss() -> str:
    return f"""
    QLineEdit#promptInput {{
        background-color: {P.input_bg};
        border: 1.5px solid {P.input_border};
        border-radius: {M.input_radius}px;
        padding: 0 {M.padding_md}px;
        color: {P.text_primary};
        selection-background-color: {P.accent};
    }}
    QLineEdit#promptInput:focus {{
        border: 1.5px solid {P.input_border_focus};
    }}
    """


def send_button_qss() -> str:
    return f"""
    QToolButton#sendButton {{
        background-color: {P.accent};
        border: none;
        border-radius: {M.input_radius}px;
    }}
    QToolButton#sendButton:hover {{
        background-color: {P.accent_soft};
    }}
    QToolButton#sendButton:disabled {{
        background-color: {P.control_bg};
    }}
    """


def scrollarea_qss() -> str:
    return f"""
    QScrollBar:vertical {{
        background: transparent;
        width: 6px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {P.scrollbar};
        border-radius: 3px;
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    """


def dialog_qss() -> str:
    return f"""
    QDialog {{
        background-color: {P.panel_bg};
        border: 1px solid {P.panel_border};
        border-radius: 16px;
    }}
    QLabel {{
        color: {P.text_primary};
    }}
    """
