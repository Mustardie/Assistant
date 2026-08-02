"""
Small, reusable animation helpers for Nova.

Everything is Qt-property based: geometry morphs, glass-state morphs,
opacity fades, and a spring-like easing curve tuned for a premium feel.
"""
from PySide6.QtCore import (
    QPropertyAnimation, QParallelAnimationGroup, QSequentialAnimationGroup,
    QEasingCurve, QRect,
)

DEFAULT_EASING = QEasingCurve(QEasingCurve.OutCubic)


def spring_easing(overshoot=1.05):
    curve = QEasingCurve(QEasingCurve.OutBack)
    curve.setOvershoot(overshoot)
    return curve


def animate_property(widget, prop: bytes, end_value, duration=220,
                     easing=None, start_value=None, finished_cb=None):
    """Animate any Qt property on a widget."""
    anim = QPropertyAnimation(widget, prop, widget)
    anim.setDuration(duration)
    if start_value is not None:
        anim.setStartValue(start_value)
    anim.setEndValue(end_value)
    anim.setEasingCurve(easing or DEFAULT_EASING)
    if finished_cb:
        anim.finished.connect(finished_cb)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def animate_geometry(widget, end_rect: QRect, duration=320, easing=None,
                     finished_cb=None):
    return animate_property(widget, b"geometry", end_rect, duration=duration,
                            easing=easing, finished_cb=finished_cb)


def fade_widget(widget, start, end, duration=180, finished_cb=None):
    """Fade a widget's opacity via its windowOpacity (top-level) or a
    QGraphicsOpacityEffect (children)."""
    if widget.isWindow():
        return animate_property(widget, b"windowOpacity", end,
                                duration=duration, start_value=start,
                                finished_cb=finished_cb)
    from PySide6.QtWidgets import QGraphicsOpacityEffect
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(start)
    widget.setGraphicsEffect(effect)
    return animate_property(effect, b"opacity", end, duration=duration,
                            finished_cb=finished_cb)


def animate_glass_state(panel, end_radius: float, end_bg_alpha: float,
                        end_shadow_blur: float = None, duration=320,
                        easing=None, finished_cb=None):
    """Animate a GlassPanel's cornerRadius/bgAlpha and optional shadow blur."""
    group = QParallelAnimationGroup(panel)

    radius_anim = QPropertyAnimation(panel, b"cornerRadius", panel)
    radius_anim.setDuration(duration)
    radius_anim.setStartValue(panel.cornerRadius)
    radius_anim.setEndValue(end_radius)
    radius_anim.setEasingCurve(easing or DEFAULT_EASING)
    group.addAnimation(radius_anim)

    alpha_anim = QPropertyAnimation(panel, b"bgAlpha", panel)
    alpha_anim.setDuration(duration)
    alpha_anim.setStartValue(panel.bgAlpha)
    alpha_anim.setEndValue(end_bg_alpha)
    alpha_anim.setEasingCurve(easing or DEFAULT_EASING)
    group.addAnimation(alpha_anim)

    if end_shadow_blur is not None and hasattr(panel, "shadowBlur"):
        blur_anim = QPropertyAnimation(panel, b"shadowBlur", panel)
        blur_anim.setDuration(duration)
        blur_anim.setStartValue(panel.shadowBlur)
        blur_anim.setEndValue(end_shadow_blur)
        blur_anim.setEasingCurve(easing or DEFAULT_EASING)
        group.addAnimation(blur_anim)

    if finished_cb:
        group.finished.connect(finished_cb)
    group.start(QParallelAnimationGroup.DeletionPolicy.DeleteWhenStopped)
    return group
