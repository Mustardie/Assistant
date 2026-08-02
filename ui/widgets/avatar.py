"""
NovaAvatar — the glowing Nova orb.

A soft blue->purple gradient sphere with a gentle outer glow, a top
highlight, and the four-point Nova sparkle. In listening/thinking/speaking
states a softly pulsing status ring appears around it (driven by a timer
that animates `phase` 0..1, so it can be extended with any property anim).

Used in the sidebar header, the chat header and the empty-state welcome.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QTimer, Property
from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient, QLinearGradient
from PySide6.QtWidgets import QWidget

from .. import icons
from ..theme import (
    ACCENT, ACCENT_2, ACCENT_GLOW, ACCENT_GLOW_2,
    STATUS_LISTENING, STATUS_THINKING, STATUS_SPEAKING, STATUS_IDLE,
)

_STATE_COLORS = {
    "idle": STATUS_IDLE,
    "listening": STATUS_LISTENING,
    "thinking": STATUS_THINKING,
    "speaking": STATUS_SPEAKING,
}


class NovaAvatar(QWidget):
    """Paints the Nova orb. `state` in idle|listening|thinking|speaking."""

    def __init__(self, size: int = 44, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self._state = "idle"
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

    # ------------------------------------------------------------------ #
    def set_state(self, state: str):
        if state not in _STATE_COLORS:
            state = "idle"
        if state != self._state:
            self._state = state
            self.update()

    def state(self) -> str:
        return self._state

    # ------------------------------------------------------------------ #
    def _tick(self):
        self._phase = (self._phase + 0.04) % 1.0
        if self._state != "idle":
            self.update()

    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        s = self._size
        center = QRectF(0, 0, s, s)
        d = s * 0.92
        orb = QRectF((s - d) / 2, (s - d) / 2, d, d)

        # outer glow
        glow = QRadialGradient(center.center(), s / 2)
        glow_color = QColor(_STATE_COLORS[self._state])
        glow_color.setAlpha(90 if self._state != "idle" else 45)
        glow.setColorAt(0.0, glow_color)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center)

        # orb body — blue -> purple gradient
        body = QLinearGradient(orb.topLeft(), orb.bottomRight())
        body.setColorAt(0.0, QColor(ACCENT))
        body.setColorAt(0.55, QColor("#7B6CFF"))
        body.setColorAt(1.0, QColor(ACCENT_2))
        painter.setBrush(body)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(orb)

        # top highlight
        hl_cx = orb.center().x() + orb.width() * 0.22
        hl_cy = orb.center().y() - orb.width() * 0.22
        highlight = QRadialGradient(hl_cx, hl_cy, orb.width() * 0.60)
        highlight.setColorAt(0.0, QColor(255, 255, 255, 130))
        highlight.setColorAt(0.55, QColor(255, 255, 255, 0))
        painter.setBrush(highlight)
        painter.drawEllipse(orb)

        # sparkle
        icon_px = icons.pixmap("sparkle-solid", "#FFFFFF", int(s * 0.40))
        painter.drawPixmap(
            int((s - icon_px.width()) / 2), int((s - icon_px.height()) / 2), icon_px
        )

        # status ring (pulsing)
        if self._state != "idle":
            pulse = (self._phase * 2.0 - 1.0) ** 2          # 0 -> 1 -> 0
            ring_alpha = int(30 + 140 * (1.0 - pulse))
            ring_r = d / 2 + 3 + pulse * (s * 0.10)
            ring_color = QColor(_STATE_COLORS[self._state])
            ring_color.setAlpha(ring_alpha)
            pen = QPen(ring_color, 2.0)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            span = 300 - int(pulse * 200)
            painter.drawArc(
                QRectF(orb.center().x() - ring_r, orb.center().y() - ring_r,
                       ring_r * 2, ring_r * 2),
                int(-90 * 16), int(span * 16),
            )
