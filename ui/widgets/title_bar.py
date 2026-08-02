"""
TitleBar — the frameless window chrome.

A draggable 52px bar with the Nova wordmark and status, plus
hand-painted window controls (minimize / maximize / close with a
soft red hover). The rest of the window stays under the cursor for
dragging; double-click toggles maximize.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient, QMouseEvent, QPainterPath
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel

from .. import icons
from ..theme import (
    TITLEBAR_H, TEXT, TEXT_FAINT, TEXT_SOFT, ACCENT, ACCENT_2,
    FONT_FAMILY, FONT_TINY, BORDER, BG_4, ASSISTANT_NAME,
)
from .icon_button import IconButton


class _LogoMark(QWidget):
    """The Nova app glyph: a gradient tile with a hand-painted "N" monogram.

    Drawn with strokes (not a font) so it renders crisply at 22px with no
    clipping, and so it reads cleanly at a glance in the title bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 22)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)

        tile = QPainterPath()
        tile.addRoundedRect(rect, 7, 7)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(ACCENT))
        gradient.setColorAt(1.0, QColor(ACCENT_2))
        painter.fillPath(tile, gradient)
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1.0))
        painter.drawPath(tile)

        # "N": two vertical legs + a diagonal, thick round-cap strokes
        pen = QPen(QColor("#FFFFFF"), 3.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        left_x, right_x = 6.2, 15.8
        top_y, bottom_y = 5.2, 16.8
        painter.drawLine(QPointF(left_x, top_y), QPointF(left_x, bottom_y))
        painter.drawLine(QPointF(left_x, top_y), QPointF(right_x, bottom_y))
        painter.drawLine(QPointF(right_x, top_y), QPointF(right_x, bottom_y))

        # small accent dot: the "supernova" highlight, top-right
        painter.setBrush(QColor("#FFFFFF"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(16.6, 5.2), 1.35, 1.35)


class _WindowControl(QWidget):
    """A window control button (minimize / maximize / close)."""

    clicked = Signal()

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(46, 36)
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
        if event.button() == Qt.LeftButton and self._pressed:
            self.clicked.emit()
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())

        if self._hovered:
            if self.kind == "close":
                painter.fillRect(rect, QColor(232, 72, 77, 200))
                color = "#FFFFFF"
            else:
                painter.fillRect(rect, QColor(BG_4))
                color = "#EDF0F6"
        else:
            color = TEXT_FAINT

        if self.kind == "minimize":
            icon_px = icons.pixmap("minus", color, 13)
            painter.drawPixmap((self.width() - icon_px.width()) // 2,
                               (self.height() - icon_px.height()) // 2, icon_px)
        elif self.kind == "maximize":
            icon_px = icons.pixmap("maximize", color, 12)
            painter.drawPixmap((self.width() - icon_px.width()) // 2,
                               (self.height() - icon_px.height()) // 2, icon_px)
        else:
            icon_px = icons.pixmap("x", color, 13)
            painter.drawPixmap((self.width() - icon_px.width()) // 2,
                               (self.height() - icon_px.height()) // 2, icon_px)


class TitleBar(QWidget):
    """Draggable window chrome with wordmark + window controls."""

    sidebarToggle = Signal()
    minimizeRequested = Signal()
    maximizeRequested = Signal()
    closeRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(TITLEBAR_H)
        self._drag_offset = None
        self._maximized = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(6)

        self.sidebar_btn = IconButton(
            "panel-left", size=32, tooltip="Toggle sidebar",
        )
        self.sidebar_btn.clicked.connect(self.sidebarToggle.emit)
        layout.addWidget(self.sidebar_btn)

        self.mark = _LogoMark()
        layout.addWidget(self.mark)

        self.wordmark = QLabel(ASSISTANT_NAME)
        self.wordmark.setStyleSheet(
            f"color: {TEXT}; font-family: {FONT_FAMILY}; font-size: 14px;"
            "font-weight: 700; background: transparent; letter-spacing: 0.4px;"
        )
        layout.addWidget(self.wordmark)

        self.status = QLabel("")
        self.status.setStyleSheet(
            f"color: {TEXT_FAINT}; font-family: {FONT_FAMILY}; font-size: {FONT_TINY}px;"
            "font-weight: 500; background: transparent;"
        )
        layout.addWidget(self.status)
        layout.addStretch(1)

        self.min_btn = _WindowControl("minimize")
        self.min_btn.clicked.connect(self.minimizeRequested.emit)
        layout.addWidget(self.min_btn)

        self.max_btn = _WindowControl("maximize")
        self.max_btn.clicked.connect(self.maximizeRequested.emit)
        layout.addWidget(self.max_btn)

        self.close_btn = _WindowControl("close")
        self.close_btn.clicked.connect(self.closeRequested.emit)
        layout.addWidget(self.close_btn)

    # ------------------------------------------------------------------ #
    def set_name(self, name: str):
        self.wordmark.setText(name)

    def set_status(self, text: str, accent: bool = False):
        self.status.setText(text)
        self.status.setStyleSheet(
            f"color: {TEXT_FAINT if not accent else ACCENT};"
            f"font-family: {FONT_FAMILY}; font-size: {FONT_TINY}px;"
            "font-weight: 500; background: transparent;"
        )

    def set_maximized_state(self, maximized: bool):
        self._maximized = maximized

    # ------------------------------------------------------------------ #
    # dragging
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().pos()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_offset is not None:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        self.maximizeRequested.emit()
        super().mouseDoubleClickEvent(event)

    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # hairline divider
        painter.setPen(QPen(QColor(BORDER), 1.0))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
