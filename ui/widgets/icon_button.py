"""Reusable circular icon button.

Every icon control in the app (history, settings, close, mic-toggle, send)
is an instance of this one widget so hover/press behavior, sizing, and
theming stay perfectly consistent — this is the 'reusable widget'
building block requirement.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, Property, QPropertyAnimation, QEasingCurve, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QToolButton, QGraphicsColorizeEffect

import qtawesome as qta

from ui.styles.theme import PALETTE as P, METRICS as M, MOTION


class IconButton(QToolButton):
    """A borderless, circular, animated icon button.

    Parameters
    ----------
    icon_name:
        A qtawesome glyph name, e.g. 'fa6s.gear'.
    diameter:
        Button diameter in px. Defaults to the standard control size.
    tooltip:
        Optional tooltip text.
    """

    def __init__(
        self,
        icon_name: str,
        diameter: int = M.icon_button_size,
        tooltip: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._icon_name = icon_name
        self._diameter = diameter
        self._icon_color = QColor(P.control_icon)
        self._icon_color_hover = QColor(P.control_icon_hover)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(QSize(diameter, diameter))
        self.setToolTip(tooltip)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._apply_icon(self._icon_color)

        self.setStyleSheet(
            f"""
            QToolButton {{
                background-color: transparent;
                border: none;
                border-radius: {diameter // 2}px;
            }}
            QToolButton:hover {{
                background-color: {P.control_bg_hover};
            }}
            QToolButton:pressed {{
                background-color: {P.control_bg_pressed};
            }}
            """
        )

    def _apply_icon(self, color: QColor) -> None:
        icon = qta.icon(self._icon_name, color=color)
        self.setIcon(icon)
        self.setIconSize(QSize(int(self._diameter * 0.5), int(self._diameter * 0.5)))

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._apply_icon(self._icon_color_hover)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._apply_icon(self._icon_color)
        super().leaveEvent(event)

    def set_active(self, active: bool) -> None:
        """Toggle a persistent 'active' tint, e.g. mic button while listening."""
        self._apply_icon(QColor(P.accent) if active else self._icon_color)


class SendButton(QToolButton):
    """Accent-filled circular send button used at the end of the text input."""

    def __init__(self, diameter: int = M.icon_button_size, parent=None):
        super().__init__(parent)
        self.setObjectName("sendButton")
        self._diameter = diameter
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(QSize(diameter, diameter))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._refresh_icon(enabled=False)
        self.setEnabled(False)

        from ui.styles.qss import send_button_qss

        self.setStyleSheet(send_button_qss())

    def _refresh_icon(self, enabled: bool) -> None:
        color = QColor("#FFFFFF") if enabled else QColor(P.text_disabled)
        icon = qta.icon("fa6s.arrow-up", color=color)
        self.setIcon(icon)
        self.setIconSize(QSize(int(self._diameter * 0.42), int(self._diameter * 0.42)))

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 (Qt override)
        super().setEnabled(enabled)
        self._refresh_icon(enabled)
