"""
NovaOrb — the animated Nova orb.

A hand-painted glowing sphere inspired by the WebGL shader orb in the
redesign: layered radial gradients, a moving specular highlight, orbiting
arcs, and a soft pulsing halo. The pulse rate and ring color follow the
voice/thinking state:

    idle      — slow breathing, accent glow
    listening — faster cyan/periwinkle pulse with an expanding ring
    thinking  — lilac shimmer
    speaking  — green pulse
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QRectF, QTimer, Property, QEasingCurve, QPropertyAnimation
from PySide6.QtGui import QColor, QPainter, QRadialGradient, QPen, QLinearGradient, QPainterPath
from PySide6.QtWidgets import QWidget

from .. import theme

_STATE_COLORS = {
    "idle": ("ACCENT", "ACCENT_2"),
    "listening": ("STATUS_LISTENING", "ACCENT_2"),
    "thinking": ("STATUS_THINKING", "ACCENT_2"),
    "speaking": ("STATUS_SPEAKING", "ACCENT_2"),
}


class NovaOrb(QWidget):
    def __init__(self, size: int = 64, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self._state = "idle"
        self._phase = 0.0
        self._pulse = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._state_progress = 0.0  # 0..1 animated on state change
        self._state_anim = None

    # ------------------------------------------------------------------ #
    def sizeHint(self):
        return self.size()

    def _tick(self):
        self._phase = (self._phase + 0.045) % (2 * math.pi)
        self._pulse = (self._pulse + 0.018) % 1.0
        if self._state_progress < 1.0:
            self._state_progress = min(1.0, self._state_progress + 0.08)
        self.update()

    def set_state(self, state: str):
        state = state if state in _STATE_COLORS else "idle"
        if state == self._state:
            return
        self._state = state
        self._state_progress = 0.0
        self.update()

    @property
    def state(self) -> str:
        return self._state

    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height())
        cx, cy = side / 2.0, side / 2.0
        radius = side * 0.5

        state = self._state
        color_key_a, color_key_b = _STATE_COLORS[state]
        col_a = QColor(getattr(theme, color_key_a))
        col_b = QColor(getattr(theme, color_key_b))

        # soft mixing when the state just changed
        if self._state_progress < 1.0:
            idle_a = QColor(theme.ACCENT)
            r = int(idle_a.red() + (col_a.red() - idle_a.red()) * self._state_progress)
            g = int(idle_a.green() + (col_a.green() - idle_a.green()) * self._state_progress)
            b = int(idle_a.blue() + (col_a.blue() - idle_a.blue()) * self._state_progress)
            col_a = QColor(r, g, b)

        # ----- halo (soft glow behind the sphere) -----
        pulse_speed = 1.0 if state == "idle" else 1.6
        breath = 0.5 + 0.5 * math.sin(self._phase * 2.0)
        halo_r = radius * (1.45 + 0.10 * breath * pulse_speed)
        halo = QRadialGradient(cx, cy, halo_r)
        glow = QColor(col_a)
        glow.setAlpha(int(42 + 26 * breath))
        halo.setColorAt(0.0, glow)
        halo.setColorAt(0.55, QColor(glow.red(), glow.green(), glow.blue(), 14))
        halo.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QRectF(cx - halo_r, cy - halo_r, halo_r * 2, halo_r * 2))

        # ----- expanding listening ring -----
        if state == "listening":
            ring_p = (self._pulse * 2.0) % 1.0
            ring_r = radius * (1.05 + ring_p * 0.65)
            ring = QColor(theme.STATUS_LISTENING)
            ring.setAlpha(int(120 * (1.0 - ring_p)))
            pen = QPen(ring, 1.6)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2))

        # ----- the sphere -----
        sphere_r = radius * (1.0 + 0.025 * breath)
        core = QRadialGradient(
            cx - sphere_r * 0.35, cy - sphere_r * 0.4, sphere_r * 1.6
        )
        core.setColorAt(0.0, QColor(col_a).lighter(112))
        core.setColorAt(0.45, col_a)
        core.setColorAt(1.0, QColor(col_a).darker(135))
        p.setBrush(core)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(cx - sphere_r, cy - sphere_r, sphere_r * 2, sphere_r * 2))

        # secondary tint arc (lilac/peach wash from the bottom-right)
        tint = QRadialGradient(
            cx + sphere_r * 0.5, cy + sphere_r * 0.55, sphere_r * 1.1
        )
        t = QColor(col_b)
        t.setAlpha(120)
        tint.setColorAt(0.0, t)
        tint.setColorAt(0.6, QColor(t.red(), t.green(), t.blue(), 30))
        tint.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(tint)
        p.drawEllipse(QRectF(cx - sphere_r, cy - sphere_r, sphere_r * 2, sphere_r * 2))

        # specular highlight (moves slowly with the phase)
        hx = cx - sphere_r * 0.38 + math.sin(self._phase * 0.6) * sphere_r * 0.08
        hy = cy - sphere_r * 0.42 + math.cos(self._phase * 0.5) * sphere_r * 0.05
        spec = QRadialGradient(hx, hy, sphere_r * 0.55)
        white = QColor(255, 255, 255)
        white.setAlpha(170)
        spec.setColorAt(0.0, white)
        spec.setColorAt(0.5, QColor(255, 255, 255, 40))
        spec.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(spec)
        p.drawEllipse(QRectF(cx - sphere_r, cy - sphere_r, sphere_r * 2, sphere_r * 2))

        # orbiting arc (two thin trails rotating at different speeds)
        p.save()
        p.translate(cx, cy)
        for i, (speed, rr, alpha) in enumerate(
            ((1.0, sphere_r * 1.28, 90), (-0.7, sphere_r * 1.42, 55))
        ):
            ang = self._phase * speed + i * math.pi * 0.6
            p.setPen(QPen(QColor(col_a.red(), col_a.green(), col_a.blue(), alpha), 1.4))
            p.setBrush(Qt.NoBrush)
            start = ang
            span = math.pi * (0.9 + 0.35 * math.sin(self._phase + i))
            path = QPainterPath()
            path.moveTo(rr * math.cos(start), rr * math.sin(start))
            steps = 24
            for s in range(1, steps + 1):
                a = start + span * s / steps
                path.lineTo(rr * math.cos(a), rr * math.sin(a))
            p.drawPath(path)
        p.restore()
