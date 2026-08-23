"""Animated, app-contained controls for the JARVIS interface."""

from __future__ import annotations

from PySide6.QtCore import Property, QRectF, Qt, QPropertyAnimation, QEasingCurve, Signal, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QGridLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui import icons
from ui.jarvis.models import WidgetSpec
from ui.jarvis.styles import CYAN, CYAN_BRIGHT, ERROR, FONT, TEXT, TEXT_FAINT, TEXT_SOFT


class AnimatedIconButton(QAbstractButton):
    """Painted icon button with smooth hover, press, and active feedback."""

    def __init__(self, icon_name: str, *, size: int = 42, tooltip: str = "", danger: bool = False, parent=None):
        super().__init__(parent)
        self.icon_name = icon_name
        self.danger = danger
        self._hover = 0.0
        self._active = False
        self._animation = QPropertyAnimation(self, b"hoverProgress", self)
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self.setFixedSize(QSize(size, size))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        if tooltip:
            self.setToolTip(tooltip)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    hoverProgress = Property(float, _get_hover, _set_hover)

    def set_active(self, active: bool):
        self._active = bool(active)
        self.update()

    def set_icon(self, icon_name: str):
        self.icon_name = icon_name
        self.update()

    def _animate(self, target: float):
        self._animation.stop()
        self._animation.setStartValue(self._hover)
        self._animation.setEndValue(target)
        self._animation.start()

    def enterEvent(self, event):
        self._animate(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)
        accent = QColor(ERROR if self.danger else CYAN)
        alpha = int(10 + self._hover * 38 + (42 if self._active else 0))
        fill = QColor(accent)
        fill.setAlpha(alpha)
        border = QColor(accent)
        border.setAlpha(int(45 + self._hover * 125 + (55 if self._active else 0)))
        painter.setBrush(fill)
        painter.setPen(QPen(border, 1.0 + self._hover * 0.5))
        painter.drawRoundedRect(rect, 10, 10)
        if self._hover > 0.05 or self._active:
            glow = QColor(accent)
            glow.setAlpha(int(20 * max(self._hover, 0.5 if self._active else 0)))
            painter.setPen(QPen(glow, 3.0))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 8, 8)
        color = ERROR if self.danger else (CYAN_BRIGHT if self._hover > 0.4 or self._active else TEXT_SOFT)
        icon_size = max(14, int(self.width() * (0.42 + self._hover * 0.04)))
        pixmap = icons.pixmap(self.icon_name, color, icon_size)
        painter.drawPixmap((self.width() - icon_size) // 2, (self.height() - icon_size) // 2, pixmap)


_WIDGET_ICONS = {
    "chat": "chat", "task_progress": "target", "confirmation": "check", "system_status": "monitor",
    "weather": "globe", "video_player": "play", "file_search": "search", "memory_recall": "book",
    "calendar": "clock", "reminders": "check", "notes": "file", "web_results": "globe",
    "audio_player": "volume", "app_launcher": "menu", "clipboard": "copy", "transfers": "download",
    "notifications": "info", "email": "send", "messaging": "chat", "connectors": "wifi",
    "tool_inspector": "sliders", "plan_inspector": "target", "automation": "refresh", "skills": "zap",
    "voice_transcript": "mic", "command_palette": "search", "system_monitor": "monitor", "code_task": "terminal",
    "terminal": "terminal", "media_review": "play", "study": "book", "quick_answer": "sparkle-solid",
    "error_debug": "info", "settings": "gear", "activity": "clock",
}


class _PaletteItem(QAbstractButton):
    def __init__(self, spec: WidgetSpec, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._hovered = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(64)
        self.setToolTip(spec.purpose)

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        fill = QColor(103, 228, 238, 24 if self._hovered else 8)
        border = QColor(103, 228, 238, 130 if self._hovered else 35)
        painter.setBrush(fill)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(rect, 9, 9)
        icon = icons.pixmap(_WIDGET_ICONS.get(self.spec.widget_type, "panel-right"), CYAN if self._hovered else TEXT_SOFT, 19)
        painter.drawPixmap(13, 13, icon)
        painter.setPen(QColor(TEXT))
        font = painter.font()
        font.setFamily(FONT)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(QRectF(43, 8, self.width() - 52, 22), Qt.AlignVCenter, self.spec.title)
        painter.setPen(QColor(TEXT_FAINT))
        font.setPixelSize(9)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.drawText(QRectF(43, 28, self.width() - 52, 28), Qt.TextWordWrap, self.spec.purpose)


class WidgetPalette(QWidget):
    """Searchable widget catalog that never leaves the JARVIS window."""

    widgetSelected = Signal(str)
    closeRequested = Signal()

    def __init__(self, specs: list[WidgetSpec], parent=None):
        super().__init__(parent)
        self.setObjectName("jarvisWidgetPalette")
        self.setStyleSheet("background: transparent;")
        self._items: list[_PaletteItem] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("WIDGET MATRIX")
        title.setStyleSheet(f"color:{TEXT}; font:600 12px '{FONT}'; letter-spacing:2px; background:transparent;")
        close = AnimatedIconButton("x", size=34, tooltip="Close widget matrix")
        close.clicked.connect(self.closeRequested)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close)
        root.addLayout(header)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search capabilities…")
        self.search.setStyleSheet(
            f"QLineEdit {{ color:{TEXT}; background:rgba(2,10,16,180); border:1px solid rgba(103,228,238,60); border-radius:9px; padding:10px; }}"
            f"QLineEdit:focus {{ border-color:{CYAN}; }}"
        )
        self.search.textChanged.connect(self._filter)
        root.addWidget(self.search)
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        self.grid = QGridLayout(body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        for index, spec in enumerate(specs):
            item = _PaletteItem(spec)
            item.clicked.connect(lambda checked=False, widget_type=spec.widget_type: self._choose(widget_type))
            self._items.append(item)
            self.grid.addWidget(item, index // 2, index % 2)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:0; } QScrollArea > QWidget > QWidget { background:transparent; }"
            "QScrollBar:vertical { background:transparent; width:6px; } QScrollBar::handle:vertical { background:rgba(103,228,238,65); border-radius:3px; min-height:30px; }"
        )
        root.addWidget(scroll, 1)

    def _choose(self, widget_type: str):
        self.widgetSelected.emit(widget_type)

    def _filter(self, query: str):
        query = query.strip().lower()
        for item in self._items:
            haystack = f"{item.spec.title} {item.spec.purpose} {item.spec.widget_type}".lower()
            item.setVisible(not query or query in haystack)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        painter.fillPath(path, QColor(5, 17, 27, 248))
        painter.setPen(QPen(QColor(103, 228, 238, 105), 1))
        painter.drawPath(path)
