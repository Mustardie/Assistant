"""
WelcomeView — the new-chat empty state.

A gradient "Hello, I'm Nova" headline, a subtitle, and three prompt
cards (Code Analysis / Research / Planning). Clicking a card submits
its prompt.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont, QLinearGradient, QBrush
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel

from .. import icons, theme

PROMPT_CARDS = [
    ("code", "Analyze my code",
     "Debug, optimize, and explain any codebase",
     "Analyze this code block for potential performance optimizations"),
    ("globe", "Research a topic",
     "Get up-to-date information with sources",
     "Write a comprehensive research summary on recent market trends"),
    ("calendar", "Plan my day",
     "Build schedules and break big tasks down",
     "Plan my weekly deep-work schedule for maximum productivity"),
]


class _PromptCard(QWidget):
    picked = Signal(str)

    def __init__(self, icon_name: str, title: str, desc: str, prompt: str, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._title = title
        self._desc = desc
        self._prompt = prompt
        self._hover = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(0, 128)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)

        icon_box = QWidget()
        icon_box.setFixedSize(36, 36)
        icon_box.setStyleSheet(
            f"background: {theme.rgba(theme.ACCENT, 36)}; border-radius: 10px;"
        )
        ib_lay = QVBoxLayout(icon_box)
        ib_lay.setContentsMargins(0, 0, 0, 0)
        self._icon_label = QLabel()
        self._icon_label.setAlignment(Qt.AlignCenter)
        self._icon_label.setPixmap(icons.pixmap(icon_name, theme.ACCENT, 18))
        ib_lay.addWidget(self._icon_label)
        lay.addWidget(icon_box)
        self._icon_box = icon_box

        title_lab = QLabel(title)
        title_lab.setStyleSheet(theme.text_qss(size=14, weight=600, color=theme.TEXT))
        lay.addWidget(title_lab)
        self._title_lab = title_lab

        desc_lab = QLabel(desc)
        desc_lab.setStyleSheet(theme.text_qss(size=12, weight=400, color=theme.TEXT_SOFT))
        desc_lab.setWordWrap(True)
        lay.addWidget(desc_lab)
        self._desc_lab = desc_lab
        lay.addStretch(1)

    def _anim_to(self, prop, target):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(180)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.picked.emit(self._prompt)
        super().mouseReleaseEvent(event)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)

    def apply_theme(self):
        self._icon_box.setStyleSheet(
            f"background: {theme.rgba(theme.ACCENT, 36)}; border-radius: 10px;")
        self._icon_label.setPixmap(icons.pixmap(self._icon_name, theme.ACCENT, 18))
        self._title_lab.setStyleSheet(
            theme.text_qss(size=14, weight=600, color=theme.TEXT))
        self._desc_lab.setStyleSheet(
            theme.text_qss(size=12, weight=400, color=theme.TEXT_SOFT))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 16, 16)
        p.fillPath(path, QColor(theme.GLASS))
        if self._hover > 0:
            p.fillPath(path, QColor(theme.HOVER))
        border = QColor(theme.BORDER_FOCUS if self._hover > 0.5 else theme.BORDER)
        p.setPen(QPen(border, 1.2))
        p.drawPath(path)


class WelcomeView(QWidget):
    promptPicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 0, 40, 40)
        lay.setSpacing(8)
        lay.addStretch(2)

        self._headline = _GradientLabel(
            f"Hello, I'm {theme.ASSISTANT_NAME}")
        self._headline.setAlignment(Qt.AlignCenter)
        self._headline.setFixedHeight(52)
        lay.addWidget(self._headline)

        self._sub = QLabel(
            "What can I help you with today? I can code, research, and plan."
        )
        self._sub.setAlignment(Qt.AlignCenter)
        self._sub.setStyleSheet(
            theme.text_qss(size=15, weight=400, color=theme.TEXT_SOFT)
        )
        lay.addWidget(self._sub)
        lay.addSpacing(26)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        self._cards = []
        for icon_name, title, desc, prompt in PROMPT_CARDS:
            card = _PromptCard(icon_name, title, desc, prompt)
            card.picked.connect(self.promptPicked.emit)
            cards.addWidget(card, 1)
            self._cards.append(card)
        lay.addLayout(cards)

        lay.addStretch(3)

    def apply_theme(self):
        self._headline.update()
        self._sub.setStyleSheet(
            theme.text_qss(size=15, weight=400, color=theme.TEXT_SOFT)
        )
        for card in self._cards:
            card.apply_theme()

    def set_assistant_name(self, name: str):
        self._headline._text = f"Hello, I'm {name}"
        self._headline.update()


class _GradientLabel(QWidget):
    """Text painted with the indigo → purple → pink gradient."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._text = text
        self._align = Qt.AlignCenter

    def setAlignment(self, align):
        self._align = align
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        font = p.font()
        font.setFamily(theme.FONT_FAMILY)
        font.setPointSizeF(26)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)

        w = p.fontMetrics().horizontalAdvance(self._text)
        grad = QLinearGradient(0, 0, w, 0)
        grad.setColorAt(0.0, QColor(theme.GRADIENT_1))
        grad.setColorAt(0.55, QColor(theme.GRADIENT_2))
        grad.setColorAt(1.0, QColor(theme.GRADIENT_3))
        p.setPen(QPen(QBrush(grad), 1.0))
        p.drawText(self.rect(), self._align, self._text)
