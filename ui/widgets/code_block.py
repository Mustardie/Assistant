"""
CodeBlock — a dark, bordered code card with a language chip and a copy
button, matching the redesign's syntax-card styling.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea

from .. import icons, theme
from .glass import GlassIconButton


class CodeBlock(QWidget):
    def __init__(self, code: str, lang: str = "", parent=None):
        super().__init__(parent)
        self._code = code.rstrip()
        self._lang = lang or "text"
        self._copied = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # header: language chip + copy button
        header = QWidget(self)
        header.setFixedHeight(36)
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(14, 0, 8, 0)
        chip = QLabel(self._lang.upper())
        chip.setStyleSheet(theme.text_qss(size=10, weight=600,
                                          color=theme.TEXT_FAINT, spacing=1.1))
        hlay.addWidget(chip)
        hlay.addStretch(1)
        self.copy_btn = GlassIconButton(
            "copy", size=26, icon_size=13, tooltip="Copy code", parent=header
        )
        self.copy_btn.clicked.connect(self._copy)
        hlay.addWidget(self.copy_btn)
        lay.addWidget(header)

        # body: selectable monospace text in a scroll area
        self._text = QLabel(self._code)
        self._text.setStyleSheet(
            f"color: {theme.TEXT}; background: transparent;"
            f"font-family: {theme.FONT_CODE}; font-size: 12.5px;"
        )
        self._text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._text.setWordWrap(False)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(theme.scroll_qss())
        self._scroll.setWidget(self._text)
        self._scroll.setMaximumHeight(300)
        lay.addWidget(self._scroll, 1)

    def _copy(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._code)
        self._copied = True
        self.copy_btn.setToolTip("Copied!")
        self.copy_btn.setProperty("icon_name", "check")
        self.copy_btn.update()
        self.copy_btn._icon_name = "check"
        self.copy_btn._color = theme.ACCENT
        self.copy_btn.update()
        QTimer.singleShot(1400, self._reset_copy)

    def _reset_copy(self):
        self._copied = False
        self.copy_btn._icon_name = "copy"
        self.copy_btn._color = None
        self.copy_btn.setToolTip("Copy code")
        self.copy_btn.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        p.fillPath(path, QColor(theme.CODE_BG))
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1.0))
        p.drawPath(path)

        # subtle top divider under the header
        p.setPen(QPen(QColor(theme.BORDER), 1.0))
        p.drawLine(0, 36, self.width(), 36)

    def apply_theme(self):
        chip = self.findChild(QLabel)
        if chip is not None:
            chip.setStyleSheet(theme.text_qss(size=10, weight=600,
                                              color=theme.TEXT_FAINT, spacing=1.1))
        self._text.setStyleSheet(
            f"color: {theme.TEXT}; background: transparent;"
            f"font-family: {theme.FONT_CODE}; font-size: 12.5px;"
        )
        self._scroll.setStyleSheet(theme.scroll_qss())
        self.update()
