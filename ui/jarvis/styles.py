"""Shared visual tokens for the JARVIS shell."""

BG = "#050B12"
BG_SOFT = "#081522"
PANEL = "rgba(8, 24, 37, 228)"
PANEL_STRONG = "rgba(9, 29, 44, 244)"
BORDER = "rgba(104, 225, 238, 72)"
BORDER_FOCUS = "rgba(104, 225, 238, 160)"
CYAN = "#67E4EE"
CYAN_BRIGHT = "#B7FAFF"
BLUE = "#4A91F2"
TEXT = "#E9FBFF"
TEXT_SOFT = "#9DC0CA"
TEXT_FAINT = "#587680"
WARNING = "#FFC76A"
ERROR = "#FF6E7A"
SUCCESS = "#65E6A5"
FONT = "Segoe UI"


def button_style(accent: bool = False, danger: bool = False) -> str:
    if danger:
        background, border, color = "rgba(255, 85, 102, 38)", "rgba(255, 110, 122, 135)", ERROR
    elif accent:
        background, border, color = "rgba(103, 228, 238, 35)", "rgba(103, 228, 238, 130)", CYAN_BRIGHT
    else:
        background, border, color = "rgba(255, 255, 255, 12)", "rgba(255, 255, 255, 30)", TEXT_SOFT
    return f"""
        QPushButton {{
            background: {background}; border: 1px solid {border}; color: {color};
            border-radius: 9px; padding: 7px 12px; font-family: {FONT};
            font-size: 12px; font-weight: 600;
        }}
        QPushButton:hover {{ background: rgba(103, 228, 238, 50); color: {TEXT}; }}
        QPushButton:pressed {{ background: rgba(103, 228, 238, 70); }}
        QPushButton:disabled {{ color: {TEXT_FAINT}; border-color: rgba(255,255,255,18); }}
    """


SCROLL_STYLE = """
    QScrollArea { background: transparent; border: none; }
    QScrollArea > QWidget > QWidget { background: transparent; }
    QScrollBar:vertical { background: transparent; width: 7px; margin: 1px; }
    QScrollBar::handle:vertical { background: rgba(103,228,238,45); border-radius: 3px; min-height: 24px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""

