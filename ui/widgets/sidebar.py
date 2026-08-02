"""
Sidebar — navigation + conversation history.

    header        Nova orb (voice-state aware) + wordmark + status
    new chat      gradient primary action
    search        filters the conversation list
    history       conversation rows (active state, hover delete)
    footer        settings gear + collapse button

Width is animated by the parent window; this widget just toggles which
labels are visible in collapsed mode.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QRectF, QSize, Signal, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QFrame,
)

from .. import icons
from ..theme import (
    BG_1, BG_2, BG_3, BG_4, BORDER, BORDER_FOCUS, TEXT, TEXT_SOFT, TEXT_FAINT,
    ACCENT, ACCENT_2, ACCENT_GLOW, FONT_FAMILY, FONT_BODY, FONT_SMALL,
    FONT_TINY, STATUS_LABELS, ASSISTANT_NAME, rgba,
)
from ..animations import animate_property
from .avatar import NovaAvatar
from .icon_button import IconButton


class _NewChatButton(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(44)
        self._hovered = False
        self._pressed = False

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(rect, 13, 13)

        if self._hovered and not self._pressed:
            glow = QColor(ACCENT)
            glow.setAlpha(60)
            painter.setBrush(glow)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 15, 15)

        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(ACCENT))
        gradient.setColorAt(1.0, QColor(ACCENT_2))
        painter.setPen(Qt.NoPen)
        painter.fillPath(path, gradient)

        plus = icons.pixmap("plus", "#FFFFFF", 15)
        painter.drawPixmap(16, (self.height() - plus.height()) // 2, plus)

        painter.setPen(QColor("#FFFFFF"))
        font = painter.font()
        font.setFamily(FONT_FAMILY)
        font.setPointSizeF(9.4)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(
            QRectF(44, 0, self.width() - 50, self.height()),
            Qt.AlignVCenter | Qt.AlignLeft, "New conversation",
        )


class _ConversationRow(QWidget):
    selected = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, conv_id: str, title: str, updated: float, parent=None):
        super().__init__(parent)
        self.conv_id = conv_id
        self.title = title
        self.updated = updated
        self.setFixedHeight(44)
        self._hovered = False
        self._active = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_active(self, active: bool):
        self._active = active
        self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().x()
            if self._hovered and pos > self.width() - 26 and not self._active:
                self.deleteRequested.emit(self.conv_id)
            else:
                self.selected.emit(self.conv_id)
        super().mouseReleaseEvent(event)

    def _time_text(self) -> str:
        delta = time.time() - self.updated
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta // 60)}m"
        if delta < 86400:
            return f"{int(delta // 3600)}h"
        if delta < 86400 * 7:
            return f"{int(delta // 86400)}d"
        return time.strftime("%b %d", time.localtime(self.updated))

    def sizeHint(self):
        return QSize(220, 44)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(4, 2, self.width() - 8, self.height() - 4)

        if self._active:
            path = QPainterPath()
            path.addRoundedRect(rect, 10, 10)
            base = QColor(ACCENT)
            base.setAlpha(46)
            painter.fillPath(path, base)
            pen_color = QColor(ACCENT)
            pen_color.setAlpha(110)
            pen = QPen(pen_color)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawPath(path)
            color = "#F2F5FF"
        elif self._hovered:
            path = QPainterPath()
            path.addRoundedRect(rect, 10, 10)
            painter.fillPath(path, QColor(BG_3))
            color = TEXT
        else:
            color = TEXT_SOFT

        # chat glyph
        icon_px = icons.pixmap(
            "chat", "#AEB9CC" if not self._active else "#E8EDFF", 14,
        )
        painter.drawPixmap(16, (self.height() - icon_px.height()) // 2, icon_px)

        # title
        painter.setPen(QColor(color))
        font = painter.font()
        font.setFamily(FONT_FAMILY)
        font.setPointSizeF(8.8)
        font.setWeight(QFont.Weight.Medium if not self._active else QFont.Weight.DemiBold)
        painter.setFont(font)
        title = self.title if len(self.title) <= 26 else self.title[:25] + "…"
        painter.drawText(
            QRectF(38, 0, self.width() - 74, self.height()),
            Qt.AlignVCenter | Qt.AlignLeft, title,
        )

        # time
        painter.setPen(QColor("#8B97AD" if not self._active else "#C9D4F5"))
        font.setWeight(QFont.Weight.Normal)
        font.setPointSizeF(7.6)
        painter.setFont(font)
        painter.drawText(
            QRectF(self.width() - 44, 0, 34, self.height()),
            Qt.AlignVCenter | Qt.AlignRight, self._time_text(),
        )

        # delete on hover
        if self._hovered and not self._active:
            trash = icons.pixmap("trash", TEXT_FAINT, 12)
            painter.drawPixmap(
                self.width() - 20, (self.height() - trash.height()) // 2, trash,
            )


class Sidebar(QWidget):
    """Conversation history + navigation rail."""

    newChatRequested = Signal()
    conversationSelected = Signal(str)
    conversationDeleteRequested = Signal(str)
    settingsRequested = Signal()
    collapseRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self._convs = []
        self._active_id = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # ---- header ----
        header = QHBoxLayout()
        header.setSpacing(10)
        header.setContentsMargins(4, 2, 4, 0)
        self.avatar = NovaAvatar(size=36)
        header.addWidget(self.avatar)

        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        self.name_label = QLabel(ASSISTANT_NAME)
        self.name_label.setStyleSheet(
            f"color: {TEXT}; font-family: {FONT_FAMILY}; font-size: 14px;"
            "font-weight: 700; background: transparent;"
        )
        self.status_label = QLabel(STATUS_LABELS["idle"])
        self.status_label.setStyleSheet(
            f"color: {TEXT_FAINT}; font-family: {FONT_FAMILY}; font-size: {FONT_TINY}px;"
            "font-weight: 500; background: transparent;"
        )
        name_col.addWidget(self.name_label)
        name_col.addWidget(self.status_label)
        header.addLayout(name_col)
        header.addStretch(1)
        outer.addLayout(header)

        # ---- new chat ----
        self.new_chat_btn = _NewChatButton()
        self.new_chat_btn.clicked.connect(self.newChatRequested.emit)
        outer.addWidget(self.new_chat_btn)

        # ---- search ----
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search conversations")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedHeight(34)
        self.search_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background: {BG_3};
                border: 1px solid {BORDER};
                border-radius: 10px;
                color: {TEXT};
                font-family: {FONT_FAMILY};
                font-size: {FONT_SMALL}px;
                padding: 0 10px 0 30px;
                selection-background-color: {rgba(ACCENT, 90)};
            }}
            QLineEdit:focus {{
                border: 1px solid {BORDER_FOCUS};
            }}
            QLineEdit::placeholder {{ color: {TEXT_FAINT}; }}
            QLineEdit::clear-button {{
                image: none;
                width: 0px;
            }}
            """
        )
        self.search_edit.textChanged.connect(self._apply_filter)
        outer.addWidget(self.search_edit)

        # ---- conversations ----
        self._list_host = QWidget()
        self._list_host.setAttribute(Qt.WA_TranslucentBackground)
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 2, 0, 0)
        self._list_layout.setSpacing(1)
        self._list_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; border: none; margin: 2px 2px 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,0.12); border-radius: 3px; min-height: 24px; }"
            "QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.22); }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        )
        self._scroll.setWidget(self._list_host)
        outer.addWidget(self._scroll, 1)

        # ---- footer ----
        footer = QHBoxLayout()
        footer.setSpacing(4)
        footer.setContentsMargins(4, 4, 4, 0)
        self.settings_btn = IconButton("gear", size=34, tooltip="Settings")
        self.settings_btn.clicked.connect(self.settingsRequested.emit)
        footer.addWidget(self.settings_btn)
        self.collapse_btn = IconButton(
            "chevron-left", size=34, tooltip="Collapse sidebar",
        )
        self.collapse_btn.clicked.connect(self.collapseRequested.emit)
        footer.addWidget(self.collapse_btn)
        footer.addStretch(1)
        outer.addLayout(footer)

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def set_name(self, name: str):
        self.name_label.setText(name)

    def set_status(self, state: str):
        self.avatar.set_state(state)
        self.status_label.setText(STATUS_LABELS.get(state, STATUS_LABELS["idle"]))

    def set_conversations(self, conversations: list):
        """conversations: list of {"id", "title", "updated"}."""
        self._convs = conversations
        self._apply_filter()

    def _apply_filter(self):
        query = self.search_edit.text().strip().lower()
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for conv in self._convs:
            if query and query not in conv["title"].lower():
                continue
            row = _ConversationRow(conv["id"], conv["title"], conv["updated"])
            row.set_active(conv["id"] == self._active_id)
            row.selected.connect(self.conversationSelected.emit)
            row.deleteRequested.connect(self.conversationDeleteRequested.emit)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)

        if self._list_layout.count() == 1:
            empty = QLabel("No conversations yet")
            empty.setStyleSheet(
                f"color: {TEXT_FAINT}; font-family: {FONT_FAMILY};"
                f"font-size: {FONT_SMALL}px; background: transparent;"
                "padding: 12px 4px;"
            )
            empty.setAlignment(Qt.AlignCenter)
            self._list_layout.insertWidget(0, empty)

    def set_active(self, conv_id: str):
        self._active_id = conv_id
        for i in range(self._list_layout.count() - 1):
            item = self._list_layout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, _ConversationRow):
                widget.set_active(widget.conv_id == conv_id)

    def highlight_new(self):
        """Pulse the new-chat button after a fresh conversation starts."""
        pass  # keep simple

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self.name_label.setVisible(not collapsed)
        self.status_label.setVisible(not collapsed)
        self.search_edit.setVisible(not collapsed)
        self.new_chat_btn.setVisible(not collapsed)
        self._scroll.setVisible(not collapsed)
        self.collapse_btn.set_icon_name(
            "chevron-right" if collapsed else "chevron-left"
        )
        self.collapse_btn.setToolTip(
            "Expand sidebar" if collapsed else "Collapse sidebar"
        )

    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(rect, 20, 20)

        base = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        base.setColorAt(0.0, QColor(BG_2))
        base.setColorAt(1.0, QColor(BG_1))
        painter.fillPath(path, base)

        pen = QPen(QColor(BORDER), 1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
