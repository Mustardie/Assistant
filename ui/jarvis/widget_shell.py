"""Reusable, reliably interactive glass panel used by every widget."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Property, QRectF, Qt, QTimer, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizeGrip, QVBoxLayout, QWidget

from ui.jarvis.controls import AnimatedIconButton
from ui.jarvis.manager import WidgetManager
from ui.jarvis.models import WidgetState
from ui.jarvis.styles import CYAN, ERROR, FONT, TEXT, TEXT_FAINT, WARNING
from ui.jarvis.widget_contents import WidgetContent


class _DragHeader(QWidget):
    dragStarted = Signal(object)
    dragMoved = Signal(object)
    dragFinished = Signal(object)
    focusRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pressed = False
        self.setCursor(Qt.OpenHandCursor)
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self.setCursor(Qt.ClosedHandCursor)
            self.focusRequested.emit()
            self.dragStarted.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed and event.buttons() & Qt.LeftButton:
            self.dragMoved.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pressed and event.button() == Qt.LeftButton:
            self._pressed = False
            self.setCursor(Qt.OpenHandCursor)
            self.dragFinished.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseReleaseEvent(event)


class WidgetShell(QWidget):
    focused = Signal(str)

    def __init__(self, manager: WidgetManager, state: WidgetState, content: WidgetContent, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.state = state
        self.content = content
        self._drag_origin: QPoint | None = None
        self._widget_origin = QPoint()
        self._applying = False
        self._normal_height = state.height
        self._hover = 0.0
        self._hover_effects_enabled = True
        self.setObjectName("jarvisWidgetShell")
        self.setMinimumSize(*manager.registry.get(state.widget_type).min_size)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self._hover_animation = QPropertyAnimation(self, b"hoverProgress", self)
        self._hover_animation.setDuration(180)
        self._hover_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._normal_geometry = None

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        self.header = _DragHeader()
        self.header.setFixedHeight(40)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(12, 0, 6, 0)
        header_layout.setSpacing(3)
        self.dot = QLabel("●")
        self.dot.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.dot.setStyleSheet(f"color:{CYAN}; background:transparent; font-size:8px;")
        self.title = QLabel(state.title.upper())
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.title.setStyleSheet(f"color:{TEXT}; background:transparent; font:600 10px '{FONT}'; letter-spacing:1px;")
        self.pin_button = AnimatedIconButton("star", size=29, tooltip="Pin widget")
        self.expand_button = AnimatedIconButton("maximize", size=29, tooltip="Expand widget")
        self.collapse_button = AnimatedIconButton("minus", size=29, tooltip="Collapse widget")
        self.close_button = AnimatedIconButton("x", size=29, tooltip="Close widget", danger=True)
        header_layout.addWidget(self.dot)
        header_layout.addWidget(self.title)
        header_layout.addStretch(1)
        header_layout.addWidget(self.pin_button)
        header_layout.addWidget(self.expand_button)
        header_layout.addWidget(self.collapse_button)
        header_layout.addWidget(self.close_button)
        root.addWidget(self.header)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setContentsMargins(11, 6, 11, 6)
        self.banner.hide()
        root.addWidget(self.banner)
        root.addWidget(content, 1)
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(20, 20)
        self.grip.setCursor(Qt.SizeFDiagCursor)

        self.header.focusRequested.connect(self._focus)
        self.header.dragStarted.connect(self._drag_start)
        self.header.dragMoved.connect(self._drag_move)
        self.header.dragFinished.connect(self._drag_finish)
        self.pin_button.clicked.connect(lambda: manager.toggle_pinned(self.state.widget_id))
        self.expand_button.clicked.connect(self._toggle_expanded)
        self.collapse_button.clicked.connect(lambda: manager.toggle_collapsed(self.state.widget_id))
        self.close_button.clicked.connect(lambda: manager.close(self.state.widget_id))
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._persist_size)
        self.apply_state(state)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    hoverProgress = Property(float, _get_hover, _set_hover)

    def _animate_hover(self, target: float):
        if not self._hover_effects_enabled:
            self._set_hover(0.0)
            return
        self._hover_animation.stop()
        self._hover_animation.setStartValue(self._hover)
        self._hover_animation.setEndValue(target)
        self._hover_animation.start()

    def set_hover_effects(self, enabled: bool):
        self._hover_effects_enabled = bool(enabled)
        if not enabled:
            self._hover_animation.stop()
            self._set_hover(0.0)

    def enterEvent(self, event):
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def _focus(self):
        if self.manager.get(self.state.widget_id):
            self.setFocus(Qt.MouseFocusReason)
            self.manager.focus(self.state.widget_id)
            self.focused.emit(self.state.widget_id)

    def _drag_start(self, global_position: QPoint):
        if self.state.expanded:
            return
        self._drag_origin = global_position
        self._widget_origin = self.pos()

    def _drag_move(self, global_position: QPoint):
        if self._drag_origin is None:
            return
        target = self._widget_origin + (global_position - self._drag_origin)
        parent = self.parentWidget()
        if parent and hasattr(parent, "constrain_position"):
            target = parent.constrain_position(self, target)
        self.move(target)

    def _drag_finish(self, _global_position: QPoint):
        if self._drag_origin is None:
            return
        self._drag_origin = None
        if self.manager.get(self.state.widget_id):
            self.manager.move(self.state.widget_id, self.x(), self.y())

    def apply_state(self, state: WidgetState) -> None:
        self._applying = True
        self.state = state
        self.title.setText(state.title.upper())
        self.pin_button.set_active(state.pinned)
        self.expand_button.set_icon("minimize" if state.expanded else "maximize")
        self.collapse_button.set_icon("maximize" if state.collapsed else "minus")
        self.dot.setStyleSheet(f"color:{ERROR if state.error else WARNING if state.loading else CYAN}; background:transparent; font-size:8px;")
        if state.error:
            self.banner.setText(f"ERROR · {state.error}")
            self.banner.setStyleSheet(f"color:{ERROR}; background:rgba(255,80,100,18); border:0;")
            self.banner.show()
        elif state.loading:
            self.banner.setText("WORKING · waiting for verified data")
            self.banner.setStyleSheet(f"color:{WARNING}; background:rgba(255,190,80,18); border:0;")
            self.banner.show()
        elif state.empty:
            self.banner.setText("NO DATA · the request returned an empty result")
            self.banner.setStyleSheet(f"color:{TEXT_FAINT}; background:rgba(255,255,255,6); border:0;")
            self.banner.show()
        else:
            self.banner.hide()
        self.content.setVisible(not state.collapsed)
        self.banner.setVisible(self.banner.isVisible() and not state.collapsed)
        self.grip.setVisible(not state.collapsed)
        if state.collapsed:
            if self.height() > 52:
                self._normal_height = self.height()
            self.setFixedHeight(42)
        else:
            self.setMinimumHeight(self.manager.registry.get(state.widget_type).min_size[1])
            self.setMaximumHeight(16777215)
            self.setGeometry(state.x, state.y, state.width, max(state.height, self._normal_height))
        self.content.apply_state(state)
        self.setVisible(not state.minimized)
        self._applying = False
        self.update()

    def _toggle_expanded(self):
        parent = self.parentWidget()
        if parent is None:
            return
        if not self.state.expanded:
            self._normal_geometry = self.geometry()
            self.manager.update(self.state.widget_id, expanded=True, collapsed=False)
            top = getattr(parent, "TOP_SAFE_AREA", 84)
            bottom = getattr(parent, "BOTTOM_SAFE_AREA", 78)
            self.setGeometry(16, top, max(320, parent.width() - 32), max(220, parent.height() - top - bottom))
            self.raise_()
        else:
            normal = self._normal_geometry
            self.manager.update(self.state.widget_id, expanded=False)
            if normal is not None:
                self.setGeometry(normal)
                self.manager.update(
                    self.state.widget_id,
                    x=normal.x(), y=normal.y(), width=normal.width(), height=normal.height(),
                )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._focus()
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        self.grip.move(self.width() - self.grip.width() - 3, self.height() - self.grip.height() - 3)
        self.grip.raise_()
        if not self._applying and not self.state.collapsed and not self.state.expanded:
            self._resize_timer.start(180)
        super().resizeEvent(event)

    def _persist_size(self):
        if self.manager.get(self.state.widget_id) and not self.state.collapsed and not self.state.expanded:
            self.manager.resize(self.state.widget_id, self.width(), self.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), 12, 12)
        painter.fillPath(path, QColor(8, 24, 37, 232))
        border_alpha = int(72 + self._hover * 105 + (45 if self.hasFocus() else 0))
        painter.setPen(QPen(QColor(104, 225, 238, min(230, border_alpha)), 1.0 + self._hover * 0.5))
        painter.drawPath(path)
        if self._hover > 0.03:
            painter.setPen(QPen(QColor(103, 228, 238, int(30 * self._hover)), 4))
            painter.drawPath(path)
        painter.setPen(QPen(QColor(103, 228, 238, 28 + int(35 * self._hover)), 1))
        painter.drawLine(12, 40, self.width() - 12, 40)
