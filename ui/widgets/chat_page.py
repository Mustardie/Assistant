"""
ChatPage — the flagship chat experience.

Layout (top → bottom):
    header row      — conversation title + model selector pill group
    message area    — scrollable: welcome (empty state), message rows,
                      typing indicator
    input dock      — floating pill centered under the messages

Owns the welcome view, message rendering (including streaming updates
to the last assistant row) and the typing indicator, exactly like the
old NovaWindow did — so NovaWindow stays a thin shell.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QRectF, Signal, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
    QSizePolicy,
)

from .. import icons, theme
from .glass import label
from .message_bubble import UserBubble, AssistantRow, TypingIndicator, fade_in, relative_time
from .chat_input import InputDock
from .welcome_view import WelcomeView


class _ModelPill(QWidget):
    clicked = Signal()

    def __init__(self, text: str, active: bool = False, parent=None):
        super().__init__(parent)
        self._text = text
        self._active = active
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(30)

    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(max(52, self.fontMetrics().horizontalAdvance(self._text) + 26), 30)

    def set_active(self, active: bool):
        self._active = active
        self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        if self._active:
            p.fillPath(path, QColor(theme.SURFACE_HIGH))
            p.setPen(QPen(QColor(theme.BORDER_STRONG), 1.0))
            p.drawPath(path)
            p.setPen(QColor(theme.TEXT))
        elif self._hover:
            p.fillPath(path, QColor(theme.HOVER))
            p.setPen(QColor(theme.TEXT_SOFT))
        else:
            p.setPen(QColor(theme.TEXT_FAINT))
        font = p.font()
        font.setFamily(theme.FONT_FAMILY)
        font.setPointSizeF(9)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignCenter, self._text)


class ChatPage(QWidget):
    textSubmitted = Signal(str)
    voicePressed = Signal()
    attachRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reply_live = False
        self._typing_row = None
        self._welcome_shown = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ---- header ----
        self._header = QWidget(self)
        self._header.setFixedHeight(58)
        self._header.setAttribute(Qt.WA_TranslucentBackground)
        header_lay = QHBoxLayout(self._header)
        header_lay.setContentsMargins(28, 0, 28, 0)
        header_lay.setSpacing(12)

        self._conv_title = QLabel("New conversation")
        self._conv_title.setStyleSheet(
            theme.text_qss(size=15.5, weight=700, color=theme.TEXT)
        )
        header_lay.addWidget(self._conv_title, 1)

        self._model_pill_group = QWidget(self._header)
        self._model_pill_group.setAttribute(Qt.WA_TranslucentBackground)
        group_lay = QHBoxLayout(self._model_pill_group)
        group_lay.setContentsMargins(0, 0, 0, 0)
        group_lay.setSpacing(4)
        self._model_pills: list[_ModelPill] = []
        self._provider_pill = _ModelPill("Ollama")
        self._provider_pill.clicked.connect(self.settingsRequested.emit)
        group_lay.addWidget(self._provider_pill)
        self._model_pill = _ModelPill("—", active=True)
        self._model_pill.clicked.connect(self.settingsRequested.emit)
        group_lay.addWidget(self._model_pill)
        self._gear_pill = _ModelPill("Manage")
        self._gear_pill.clicked.connect(self.settingsRequested.emit)
        group_lay.addWidget(self._gear_pill)
        self._model_pills = [self._provider_pill, self._model_pill, self._gear_pill]
        header_lay.addWidget(self._model_pill_group, 0, Qt.AlignVCenter)
        lay.addWidget(self._header)

        # ---- messages ----
        self._messages_host = QWidget(self)
        self._messages_host.setAttribute(Qt.WA_TranslucentBackground)
        self._messages_layout = QVBoxLayout(self._messages_host)
        self._messages_layout.setContentsMargins(28, 8, 28, 8)
        self._messages_layout.setSpacing(18)

        self._welcome = WelcomeView(self._messages_host)
        self._welcome.promptPicked.connect(self._on_prompt_picked)
        self._messages_layout.addWidget(self._welcome, 1)
        self._welcome.setVisible(False)

        self._messages_layout.addStretch(1)

        self._messages_scroll = QScrollArea(self)
        self._messages_scroll.setWidgetResizable(True)
        self._messages_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._messages_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._messages_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._messages_scroll.setStyleSheet(theme.scroll_qss())
        self._messages_scroll.setWidget(self._messages_host)
        lay.addWidget(self._messages_scroll, 1)

        # ---- input dock (full page width, matching message margins) ----
        dock_host = QWidget(self)
        dock_host.setAttribute(Qt.WA_TranslucentBackground)
        dock_host.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        dock_lay = QHBoxLayout(dock_host)
        dock_lay.setContentsMargins(28, 0, 28, 14)
        self.input_bar = InputDock(dock_host)
        dock_lay.addWidget(self.input_bar)
        self.input_bar.submitted.connect(self._on_submit)
        self.input_bar.voicePressed.connect(self.voicePressed.emit)
        self.input_bar.attachRequested.connect(self.attachRequested.emit)
        lay.addWidget(dock_host)

    # ================================================================== #
    # public API (used by NovaWindow / main.py contract)
    # ================================================================== #
    def set_title(self, title: str):
        self._conv_title.setText(title or "New conversation")

    def set_model(self, provider: str, model: str):
        short = model.split(":")[0].split("/")[-1].strip() if model else "—"
        self._provider_pill._text = provider
        self._model_pill._text = short
        for pill in self._model_pills:
            pill.update()

    def show_welcome(self, visible: bool):
        self._welcome_shown = visible
        self._welcome.setVisible(visible)

    def clear(self):
        """Remove every message row, keep welcome + trailing stretch."""
        while self._messages_layout.count() > 2:
            item = self._messages_layout.takeAt(self._messages_layout.count() - 2)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._typing_row = None
        self._reply_live = False

    def append_user(self, text: str, ts=None):
        self._finish_typing()
        ts = ts or time.time()
        self.show_welcome(False)
        row = QWidget()
        row.setAttribute(Qt.WA_TranslucentBackground)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bubble = UserBubble()
        bubble.set_text(text)
        bubble.setMaximumWidth(660)
        lay.addStretch(1)
        lay.addWidget(bubble, 0, Qt.AlignTop)
        self._messages_layout.insertWidget(self._messages_layout.count() - 1, row)
        fade_in(row)
        QTimer.singleShot(0, self.scroll_to_bottom)

    def append_assistant(self, text: str, ts=None):
        """Show/stream an assistant reply. Progressive chunks accumulate."""
        text = (text or "").strip()
        if not text:
            return
        ts = ts or time.time()
        self.show_welcome(False)

        if self._typing_row is not None:
            self._finish_typing()
            row = AssistantRow()
            row.set_text(text)
            row.set_time(ts)
            self._messages_layout.insertWidget(self._messages_layout.count() - 1, row)
            fade_in(row)
            self._reply_live = True
        elif self._reply_live and self._last_assistant_row() is not None:
            row = self._last_assistant_row()
            prev = row._text
            if text.startswith(prev):
                row.set_text(text)
                row.updateGeometry()
            elif text != prev:
                row.set_text(prev + "\n\n" + text)
                row.updateGeometry()
        else:
            row = AssistantRow()
            row.set_text(text)
            row.set_time(ts)
            self._messages_layout.insertWidget(self._messages_layout.count() - 1, row)
            fade_in(row)
            self._reply_live = True
        QTimer.singleShot(0, self.scroll_to_bottom)

    def begin_reply(self):
        if self._typing_row is not None:
            return
        row = QWidget()
        row.setAttribute(Qt.WA_TranslucentBackground)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        orb = None
        from .nova_orb import NovaOrb
        orb = NovaOrb(size=24)
        orb.set_state("thinking")
        lay.addWidget(orb, 0, Qt.AlignTop)
        indicator = TypingIndicator()
        lay.addWidget(indicator, 0, Qt.AlignTop)
        lay.addStretch(1)
        self._messages_layout.insertWidget(self._messages_layout.count() - 1, row)
        self._typing_row = row
        QTimer.singleShot(0, self.scroll_to_bottom)

    def _finish_typing(self):
        if self._typing_row is not None:
            self._typing_row.deleteLater()
            self._typing_row = None

    def _last_assistant_row(self):
        count = self._messages_layout.count()
        for i in range(count - 2, 0, -1):
            w = self._messages_layout.itemAt(i).widget()
            if isinstance(w, AssistantRow):
                return w
        return None

    def set_voice_state(self, state: str):
        self.input_bar.set_voice_state(state)

    def focus_input(self):
        self.input_bar.focus_input()

    def scroll_to_bottom(self):
        bar = self._messages_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_assistant_name(self, name: str):
        count = self._messages_layout.count()
        for i in range(count):
            w = self._messages_layout.itemAt(i).widget()
            if isinstance(w, AssistantRow):
                w.set_name(name)
        self.input_bar.set_name(name)

    # ================================================================== #
    # internals
    # ================================================================== #
    def _on_prompt_picked(self, text: str):
        self._on_submit(text)

    def _on_submit(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        self._reply_live = False
        self.textSubmitted.emit(text)

    def apply_theme(self):
        self._conv_title.setStyleSheet(
            theme.text_qss(size=15.5, weight=700, color=theme.TEXT)
        )
        self._messages_scroll.setStyleSheet(theme.scroll_qss())
        self._welcome.apply_theme()
        self.input_bar.apply_theme()
        for pill in self._model_pills:
            pill.update()
        count = self._messages_layout.count()
        for i in range(count):
            w = self._messages_layout.itemAt(i).widget()
            if w is not None and hasattr(w, "apply_theme"):
                try:
                    w.apply_theme()
                except Exception:
                    pass
        self.update()
