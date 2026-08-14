"""
TopBar — the 64px app bar of the redesign.

Center: nav links (Models · Workspace · Settings — Settings opens the
settings page). Right: Upgrade pill, notifications bell, account orb,
and the window controls (minimize / maximize / close). The whole bar is
a drag region for the frameless window.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont, QLinearGradient
from PySide6.QtWidgets import QWidget, QHBoxLayout

from .. import icons, theme
from .glass import GlassButton, GlassIconButton

_LINKS = [
    ("Models", None),
    ("Workspace", None),
    ("Settings", "settings"),
]


class _LinkButton(QWidget):
    clicked = Signal()

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._text = text
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)

    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(max(60, self.fontMetrics().horizontalAdvance(self._text) + 28), 34)

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
        if self._hover:
            rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
            path = QPainterPath()
            path.addRoundedRect(rect, 10, 10)
            p.fillPath(path, QColor(theme.HOVER))
        p.setPen(QColor(theme.TEXT if self._hover else theme.TEXT_SOFT))
        font = p.font()
        font.setFamily(theme.FONT_FAMILY)
        font.setPointSizeF(9.4)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignCenter, self._text)


class TopBar(QWidget):
    settingsRequested = Signal()
    minimizeRequested = Signal()
    maximizeRequested = Signal()
    closeRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(theme.TITLEBAR_H)
        self.setAttribute(Qt.WA_TranslucentBackground)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 0, 14, 0)
        lay.setSpacing(6)

        # ---- center links ----
        links = QHBoxLayout()
        links.setSpacing(4)
        links.addStretch(1)
        self._link_widgets = []
        for text, key in _LINKS:
            btn = _LinkButton(text)
            if key == "settings":
                btn.clicked.connect(self.settingsRequested.emit)
            else:
                btn.clicked.connect(lambda: None)
            links.addWidget(btn)
            self._link_widgets.append(btn)
        links.addStretch(1)
        lay.addLayout(links, 1)

        # ---- right cluster ----
        self.upgrade_btn = GlassButton("Upgrade", icon_name="zap", icon_size=14,
                                       variant="primary", pill=True)
        self.upgrade_btn.setMinimumHeight(34)
        lay.addWidget(self.upgrade_btn)

        self.bell_btn = GlassIconButton("bell", size=34, icon_size=17,
                                        tooltip="Notifications")
        lay.addWidget(self.bell_btn)

        self.account = _AccountOrb()
        lay.addWidget(self.account)
        lay.addSpacing(6)

        # ---- window controls ----
        lay.addSpacing(8)
        self.min_btn = _WinButton("minus", "Minimize")
        self.min_btn.clicked.connect(self.minimizeRequested.emit)
        lay.addWidget(self.min_btn)
        self.max_btn = _WinButton("maximize", "Maximize")
        self.max_btn.clicked.connect(self.maximizeRequested.emit)
        lay.addWidget(self.max_btn)
        self.close_btn = _WinButton("x", "Close", danger=True)
        self.close_btn.clicked.connect(self.closeRequested.emit)
        lay.addWidget(self.close_btn)

    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.window():
            self._drag_pos = event.globalPosition().toPoint()
            self._window_start = self.window().pos()
            self._dragging = True
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, "_dragging", False) and event.buttons() & Qt.LeftButton:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.window().move(self._window_start + delta)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._dragging = False
        super().mouseReleaseEvent(event)

    def apply_theme(self):
        for btn in self._link_widgets:
            btn.update()
        self.update()


class _AccountOrb(QWidget):
    """Small gradient circle standing in for the account avatar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0.0, QColor(theme.ACCENT))
        grad.setColorAt(1.0, QColor(theme.ACCENT_2))
        p.setBrush(grad)
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1.0))
        p.drawEllipse(rect)
        p.setPen(QColor(theme.ON_ACCENT))
        font = p.font()
        font.setFamily(theme.FONT_FAMILY)
        font.setPointSizeF(11)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignCenter, "A")


class _WinButton(QWidget):
    clicked = Signal()

    def __init__(self, icon_name: str, tooltip: str, danger: bool = False, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._danger = danger
        self._hover = False
        self.setFixedSize(38, 34)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

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
        path.addRoundedRect(rect, 9, 9)
        if self._hover:
            fill = QColor(0, 0, 0) if self._danger else QColor(theme.HOVER)
            fill.setAlpha(150 if self._danger else 255)
            p.fillPath(path, fill)
        color = "#FFFFFF" if (self._hover and self._danger) else theme.TEXT_SOFT
        pm = icons.pixmap(self._icon_name, color, 15)
        p.drawPixmap(int((self.width() - 15) / 2), int((self.height() - 15) / 2), pm)
