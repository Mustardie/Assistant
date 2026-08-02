"""Reusable transition builders. Every animation in the app is constructed
here so widgets don't hand-roll QPropertyAnimation boilerplate, and so
durations/easing stay consistent with ui.styles.theme.MOTION.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import (
    QObject,
    QPropertyAnimation,
    QEasingCurve,
    QRect,
    QParallelAnimationGroup,
    QSequentialAnimationGroup,
)

from ui.styles.theme import MOTION


def animate_geometry(
    target: QObject,
    start: QRect,
    end: QRect,
    duration: int = MOTION.expand_ms,
    easing: QEasingCurve.Type = QEasingCurve.Type.OutCubic,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation:
    anim = QPropertyAnimation(target, b"geometry", target)
    anim.setDuration(duration)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(easing)
    if on_finished:
        anim.finished.connect(on_finished)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def animate_opacity(
    effect: QObject,
    start: float,
    end: float,
    duration: int = MOTION.fade_ms,
    easing: QEasingCurve.Type = QEasingCurve.Type.OutQuad,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation:
    """Animates a QGraphicsOpacityEffect's `opacity` property.

    NOTE: a widget can only hold ONE QGraphicsEffect at a time — don't use
    this on a widget that already owns a QGraphicsDropShadowEffect (e.g.
    FloatingBubble, AssistantPanel); use animate_window_opacity instead,
    which animates QWidget.windowOpacity on the top-level window and
    leaves each child widget's own shadow effect untouched.
    """
    anim = QPropertyAnimation(effect, b"opacity", effect)
    anim.setDuration(duration)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(easing)
    if on_finished:
        anim.finished.connect(on_finished)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def animate_window_opacity(
    window: QObject,
    start: float,
    end: float,
    duration: int = MOTION.fade_ms,
    easing: QEasingCurve.Type = QEasingCurve.Type.OutQuad,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation:
    """Animates a top-level QWidget's own `windowOpacity` property. Safe to
    combine with per-child QGraphicsDropShadowEffect instances since it
    doesn't touch each widget's graphicsEffect."""
    anim = QPropertyAnimation(window, b"windowOpacity", window)
    anim.setDuration(duration)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(easing)
    if on_finished:
        anim.finished.connect(on_finished)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def crossfade_and_resize(
    target: QObject,
    effect: QObject,
    geo_start: QRect,
    geo_end: QRect,
    duration: int = MOTION.expand_ms,
) -> QParallelAnimationGroup:
    """Combined fade + resize used for bubble <-> panel morph."""
    group = QParallelAnimationGroup(target)

    geo_anim = QPropertyAnimation(target, b"geometry")
    geo_anim.setDuration(duration)
    geo_anim.setStartValue(geo_start)
    geo_anim.setEndValue(geo_end)
    geo_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    fade_anim = QPropertyAnimation(effect, b"opacity")
    fade_anim.setDuration(max(120, duration // 2))
    fade_anim.setStartValue(0.0)
    fade_anim.setEndValue(1.0)
    fade_anim.setEasingCurve(QEasingCurve.Type.OutQuad)

    group.addAnimation(geo_anim)
    group.addAnimation(fade_anim)
    group.start(QParallelAnimationGroup.DeletionPolicy.DeleteWhenStopped)
    return group
