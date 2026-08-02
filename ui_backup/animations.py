"""
Small, reusable animation helpers built on QPropertyAnimation.
Nothing here knows about Nova's states — it just animates widget geometry
and GlassPanel's cornerRadius/bgAlpha properties smoothly.
"""
from PySide6.QtCore import QPropertyAnimation, QParallelAnimationGroup, QEasingCurve, QRect


def animate_geometry(widget, end_rect: QRect, duration=380,
                      easing=QEasingCurve.OutCubic, finished_cb=None):
    """Animate a widget's geometry (position + size) to end_rect."""
    anim = QPropertyAnimation(widget, b"geometry", widget)
    anim.setDuration(duration)
    anim.setStartValue(widget.geometry())
    anim.setEndValue(end_rect)
    anim.setEasingCurve(easing)
    if finished_cb:
        anim.finished.connect(finished_cb)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def animate_glass_state(panel, end_radius: float, end_bg_alpha: float,
                         duration=380, easing=QEasingCurve.OutCubic, finished_cb=None):
    """Animate a GlassPanel's cornerRadius + bgAlpha together (the 'morph')."""
    group = QParallelAnimationGroup(panel)

    radius_anim = QPropertyAnimation(panel, b"cornerRadius", panel)
    radius_anim.setDuration(duration)
    radius_anim.setStartValue(panel.cornerRadius)
    radius_anim.setEndValue(end_radius)
    radius_anim.setEasingCurve(easing)
    group.addAnimation(radius_anim)

    alpha_anim = QPropertyAnimation(panel, b"bgAlpha", panel)
    alpha_anim.setDuration(duration)
    alpha_anim.setStartValue(panel.bgAlpha)
    alpha_anim.setEndValue(end_bg_alpha)
    alpha_anim.setEasingCurve(easing)
    group.addAnimation(alpha_anim)

    if finished_cb:
        group.finished.connect(finished_cb)
    group.start(QParallelAnimationGroup.DeletionPolicy.DeleteWhenStopped)
    return group


def fade_widget(widget, opacity_effect, start, end, duration=200, finished_cb=None):
    """Fade a widget's QGraphicsOpacityEffect between two opacity values."""
    anim = QPropertyAnimation(opacity_effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    if finished_cb:
        anim.finished.connect(finished_cb)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim
