"""
AttachmentMenu — a floating glass popup for attachments.

Slides up from the input bar with four premium actions:
    Files / Images / Screenshot / Context

Emits `actionSelected(kind)` and closes. Painted entirely by hand so it
matches the design language (glass card, soft shadow, hover chips).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient, QPainterPath, QFont
from PySide6.QtWidgets import QWidget, QVBoxLayout

from .. import icons
from ..theme import (
    BG_2, BG_3, BG_4, BORDER, TEXT, TEXT_SOFT, TEXT_FAINT,
    FONT_FAMILY, FONT_BODY, FONT_SMALL, FONT_TINY,
)

_ITEMS = [
    ("file", "Files", "Attach documents from your computer"),
    ("image", "Images", "Add a picture to the conversation"),
    ("screenshot", "Screenshot", "Capture what's on your screen"),
    ("context", "Context", "Attach notes, memory or workspace context"),
]


class _MenuItem(QWidget):
    hovered = Signal()
    activated = Signal(str)

    def __init__(self, kind: str, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.title = title
        self.subtitle = subtitle
        self.setFixedHeight(64)
        self._hovered = False

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
            self.activated.emit(self.kind)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(6, 2, self.width() - 12, self.height() - 4)

        if self._hovered:
            path = QPainterPath()
            path.addRoundedRect(rect, 12, 12)
            painter.fillPath(path, QColor(BG_4))

        # icon chip
        chip = QRectF(14, 15, 34, 34)
        chip_path = QPainterPath()
        chip_path.addRoundedRect(chip, 10, 10)
        painter.fillPath(chip_path, QColor(BG_3))
        if self._hovered:
            gradient = QLinearGradient(chip.topLeft(), chip.bottomRight())
            gradient.setColorAt(0.0, QColor(110, 139, 255, 70))
            gradient.setColorAt(1.0, QColor(155, 109, 255, 70))
            painter.fillPath(chip_path, gradient)

        icon_px = icons.pixmap(
            {"file": "file", "image": "image", "screenshot": "monitor",
             "context": "target"}.get(self.kind, "file"),
            "#C3CCDB" if self._hovered else TEXT_SOFT, 17,
        )
        painter.drawPixmap(
            int(chip.center().x() - icon_px.width() / 2),
            int(chip.center().y() - icon_px.height() / 2), icon_px,
        )

        # labels
        painter.setPen(QColor(TEXT))
        font = painter.font()
        font.setFamily(FONT_FAMILY)
        font.setPointSizeF(9.2)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(
            QRectF(58, 12, self.width() - 80, 20), Qt.AlignLeft | Qt.AlignVCenter,
            self.title,
        )
        painter.setPen(QColor(TEXT_FAINT))
        font.setWeight(QFont.Weight.Normal)
        font.setPointSizeF(8.0)
        painter.setFont(font)
        painter.drawText(
            QRectF(58, 32, self.width() - 80, 18), Qt.AlignLeft | Qt.AlignVCenter,
            self.subtitle,
        )

        chevron = icons.pixmap("chevron-right", TEXT_FAINT, 14)
        painter.drawPixmap(
            self.width() - chevron.width() - 20,
            (self.height() - chevron.height()) // 2, chevron,
        )


class AttachmentMenu(QWidget):
    """Frameless popup card with attachment actions."""

    actionSelected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.FramelessWindowHint | Qt.Popup | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self._w = 320
        self._h = len(_ITEMS) * 68 + 28
        self.setFixedSize(self._w, self._h)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(4)

        for kind, title, subtitle in _ITEMS:
            item = _MenuItem(kind, title, subtitle)
            item.activated.connect(self.actionSelected.emit)
            item.activated.connect(self._close)
            layout.addWidget(item)

    def popup(self, anchor_global: "QPoint"):
        """Position the menu above `anchor_global` and slide it in."""
        from PySide6.QtWidgets import QApplication

        x = anchor_global.x() - 20
        screen = QApplication.screenAt(anchor_global)
        if screen is None:
            screen = QApplication.primaryScreen()
        avail = screen.availableGeometry()
        x = max(avail.left(), min(x, avail.right() - self._w))
        y = anchor_global.y() - self._h - 12
        if y < avail.top():
            y = anchor_global.y() + 14

        self.setGeometry(x, y, self._w, self._h)
        self.show()
        self.raise_()

        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(160)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _close(self):
        self.close()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)

        painter.fillPath(path, QColor(BG_2))
        highlight = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        highlight.setColorAt(0.0, QColor(255, 255, 255, 22))
        highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, highlight)

        pen = QPen(QColor(BORDER), 1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
