"""Low-cost, state-driven animated JARVIS intelligence core."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QConicalGradient, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from ui.jarvis.models import JarvisState
from ui.jarvis.styles import CYAN, CYAN_BRIGHT, ERROR, FONT, SUCCESS, WARNING


_STATE_STYLE = {
    JarvisState.IDLE: (QColor(CYAN), 0.45, 0.45),
    JarvisState.LISTENING: (QColor("#82F7FF"), 1.25, 0.90),
    JarvisState.THINKING: (QColor("#71A9FF"), 1.75, 0.70),
    JarvisState.SPEAKING: (QColor(SUCCESS), 1.15, 1.00),
    JarvisState.EXECUTING_TOOL: (QColor("#B58AFF"), 2.10, 0.80),
    JarvisState.WAITING_FOR_CONFIRMATION: (QColor(WARNING), 0.65, 0.72),
    JarvisState.ERROR: (QColor(ERROR), 0.38, 1.00),
}

_STATE_LABEL = {
    JarvisState.IDLE: "STANDBY",
    JarvisState.LISTENING: "LISTENING",
    JarvisState.THINKING: "SYNTHESIZING",
    JarvisState.SPEAKING: "RESPONDING",
    JarvisState.EXECUTING_TOOL: "EXECUTING",
    JarvisState.WAITING_FOR_CONFIRMATION: "AUTHORIZATION",
    JarvisState.ERROR: "ATTENTION",
}


class JarvisCore(QWidget):
    activated = Signal()
    stateChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = JarvisState.IDLE
        self._phase = 0.0
        self._audio_level = 0.0
        self._activation_burst = 0.0
        self._animation_enabled = True
        self._detail = "Ready when you are"
        self.setMinimumSize(360, 360)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    @property
    def state(self) -> JarvisState:
        return self._state

    def set_state(self, state: str | JarvisState, detail: str = "") -> None:
        try:
            normalized = JarvisState.normalize(state)
        except ValueError:
            normalized = JarvisState.ERROR
        changed = normalized != self._state
        self._state = normalized
        if detail:
            self._detail = detail
        elif normalized == JarvisState.IDLE:
            self._detail = "Ready when you are"
        if changed:
            self.stateChanged.emit(normalized.value)
        self.update()

    def set_audio_level(self, value: float) -> None:
        self._audio_level = max(0.0, min(float(value), 1.0))

    def set_animation_enabled(self, enabled: bool) -> None:
        self._animation_enabled = bool(enabled)
        if self._animation_enabled and not self._timer.isActive():
            self._timer.start()
        elif not self._animation_enabled:
            self._timer.stop()
        self.update()

    def trigger_listening(self) -> None:
        """Immediate acknowledgement for a core click, before STT loads."""
        self._activation_burst = 1.0
        self.set_state(JarvisState.LISTENING, "Listening · speak now")

    def _tick(self) -> None:
        _, speed, _ = _STATE_STYLE[self._state]
        self._phase = (self._phase + speed * 0.0075) % 1.0
        self._audio_level *= 0.88
        self._activation_burst *= 0.91
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = max(70.0, min(self.width(), self.height()) * 0.39)
        color, _, intensity = _STATE_STYLE[self._state]
        pulse = 0.5 + 0.5 * math.sin(self._phase * math.tau * 1.8)
        active = intensity * (0.70 + pulse * 0.30 + self._audio_level * 0.25 + self._activation_burst * 0.35)

        glow = QRadialGradient(center, radius * 1.45)
        glow_color = QColor(color)
        glow_color.setAlpha(int(42 * active))
        glow.setColorAt(0.0, glow_color)
        transparent = QColor(color)
        transparent.setAlpha(0)
        glow.setColorAt(0.52, QColor(color.red(), color.green(), color.blue(), int(14 * active)))
        glow.setColorAt(1.0, transparent)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, radius * 1.45, radius * 1.45)

        if self._activation_burst > 0.03:
            burst_color = QColor(color)
            burst_color.setAlpha(int(125 * self._activation_burst))
            burst_radius = radius * (1.05 + (1.0 - self._activation_burst) * 0.32)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(burst_color, 2.2 * self._activation_burst, Qt.PenStyle.SolidLine))
            painter.drawEllipse(center, burst_radius, burst_radius)

        self._draw_ring(painter, center, radius * 1.03, color, 1.1, self._phase * 360, segments=5, alpha=int(95 * active))
        self._draw_ring(painter, center, radius * 0.86, color, 2.0, -self._phase * 520, segments=9, alpha=int(145 * active))
        self._draw_ticks(painter, center, radius * 0.73, color, self._phase * 120, active)
        self._draw_ring(painter, center, radius * 0.61, color, 3.0 + pulse * 1.2, self._phase * 290, segments=2, alpha=int(220 * active))
        self._draw_ring(painter, center, radius * 0.47, QColor(CYAN_BRIGHT), 1.0, -self._phase * 180, segments=1, alpha=int(90 * active))

        inner = QRadialGradient(center, radius * 0.48)
        inner.setColorAt(0.0, QColor(7, 24, 35, 230))
        inner.setColorAt(0.78, QColor(6, 17, 26, 245))
        inner.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), int(45 * active)))
        painter.setBrush(inner)
        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), int(100 * active)), 1.0))
        painter.drawEllipse(center, radius * 0.43, radius * 0.43)

        painter.setPen(QColor(CYAN_BRIGHT if self._state != JarvisState.ERROR else ERROR))
        title_font = QFont(FONT, max(13, int(radius * 0.09)), QFont.Weight.DemiBold)
        title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4.0)
        painter.setFont(title_font)
        title_rect = QRectF(center.x() - radius * 0.38, center.y() - 28, radius * 0.76, 30)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, "J.A.R.V.I.S")

        status_font = QFont(FONT, max(7, int(radius * 0.045)), QFont.Weight.Bold)
        status_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        painter.setFont(status_font)
        status = _STATE_LABEL[self._state]
        painter.setPen(QColor(color.red(), color.green(), color.blue(), 215))
        painter.drawText(QRectF(center.x() - radius * 0.35, center.y() + 7, radius * 0.70, 22), Qt.AlignmentFlag.AlignCenter, status)

        painter.setFont(QFont(FONT, max(7, int(radius * 0.039))))
        painter.setPen(QColor(141, 185, 197, 160))
        painter.drawText(QRectF(center.x() - radius * 0.52, center.y() + radius * 0.54, radius * 1.04, 24), Qt.AlignmentFlag.AlignCenter, self._detail[:58])

    @staticmethod
    def _draw_ring(painter, center, radius, color, width, rotation, *, segments, alpha):
        painter.save()
        painter.translate(center)
        painter.rotate(rotation)
        painter.translate(-center)
        rect = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
        pen_color = QColor(color)
        pen_color.setAlpha(max(0, min(alpha, 255)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(pen_color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        gap = 18 if segments > 1 else 3
        sweep = (360 - gap * segments) / segments
        for index in range(segments):
            start = index * (sweep + gap) + index * 4
            painter.drawArc(rect, int(start * 16), int(sweep * 16))
        painter.restore()

    @staticmethod
    def _draw_ticks(painter, center, radius, color, rotation, intensity):
        painter.save()
        painter.translate(center)
        painter.rotate(rotation)
        tick_color = QColor(color)
        tick_color.setAlpha(int(105 * intensity))
        painter.setPen(QPen(tick_color, 1.0))
        for index in range(48):
            angle = math.radians(index * 7.5)
            length = 9 if index % 6 == 0 else 4
            p1 = QPointF(math.cos(angle) * (radius - length), math.sin(angle) * (radius - length))
            p2 = QPointF(math.cos(angle) * radius, math.sin(angle) * radius)
            painter.drawLine(p1, p2)
        painter.restore()
