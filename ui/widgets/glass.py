"""
Glass control kit — the Material-3 "Nova" building blocks.

    GlassButton      — pill/rounded buttons (primary, ghost, outline)
    GlassIconButton  — compact square icon control with hover glow
    GlassLineEdit    — rounded text field with focus ring
    GlassCombo       — rounded select with painted chevron + styled popup
    GlassToggle      — animated Material switch
    GlassSlider      — slim custom slider with round thumb
    GlassCard        — frosted rounded surface with optional header

All colors are read from ui.theme at paint time, so a live theme change
only needs a repaint (and the combo popup QSS is re-applied via
apply_theme()).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal, Property, QPropertyAnimation, QEasingCurve, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont, QLinearGradient
from PySide6.QtWidgets import (
    QWidget, QPushButton, QLineEdit, QComboBox, QLabel, QHBoxLayout, QVBoxLayout,
)

from .. import icons, theme
from .nova_orb import NovaOrb  # noqa: F401  (re-exported for convenience)


class _HoverableMixin:
    def _init_hover(self):
        self._hover = 0.0
        self._press = 0.0
        self._anim = None

    def _anim_to(self, prop, target):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(160)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        return anim

    def enterEvent(self, event):
        self._hover_target = 1.0
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_target = 0.0
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._anim_to(b"press", 1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._anim_to(b"press", 0.0)
        super().mouseReleaseEvent(event)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    def _get_press(self):
        return self._press

    def _set_press(self, v):
        self._press = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)
    press = Property(float, _get_press, _set_press)


class GlassButton(QPushButton):
    """A polished button. Variants:
        primary — filled with the accent color, dark text
        ghost   — translucent surface with border
        subtle  — no border, fills on hover (nav-style)
    """

    def __init__(self, text: str = "", *, icon_name: str | None = None,
                 icon_size: int = 16, variant: str = "ghost",
                 pill: bool = False, radius: int = 10, parent=None):
        super().__init__(text, parent)
        self._variant = variant
        self._pill = pill
        self._radius = radius
        self._icon_name = icon_name
        self._icon_size = icon_size
        self._hover = 0.0
        self._press = 0.0
        self._anim = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumHeight(38)

    # -- animation props -------------------------------------------------- #
    def _anim_to(self, prop, target, duration=160):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(duration)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        return anim

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._anim_to(b"press", 1.0, 90)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._anim_to(b"press", 0.0, 200)
        super().mouseReleaseEvent(event)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    def _get_press(self):
        return self._press

    def _set_press(self, v):
        self._press = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)
    press = Property(float, _get_press, _set_press)

    # -- painting ---------------------------------------------------------- #
    def sizeHint(self):
        text_w = self.fontMetrics().horizontalAdvance(self.text())
        w = text_w + 44
        if self._icon_name:
            w += self._icon_size + 8
        return self.minimumSize().expandedTo(
            __import__("PySide6.QtCore", fromlist=["QSize"]).QSize(w, self.minimumHeight())
        )

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._radius if not self._pill else min(self.height(), self.width()) / 2.0
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(rect, r, r)

        variant = self._variant
        if variant == "primary":
            base = QColor(theme.ACCENT)
            if self._hover > 0:
                base = base.lighter(int(100 + 10 * self._hover))
            if self._press > 0:
                base = base.darker(int(100 + 12 * self._press))
            p.fillPath(path, base)
            text_color = QColor(theme.ON_ACCENT)
            border = QColor(0, 0, 0, 0)
        elif variant == "ghost":
            p.fillPath(path, QColor(theme.GLASS))
            if self._hover > 0:
                p.fillPath(path, QColor(theme.HOVER))
            if self._press > 0:
                p.fillPath(path, QColor(theme.HOVER_STRONG))
            border = QColor(theme.BORDER_STRONG if self._hover > 0 else theme.BORDER)
            text_color = QColor(theme.TEXT)
        else:  # subtle
            if self._hover > 0:
                p.fillPath(path, QColor(theme.HOVER))
            if self._press > 0:
                p.fillPath(path, QColor(theme.HOVER_STRONG))
            border = QColor(0, 0, 0, 0)
            text_color = QColor(theme.TEXT)

        if border.alpha() > 0:
            p.setPen(QPen(border, 1.0))
            p.drawPath(path)

        text_w = self.fontMetrics().horizontalAdvance(self.text())
        icon_x = None
        if self._icon_name:
            pm = icons.pixmap(self._icon_name, text_color.name(), self._icon_size)
            if self.text():
                total = text_w + 8 + pm.width()
                icon_x = rect.center().x() - total / 2.0
            else:
                icon_x = rect.center().x() - pm.width() / 2.0
            p.drawPixmap(int(icon_x), int(rect.center().y() - pm.height() / 2.0), pm)

        if self.text():
            p.setPen(text_color)
            font = p.font()
            font.setFamily(theme.FONT_FAMILY)
            font.setPointSizeF(9.6)
            font.setWeight(QFont.Weight.Medium)
            p.setFont(font)
            if icon_x is not None:
                text_rect = QRectF(icon_x + self._icon_size + 8, 1,
                                   self.width() - (icon_x + self._icon_size + 8), self.height() - 2)
                p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self.text())
            else:
                p.drawText(QRectF(0, 0, self.width(), self.height()).adjusted(0, 1, 0, 0),
                           Qt.AlignCenter, self.text())

        if not self.isEnabled():
            p.fillPath(path, QColor(0, 0, 0, 60))


class GlassIconButton(QWidget):
    """Compact square icon button with hover lift + tooltip."""

    clicked = Signal()

    def __init__(self, icon_name: str, size: int = 34, *, icon_size: int | None = None,
                 tooltip: str = "", color: str | None = None, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon_size = icon_size or int(size * 0.52)
        self._size = size
        self._tooltip = tooltip
        self._color = color
        self._hover = 0.0
        self._press = 0.0
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        if tooltip:
            self.setToolTip(tooltip)

    def _anim_to(self, prop, target, duration=150):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(duration)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        return anim

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._anim_to(b"press", 1.0, 90)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._anim_to(b"press", 0.0, 200)
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
        super().mouseReleaseEvent(event)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    def _get_press(self):
        return self._press

    def _set_press(self, v):
        self._press = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)
    press = Property(float, _get_press, _set_press)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        p.fillPath(path, QColor(theme.GLASS))
        if self._hover > 0:
            p.fillPath(path, QColor(theme.HOVER))
        if self._press > 0:
            p.fillPath(path, QColor(theme.HOVER_STRONG))
        color = QColor(self._color or theme.TEXT_SOFT)
        if self._hover > 0:
            color = QColor(theme.TEXT)
        pm = icons.pixmap(self._icon_name, color.name(), self._icon_size)
        p.drawPixmap(int((self.width() - pm.width()) / 2),
                     int((self.height() - pm.height()) / 2), pm)


class GlassLineEdit(QWidget):
    """Rounded text field: painted frame + focus ring, native QLineEdit text."""

    changed = Signal(str)
    submitted = Signal(str)

    def __init__(self, placeholder: str = "", *, default_text: str = "",
                 max_width: int | None = None, parent=None):
        super().__init__(parent)
        self._hover = 0.0
        self._focus = 0.0
        self._max_width = max_width

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(0)
        self.editor = QLineEdit(default_text, self)
        self.editor.setPlaceholderText(placeholder)
        self.editor.setStyleSheet(theme.edit_qss())
        self.editor.textChanged.connect(self.changed.emit)
        self.editor.returnPressed.connect(self.submitted.emit)
        lay.addWidget(self.editor)

        self.setMinimumHeight(42)
        if max_width:
            self.setMaximumWidth(max_width)
        self.setFocusProxy(self.editor)

    def setText(self, text: str):
        self.editor.setText(text)

    def text(self) -> str:
        return self.editor.text()

    def setEnabled(self, enabled: bool):
        super().setEnabled(enabled)
        self.editor.setEnabled(enabled)

    # -- painting ---------------------------------------------------------- #
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)

        fill = QColor(theme.GLASS)
        p.fillPath(path, fill)

        border = QColor(theme.BORDER_STRONG)
        if self._hover > 0:
            p.fillPath(path, QColor(theme.HOVER))
            border = QColor(theme.BORDER_STRONG)
        if self._focus > 0:
            border = QColor(theme.BORDER_FOCUS)
        pen = QPen(border, 1.2)
        p.setPen(pen)
        p.drawPath(path)

    def _anim_to(self, prop, target, duration=160):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(duration)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def focusInEvent(self, event):
        self._anim_to(b"focus", 1.0)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._anim_to(b"focus", 0.0)
        super().focusOutEvent(event)

    def _get_focus(self):
        return self._focus

    def _set_focus(self, v):
        self._focus = float(v)
        self.update()

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)
    focus = Property(float, _get_focus, _set_focus)

    def apply_theme(self):
        self.editor.setStyleSheet(theme.edit_qss())


class GlassCombo(QWidget):
    """Rounded select with painted frame + chevron, styled popup list."""

    changed = Signal(int)

    def __init__(self, items: list[str], parent=None):
        super().__init__(parent)
        self._hover = 0.0
        self._focus = 0.0
        self.setMinimumHeight(42)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 0, 38, 0)
        lay.setSpacing(0)
        self.combo = QComboBox(self)
        self.combo.addItems(items)
        self.combo.setStyleSheet(theme.combo_qss())
        self.combo.currentIndexChanged.connect(self.changed.emit)
        lay.addWidget(self.combo)
        self.setFocusProxy(self.combo)

    def currentIndex(self) -> int:
        return self.combo.currentIndex()

    def currentText(self) -> str:
        return self.combo.currentText()

    def currentData(self):
        return self.combo.currentData()

    def setCurrentIndex(self, idx: int):
        self.combo.setCurrentIndex(idx)

    def setCurrentText(self, text: str):
        idx = self.combo.findText(text)
        if idx >= 0:
            self.combo.setCurrentIndex(idx)

    def setCurrentData(self, data):
        idx = self.combo.findData(data)
        if idx >= 0:
            self.combo.setCurrentIndex(idx)

    def count(self) -> int:
        return self.combo.count()

    def itemData(self, idx: int):
        return self.combo.itemData(idx)

    def addItems(self, items: list[str]):
        self.combo.addItems(items)

    def addItem(self, label: str, data=None):
        self.combo.addItem(label, data)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        p.fillPath(path, QColor(theme.GLASS))
        if self._hover > 0:
            p.fillPath(path, QColor(theme.HOVER))
        border = QColor(theme.BORDER_STRONG)
        if self._focus > 0:
            border = QColor(theme.BORDER_FOCUS)
        p.setPen(QPen(border, 1.2))
        p.drawPath(path)

        # chevron
        pm = icons.pixmap("chevron-down", theme.TEXT_FAINT, 15)
        p.drawPixmap(self.width() - 30, int((self.height() - pm.height()) / 2), pm)

    def _anim_to(self, prop, target, duration=160):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(duration)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def focusInEvent(self, event):
        self._anim_to(b"focus", 1.0)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._anim_to(b"focus", 0.0)
        super().focusOutEvent(event)

    def _get_focus(self):
        return self._focus

    def _set_focus(self, v):
        self._focus = float(v)
        self.update()

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)
    focus = Property(float, _get_focus, _set_focus)

    def apply_theme(self):
        self.combo.setStyleSheet(theme.combo_qss())
        self.update()


class GlassToggle(QWidget):
    """An animated Material-style switch."""

    toggled = Signal(bool)

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedSize(42, 24)
        self._checked = checked
        self._progress = 1.0 if checked else 0.0
        self._anim = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _get_progress(self):
        return self._progress

    def _set_progress(self, v):
        self._progress = float(v)
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool):
        if checked == self._checked:
            return
        self._checked = checked
        anim = QPropertyAnimation(self, b"progress", self)
        anim.setDuration(180)
        anim.setStartValue(self._progress)
        anim.setEndValue(1.0 if checked else 0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
            self.toggled.emit(self._checked)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        track = QRectF(0.5, 0.5, self.width() - 1, h - 1)
        tpath = QPainterPath()
        tpath.addRoundedRect(track, h / 2, h / 2)

        # color morph between off (surface-high + border) and on (accent)
        off = QColor(theme.BG_4)
        on = QColor(theme.ACCENT)
        col = QColor(
            int(off.red() + (on.red() - off.red()) * self._progress),
            int(off.green() + (on.green() - off.green()) * self._progress),
            int(off.blue() + (on.blue() - off.blue()) * self._progress),
        )
        p.fillPath(tpath, col)

        # thumb
        r = (h - 6) / 2.0
        cx = r + 3 + self._progress * (self.width() - (r + 3) * 2)
        cy = h / 2.0
        p.setBrush(QColor(255, 255, 255))
        p.setPen(QPen(QColor(0, 0, 0, 30), 0.6))
        p.drawEllipse(QPointF(cx, cy), r, r)


class GlassSlider(QWidget):
    """Slim slider: rounded track, accent fill, round thumb with hover scale."""

    changed = Signal(float)

    def __init__(self, minimum: float = 0.0, maximum: float = 1.0,
                 step: float = 0.01, value: float = 1.0, parent=None):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._step = step
        self._value = value
        self._hover = 0.0
        self._dragging = False
        self.setFixedHeight(22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def value(self) -> float:
        return self._value

    def setValue(self, value: float):
        self._value = max(self._min, min(self._max, float(value)))
        self.update()

    def _x_to_value(self, x: float) -> float:
        frac = max(0.0, min(1.0, (x - 12) / max(1, self.width() - 24)))
        raw = self._min + frac * (self._max - self._min)
        steps = round((raw - self._min) / self._step)
        return self._min + steps * self._step

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self.setValue(self._x_to_value(event.position().x()))
            self.changed.emit(self._value)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.setValue(self._x_to_value(event.position().x()))
            self.changed.emit(self._value)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._dragging = False
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def _anim_to(self, prop, target, duration=150):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(duration)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cy = self.height() / 2.0
        track_w = self.width() - 24
        x0 = 12.0
        frac = (self._value - self._min) / (self._max - self._min)

        track = QRectF(x0, cy - 3, track_w, 6)
        tpath = QPainterPath()
        tpath.addRoundedRect(track, 3, 3)
        p.fillPath(tpath, QColor(theme.BG_4))

        fill = QRectF(x0, cy - 3, max(6, track_w * frac), 6)
        fpath = QPainterPath()
        fpath.addRoundedRect(fill, 3, 3)
        p.fillPath(fpath, QColor(theme.ACCENT))

        thumb_r = 7.0 + 2.5 * self._hover
        tx = x0 + track_w * frac
        p.setBrush(QColor(theme.TEXT))
        p.setPen(QPen(QColor(theme.ACCENT), 1.5))
        p.drawEllipse(QPointF(tx, cy), thumb_r, thumb_r)


class GlassCard(QWidget):
    """Frosted rounded card: subtle surface fill + hairline border.

    Child widgets are added to `.body` (a QVBoxLayout).
    """

    def __init__(self, parent=None, *, padding: int = 20, radius: int = 16):
        super().__init__(parent)
        self._radius = radius
        lay = QVBoxLayout(self)
        lay.setContentsMargins(padding, 18, padding, padding)
        lay.setSpacing(12)
        self.body = lay
        self.setAttribute(Qt.WA_TranslucentBackground)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        p.fillPath(path, QColor(theme.SURFACE_GLASS))
        p.setPen(QPen(QColor(theme.BORDER), 1.0))
        p.drawPath(path)


class SectionHeader(QLabel):
    """Small uppercase section label used above setting groups."""

    def __init__(self, text: str, parent=None):
        super().__init__(text.upper(), parent)
        self.setStyleSheet(theme.text_qss(size=11, weight=600,
                                          color=theme.TEXT_FAINT, spacing=1.2))

    def apply_theme(self):
        self.setStyleSheet(theme.text_qss(size=11, weight=600,
                                          color=theme.TEXT_FAINT, spacing=1.2))


def divider() -> QWidget:
    line = QWidget()
    line._is_divider = True
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {theme.BORDER};")

    def _apply():
        line.setStyleSheet(f"background: {theme.BORDER};")

    line.apply_theme = _apply
    return line


def label(text: str, *, size: float = theme.FONT_BODY, weight: int = 400,
          color: str | None = None, wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    lab._text_style = (size, weight, color)

    def _apply():
        lab.setStyleSheet(theme.text_qss(size=size, weight=weight, color=color))

    lab.apply_theme = _apply
    _apply()
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if wrap:
        lab.setWordWrap(True)
    return lab
