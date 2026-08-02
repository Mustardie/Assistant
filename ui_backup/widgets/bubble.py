"""The floating bubble — the assistant's collapsed resting state.

Click behavior (per spec): clicking the bubble opens the panel in TEXT
mode. The global Ctrl+Space hotkey instead opens the panel already in
LISTENING mode. See ui/main_window.py for the state wiring; this widget
only owns its own paint/hover/press visuals and emits `clicked`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, Signal, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPainter, QColor, QPainterPath, QPen
from PySide6.QtWidgets import QWidget, QGraphicsDropShadowEffect

import qtawesome as qta

from ui.styles.theme import PALETTE as P, METRICS as M, MOTION
from ui.animations.pulse import PulseAnimation


class FloatingBubble(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._diameter = M.bubble_diameter
        self.setFixedSize(self._diameter, self._diameter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        self._hovered = False
        self._pressed = False
        self._glow_strength = 0.5  # animated 0..1, drives shadow blur/opacity
        self._state = "idle"  # idle | listening | thinking | speaking

        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(28)
        self._shadow.setOffset(0, 4)
        self._shadow.setColor(QColor(0, 0, 0, 160))
        self.setGraphicsEffect(self._shadow)

        self._scale = 1.0
        self._scale_anim = QPropertyAnimation(self, b"scale", self)
        self._scale_anim.setDuration(MOTION.hover_ms)
        self._scale_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._pulse_anim: PulseAnimation | None = None

    # -- animated Qt properties ---------------------------------------- #

    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, value: float) -> None:
        self._scale = value
        self.update()

    scale = Property(float, _get_scale, _set_scale)

    def _get_glow(self) -> float:
        return self._glow_strength

    def _set_glow(self, value: float) -> None:
        self._glow_strength = value
        self._shadow.setColor(self._glow_color(value))
        self._shadow.setBlurRadius(24 + value * 26)
        self.update()

    glow = Property(float, _get_glow, _set_glow)

    def _glow_color(self, strength: float) -> QColor:
        base = QColor(self._state_color())
        base.setAlpha(int(90 + strength * 140))
        return base

    def _state_color(self) -> str:
        return {
            "idle": P.idle_dot,
            "listening": P.listening,
            "thinking": P.thinking,
            "speaking": P.speaking,
        }.get(self._state, P.idle_dot)

    # -- public API ------------------------------------------------------ #

    def set_state(self, state: str) -> None:
        """state in {'idle', 'listening', 'thinking', 'speaking'}"""
        self._state = state
        if self._pulse_anim:
            self._pulse_anim.stop()
            self._pulse_anim = None

        if state == "idle":
            self._set_glow(0.35)
        else:
            self._pulse_anim = PulseAnimation(self, b"glow", low=0.45, high=1.0)
            self._pulse_anim.start()
        self.update()

    # -- interaction ------------------------------------------------------ #

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._animate_scale(1.06)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._animate_scale(1.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self._animate_scale(0.94)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self._animate_scale(1.06 if self._hovered else 1.0)
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
        super().mouseReleaseEvent(event)

    def _animate_scale(self, target: float) -> None:
        self._scale_anim.stop()
        self._scale_anim.setStartValue(self._scale)
        self._scale_anim.setEndValue(target)
        self._scale_anim.start()

    # -- painting ------------------------------------------------------ #

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        d = self._diameter * self._scale
        offset = (self._diameter - d) / 2
        rect = self.rect().adjusted(0, 0, 0, 0)
        cx, cy = rect.center().x(), rect.center().y()

        path = QPainterPath()
        path.addEllipse(cx - d / 2, cy - d / 2, d, d)

        bg = QColor(P.bubble_bg_hover if self._hovered else P.bubble_bg)
        painter.fillPath(path, bg)

        ring = QColor(self._state_color())
        ring.setAlpha(int(120 + self._glow_strength * 135))
        pen = QPen(ring, 2.4)
        painter.setPen(pen)
        painter.drawPath(path)

        icon_name = "fa6s.microphone" if self._state != "thinking" else "fa6s.wand-magic-sparkles"
        icon = qta.icon(icon_name, color=QColor(P.text_primary))
        icon_size = int(d * 0.36)
        pixmap = icon.pixmap(QSize(icon_size, icon_size))
        painter.drawPixmap(
            int(cx - icon_size / 2),
            int(cy - icon_size / 2),
            pixmap,
        )
        painter.end()
