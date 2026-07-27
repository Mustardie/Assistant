"""Looping 'breathing' pulse animation, used for:
  - the idle bubble's subtle breathing glow
  - the listening-state accent ring
  - the waveform bars (amplitude pulse) while capturing audio

Built on QPropertyAnimation with loopCount(-1) rather than a QTimer so it
integrates with Qt's animation framework (pause/resume/stop are free).
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QPropertyAnimation, QEasingCurve

from ui.styles.theme import MOTION


class PulseAnimation(QPropertyAnimation):
    """A looping ping-pong animation over a single float property.

    Usage:
        self._glow = 0.0
        anim = PulseAnimation(self, b"glow", low=0.35, high=1.0)
        anim.start()
    """

    def __init__(
        self,
        target: QObject,
        prop_name: bytes,
        low: float = 0.4,
        high: float = 1.0,
        duration: int = MOTION.pulse_ms,
        parent: QObject | None = None,
    ):
        super().__init__(target, prop_name, parent or target)
        self.setDuration(duration)
        self.setStartValue(low)
        self.setEndValue(high)
        self.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.setLoopCount(-1)
        # Ping-pong: Qt animations don't natively reverse each loop, so we
        # use setDirection flip via finished-of-loop is not exposed; instead
        # we rely on InOutSine + KeepWhenStopped and toggle direction on
        # each `currentLoopChanged` for a true breathing effect.
        self.currentLoopChanged.connect(self._flip_direction)

    def _flip_direction(self, _loop: int) -> None:
        if self.direction() == QPropertyAnimation.Direction.Forward:
            self.setDirection(QPropertyAnimation.Direction.Backward)
        else:
            self.setDirection(QPropertyAnimation.Direction.Forward)
