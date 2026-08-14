"""
Message rows — the redesigned chat bubbles.

    UserBubble      — right-aligned, surface-container-high, rounded-2xl
                      with a small corner kick (rounded-tr-sm)
    AssistantRow    — mini orb + name + time header, hover action buttons
                      (copy / regenerate / share), and a frosted glass card
                      body built from markdown blocks (paragraphs, code
                      cards, tables, lists, quotes)
    TypingIndicator — three bouncing dots while Nova thinks
"""
from __future__ import annotations

import html as _html
import time

from PySide6.QtCore import Qt, QRectF, QTimer, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGraphicsOpacityEffect,
)

from .. import icons, theme
from .markdown import parse_markdown, table_html
from .code_block import CodeBlock
from .glass import GlassIconButton, GlassCard
from .nova_orb import NovaOrb


def fade_in(widget: QWidget, duration: int = 260):
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


def relative_time(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return time.strftime("%b %d", time.localtime(ts))


class UserBubble(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._text_label = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(0)
        self._text_label = QLabel("")
        self._text_label.setStyleSheet(
            f"color: {theme.TEXT}; font-family: {theme.FONT_FAMILY};"
            f" font-size: {theme.FONT_BODY}px; background: transparent;"
            " line-height: 1.5;"
        )
        self._text_label.setTextFormat(Qt.RichText)
        self._text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._text_label.setWordWrap(True)
        lay.addWidget(self._text_label)

    def set_text(self, text: str):
        self._text = text
        escaped = _html.escape(text).replace("\n", "<br/>")
        self._text_label.setText(escaped)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        r = 16
        path.addRoundedRect(rect, r, r)
        # rounded-tr-sm kick
        path2 = QPainterPath()
        path2.addRect(rect.right() - 8, rect.top(), 8, 10)
        recti = rect.adjusted(0, 0, -8, 0)
        kick = QPainterPath()
        kick.addRect(recti.right(), recti.top(), 8, 9)
        final = path.subtracted(kick)
        p.fillPath(final, QColor(theme.SURFACE_HIGH))
        p.setPen(QPen(QColor(theme.BORDER), 1.0))
        p.drawPath(final)

    def apply_theme(self):
        self._text_label.setStyleSheet(
            f"color: {theme.TEXT}; font-family: {theme.FONT_FAMILY};"
            f" font-size: {theme.FONT_BODY}px; background: transparent; line-height: 1.5;"
        )
        self.update()


class AssistantRow(QWidget):
    """Mini orb + name header, hover actions, glass card body (markdown)."""

    copyRequested = None  # set by ChatPage if needed

    def __init__(self, parent=None):
        super().__init__(parent)
        self._name = theme.ASSISTANT_NAME
        self._ts = 0.0
        self._text = ""
        self._actions_visible = False

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(8)

        # ---- header row ----
        self._header = QWidget(self)
        self._header.setAttribute(Qt.WA_TranslucentBackground)
        hlay = QHBoxLayout(self._header)
        hlay.setContentsMargins(2, 0, 2, 0)
        hlay.setSpacing(8)
        self.orb = NovaOrb(size=24)
        self.orb.set_state("thinking")
        hlay.addWidget(self.orb, 0, Qt.AlignVCenter)

        self._name_label = QLabel(self._name)
        self._name_label.setStyleSheet(
            theme.text_qss(size=13, weight=600, color=theme.TEXT)
        )
        hlay.addWidget(self._name_label, 0, Qt.AlignVCenter)

        self._time_label = QLabel("")
        self._time_label.setStyleSheet(theme.hint_qss(size=11))
        hlay.addWidget(self._time_label, 0, Qt.AlignVCenter)

        hlay.addStretch(1)

        self._actions = QWidget(self._header)
        self._actions.setAttribute(Qt.WA_TranslucentBackground)
        alay = QHBoxLayout(self._actions)
        alay.setContentsMargins(0, 0, 0, 0)
        alay.setSpacing(2)
        self._action_buttons = []
        for icon_name, tip in (("copy", "Copy reply"), ("refresh", "Regenerate"),
                               ("share", "Share")):
            btn = GlassIconButton(icon_name, size=26, icon_size=13, tooltip=tip,
                                  parent=self._actions)
            alay.addWidget(btn)
            self._action_buttons.append(btn)
        self._actions.setVisible(False)
        hlay.addWidget(self._actions, 0, Qt.AlignVCenter)
        self._root.addWidget(self._header)

        # ---- body card ----
        self._card = GlassCard(radius=18)
        self._card.body.setSpacing(10)
        self._root.addWidget(self._card)

        self._header.enterEvent = self._header_enter
        self._header.leaveEvent = self._header_leave

    def _header_enter(self, event):
        self._actions.setVisible(True)
        super(type(self._header), self._header).enterEvent(event)

    def _header_leave(self, event):
        self._actions.setVisible(False)
        super(type(self._header), self._header).leaveEvent(event)

    # ------------------------------------------------------------------ #
    def set_name(self, name: str):
        self._name = name
        self._name_label.setText(name)

    def set_time(self, ts: float):
        self._ts = ts or time.time()
        self._time_label.setText(relative_time(self._ts))

    def set_text(self, text: str):
        self._text = text
        self._render()

    def _render(self):
        # wipe the card body
        while self._card.body.count():
            item = self._card.body.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        blocks = parse_markdown(self._text)
        for block in blocks:
            kind = block["type"]
            if kind == "code":
                cb = CodeBlock(block.get("code", ""), block.get("lang", ""))
                self._card.body.addWidget(cb)
            elif kind == "paragraph":
                lab = QLabel(block.get("html", ""))
                lab.setTextFormat(Qt.RichText)
                lab.setOpenExternalLinks(True)
                lab.setWordWrap(True)
                lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
                lab.setStyleSheet(theme.text_qss(size=14, weight=400, color=theme.TEXT))
                self._card.body.addWidget(lab)
            elif kind == "heading":
                size = {1: 17, 2: 15, 3: 13.5}[block.get("level", 2)]
                lab = QLabel(block.get("html", ""))
                lab.setTextFormat(Qt.RichText)
                lab.setWordWrap(True)
                lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
                lab.setStyleSheet(theme.text_qss(size=size, weight=700, color=theme.TEXT))
                self._card.body.addWidget(lab)
            elif kind == "list":
                tag = "ol" if block.get("ordered") else "ul"
                items = "".join(f"<li>{it}</li>" for it in block.get("items", []))
                lab = QLabel(f"<{tag}>{items}</{tag}>")
                lab.setTextFormat(Qt.RichText)
                lab.setWordWrap(True)
                lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
                lab.setStyleSheet(theme.text_qss(size=14, weight=400, color=theme.TEXT))
                self._card.body.addWidget(lab)
            elif kind == "quote":
                lab = QLabel(
                    f'<div style="border-left:2px solid {theme.ACCENT}; padding:2px 12px;'
                    f' color:{theme.TEXT_SOFT};">{block.get("html", "")}</div>'
                )
                lab.setTextFormat(Qt.RichText)
                lab.setWordWrap(True)
                lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
                lab.setStyleSheet("background: transparent;")
                self._card.body.addWidget(lab)
            elif kind == "table":
                lab = QLabel(table_html(block))
                lab.setTextFormat(Qt.RichText)
                lab.setWordWrap(False)
                lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
                self._card.body.addWidget(lab)
            elif kind == "hr":
                line = QWidget()
                line.setFixedHeight(1)
                line.setStyleSheet(f"background: {theme.BORDER};")
                self._card.body.addWidget(line)

        self._card.body.addStretch(1)

    def apply_theme(self):
        self._name_label.setStyleSheet(
            theme.text_qss(size=13, weight=600, color=theme.TEXT)
        )
        self._time_label.setStyleSheet(theme.hint_qss(size=11))
        self._render()
        self.update()


class TypingIndicator(QWidget):
    """Three bouncing dots, animated with a paint timer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        self._phase += 1
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = 3
        spacing = 8
        total = n * 7 + (n - 1) * spacing
        x0 = (self.width() - total) / 2.0
        cy = self.height() / 2.0
        for i in range(n):
            off = (self._phase + i) % 4
            lift = 0.0
            if off == 1:
                lift = -5.0
            elif off == 2:
                lift = -2.0
            cx = x0 + i * (7 + spacing) + 3.5
            r = 3.0 if off == 0 else 3.0
            color = QColor(theme.ACCENT)
            alpha = int(120 + (off == 1) * 135)
            color.setAlpha(alpha)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(cx - r, cy + lift - r, r * 2, r * 2))

    def apply_theme(self):
        self.update()
