"""
Message bubbles — the heart of the conversation.

    UserBubble       right-aligned gradient chip, crisp white text
    AssistantBubble  glass card with the mini Nova orb and name row
    TypingIndicator  three soft pulsing dots while Nova thinks

Every bubble fades in gently when it appears.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QRectF, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import (
    QColor, QPainter, QPen, QLinearGradient, QPainterPath,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGraphicsOpacityEffect,
)

from ..theme import (
    BG_2, BG_3, BORDER, TEXT, TEXT_FAINT, TEXT_SOFT, ACCENT, ACCENT_2,
    FONT_FAMILY, FONT_BODY, FONT_SMALL, FONT_TINY, ASSISTANT_NAME,
)
from .avatar import NovaAvatar


def _bubble_font(size=FONT_BODY, weight="normal"):
    return f"font-family: {FONT_FAMILY}; font-size: {size}px; font-weight: {weight};"


def fade_in(widget, duration=260):
    """Fade a widget in from transparent.

    The opacity effect is removed as soon as the animation finishes:
    keeping it would force the widget to render through an offscreen
    texture, which glitches inside a QScrollArea (messages appear cut
    off / stutter while scrolling)."""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


def bubble_path(w: float, h: float, tl=18, tr=18, br=18, bl=18) -> QPainterPath:
    """Rounded rect with an independent radius per corner.

    Qt arc angles: 0°=right, 90°=top, 180°=left, 270°=bottom, and a
    positive sweep is counter-clockwise on screen. The bubble outline
    runs clockwise, so every corner arcs the short way with a NEGATIVE
    sweep (-90°)."""
    tl = max(0.0, min(tl, w / 2, h / 2))
    tr = max(0.0, min(tr, w / 2, h / 2))
    br = max(0.0, min(br, w / 2, h / 2))
    bl = max(0.0, min(bl, w / 2, h / 2))
    path = QPainterPath()
    path.moveTo(tl, 0)
    path.lineTo(w - tr, 0)
    path.arcTo(w - 2 * tr, 0, 2 * tr, 2 * tr, 90, -90)
    path.lineTo(w, h - br)
    path.arcTo(w - 2 * br, h - 2 * br, 2 * br, 2 * br, 0, -90)
    path.lineTo(bl, h)
    path.arcTo(0, h - 2 * bl, 2 * bl, 2 * bl, 270, -90)
    path.lineTo(0, tl)
    path.arcTo(0, 0, 2 * tl, 2 * tl, 180, -90)
    path.closeSubpath()
    return path


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #
class MessageBubble(QWidget):
    """Base class: a selectable text surface."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._time = time.time()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel(self)
        self._label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.PlainText)
        layout.addWidget(self._label)

    def set_text(self, text: str):
        self._label.setText(text)

    def text(self) -> str:
        return self._label.text()

    def set_max_width(self, width: int):
        self._label.setMaximumWidth(width)


class UserBubble(MessageBubble):
    """Right-aligned gradient chip for user messages."""

    PAD_X = 18
    PAD_Y = 12
    MAX_BUBBLE_WIDTH = 640

    def __init__(self, parent=None):
        super().__init__(parent)
        self._label.setAlignment(Qt.AlignLeft)
        self._label.setStyleSheet(
            f"color: #FFFFFF; {_bubble_font(FONT_BODY, 500)}; background: transparent;"
        )
        self._label.setContentsMargins(self.PAD_X, self.PAD_Y, self.PAD_X, self.PAD_Y)
        self.setMaximumWidth(self.MAX_BUBBLE_WIDTH + self.PAD_X * 2)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = bubble_path(rect.width(), rect.height(), tl=18, tr=18, br=6, bl=18)

        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(ACCENT))
        gradient.setColorAt(1.0, QColor(ACCENT_2))
        painter.setBrush(gradient)
        painter.setPen(Qt.NoPen)
        painter.drawPath(path)


class AssistantBubble(MessageBubble):
    """Glass card with the Nova orb and a name/time row."""

    CORNER = 18
    MAX_BUBBLE_WIDTH = 760

    def __init__(self, parent=None):
        super().__init__(parent)
        self._label.setStyleSheet(
            f"color: {TEXT}; {_bubble_font(FONT_BODY, 400)};"
            f"background: transparent; line-height: 1.45;"
        )
        self._label.setContentsMargins(16, 10, 16, 16)
        self.setMaximumWidth(self.MAX_BUBBLE_WIDTH + 32)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = bubble_path(rect.width(), rect.height(), tl=6, tr=18, br=18, bl=18)

        painter.fillPath(path, QColor(BG_2))
        pen = QPen(QColor(BORDER), 1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        # soft top highlight for a glass feel
        highlight = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        highlight.setColorAt(0.0, QColor(255, 255, 255, 14))
        highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, highlight)


class AssistantRow(QWidget):
    """Avatar + name row + bubble stacked in a vertical group."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.setContentsMargins(2, 0, 0, 0)
        self.avatar = NovaAvatar(size=26)
        self.name = QLabel(ASSISTANT_NAME)
        self.name.setStyleSheet(
            f"color: {TEXT_SOFT}; {_bubble_font(FONT_SMALL, 600)}; background: transparent;"
        )
        self.time = QLabel("")
        self.time.setStyleSheet(
            f"color: {TEXT_FAINT}; {_bubble_font(FONT_TINY, 400)}; background: transparent;"
        )
        header.addWidget(self.avatar)
        header.addWidget(self.name)
        header.addWidget(self.time)
        header.addStretch(1)
        layout.addLayout(header)

        self.bubble = AssistantBubble()
        layout.addWidget(self.bubble)

    def set_name(self, name: str):
        self.name.setText(name)

    def set_text(self, text: str):
        self.bubble.set_text(text)

    def set_time(self, ts: float):
        self.time.setText(time.strftime("%I:%M %p", time.localtime(ts)))


class TypingIndicator(QWidget):
    """Three pulsing dots inside an assistant-style glass chip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(90)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedHeight(46)
        self.setMinimumWidth(64)

    def _tick(self):
        self._phase = (self._phase + 0.14) % 1.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = bubble_path(rect.width(), rect.height(), tl=6, tr=18, br=18, bl=18)
        painter.fillPath(path, QColor(BG_3))
        painter.setPen(QPen(QColor(BORDER), 1.0))
        painter.drawPath(path)

        cy = self.height() / 2
        gap = 16
        x0 = (self.width() - 2 * gap) / 2
        for i in range(3):
            wave = (self._phase - i * 0.22) % 1.0
            scale = 1.0 - abs(2 * wave - 1)
            r = 4.0 * (0.55 + 0.75 * scale)
            alpha = int(110 + 145 * scale)
            color = QColor(TEXT_SOFT)
            color.setAlpha(alpha)
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRectF(x0 + i * gap - r, cy - r, r * 2, r * 2))
