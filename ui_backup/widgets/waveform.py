"""Animated bar waveform.

Purely decorative until wired to real audio amplitude data — exposes
`set_levels()` so a backend can later feed it live microphone amplitude
per bar instead of the built-in idle animation. Until then it animates a
pleasant procedural pattern so the listening state doesn't look static.
"""
from __future__ import annotations

import random

from PySide6.QtCore import Qt, QTimer, QRectF, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPainter, QColor, QPainterPath
from PySide6.QtWidgets import QWidget

from ui.styles.theme import PALETTE as P, METRICS as M, MOTION


class WaveformWidget(QWidget):
    def __init__(self, bar_count: int = M.waveform_bar_count, parent=None):
        super().__init__(parent)
        self._bar_count = bar_count
        self._levels = [0.35] * bar_count
        self._targets = [0.35] * bar_count
        self._color = QColor(P.accent)
        self._active = False

        self.setFixedHeight(M.waveform_height)
        self.setMinimumWidth(
            bar_count * M.waveform_bar_width + (bar_count - 1) * M.waveform_bar_gap
        )

        self._timer = QTimer(self)
        self._timer.setInterval(MOTION.bar_ms // 3)
        self._timer.timeout.connect(self._tick)

    # -- public API -------------------------------------------------- #

    def start(self) -> None:
        self._active = True
        self._timer.start()

    def stop(self) -> None:
        self._active = False
        self._timer.stop()
        self._levels = [0.2] * self._bar_count
        self.update()

    def set_levels(self, levels: list[float]) -> None:
        """Feed real amplitude data (0.0-1.0 per bar) from a backend."""
        n = self._bar_count
        if len(levels) != n:
            levels = (levels + [0.0] * n)[:n]
        self._targets = [max(0.08, min(1.0, v)) for v in levels]

    def set_color(self, hex_color: str) -> None:
        self._color = QColor(hex_color)
        self.update()

    # -- internals ----------------------------------------------------- #

    def _tick(self) -> None:
        if not any(self._targets):
            self._targets = [random.uniform(0.15, 1.0) for _ in range(self._bar_count)]
        for i in range(self._bar_count):
            self._levels[i] += (self._targets[i] - self._levels[i]) * 0.5
        if random.random() < 0.5:
            self._targets = [random.uniform(0.15, 1.0) for _ in range(self._bar_count)]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)

        bar_w = M.waveform_bar_width
        gap = M.waveform_bar_gap
        total_w = self._bar_count * bar_w + (self._bar_count - 1) * gap
        x = (self.width() - total_w) / 2
        mid_y = self.height() / 2

        for level in self._levels:
            h = max(bar_w, level * self.height())
            rect = QRectF(x, mid_y - h / 2, bar_w, h)
            path = QPainterPath()
            path.addRoundedRect(rect, bar_w / 2, bar_w / 2)
            painter.drawPath(path)
            x += bar_w + gap

        painter.end()
