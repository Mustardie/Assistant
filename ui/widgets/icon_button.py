"""
IconButton — the single reusable icon control used everywhere in Nova
(sidebar actions, window controls, input actions, settings rows).

Hover/press states are painted by hand so the feel stays consistent:
a soft elevated chip on hover, a subtle press, and an optional active
(accent) state used e.g. by the microphone while listening.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton

from .. import icons
from ..theme import BG_3, BG_4, TEXT_SOFT, TEXT, ACCENT, R_XS


class IconButton(QAbstractButton):
    """A circular/chip icon button with hand-painted states."""

    def __init__(self, icon_name: str, size: int = 32, radius: int | None = None,
                 color: str | None = None, hover_color: str | None = None,
                 tooltip: str = "", parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._size = size
        self._radius = radius if radius is not None else size // 2
        self._color = QColor(color or TEXT_SOFT)
        self._hover_color = QColor(hover_color or TEXT)
        self._hovered = False
        self._pressed = False
        self._active = False
        self._icon_scale = 0.52

        self.setFixedSize(QSize(size, size))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if tooltip:
            self.setToolTip(tooltip)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def set_icon_name(self, name: str):
        self._icon_name = name
        self.update()

    def set_active(self, active: bool):
        self._active = active
        self.update()

    def is_active(self) -> bool:
        return self._active

    def set_icon_scale(self, scale: float):
        self._icon_scale = scale
        self.update()

    # ------------------------------------------------------------------ #
    # painting
    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)

        if self._active:
            painter.setBrush(QColor(ACCENT).lighter(110))
            painter.setPen(QPen(QColor(ACCENT), 1.2))
            painter.drawRoundedRect(rect, self._radius, self._radius)
            color = "#FFFFFF"
        elif self._pressed and self._hovered:
            painter.setBrush(QColor(BG_4))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, self._radius, self._radius)
            color = self._hover_color
        elif self._hovered:
            painter.setBrush(QColor(BG_3))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, self._radius, self._radius)
            color = self._hover_color
        else:
            color = self._color

        icon_px = icons.pixmap(
            self._icon_name, color.name() if isinstance(color, QColor) else color,
            int(self._size * self._icon_scale),
        )
        x = (self.width() - icon_px.width()) // 2
        y = (self.height() - icon_px.height()) // 2
        painter.drawPixmap(x, y, icon_px)

    # ------------------------------------------------------------------ #
    # state tracking
    # ------------------------------------------------------------------ #
    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)
