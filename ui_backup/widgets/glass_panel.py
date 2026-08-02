"""
GlassPanel — a single translucent "liquid glass" surface.

Reproduces (from Figma node 6:65):
    border-radius   -> corner_radius   (animatable)
    background      -> rgba(255,255,255, bg_alpha)  (animatable)
    border          -> 0.5px rgba(255,255,255,0.05) hairline
    box-shadow      -> inset dark highlight (top) + inset light highlight (bottom)
    backdrop-filter -> blur(5px), approximated via a blurred screen-snapshot
                       (Qt has no live backdrop blur, see note in nova_window.py)

corner_radius and bg_alpha are exposed as Qt Properties so QPropertyAnimation
can smoothly morph one panel "shape" into another (bubble -> input bar, etc).
"""
from PySide6.QtCore import Qt, QRectF, QPoint, Property, Signal
from PySide6.QtGui import (
    QPainter, QPainterPath, QLinearGradient, QColor, QPen, QPixmap, QGuiApplication
)
from PySide6.QtWidgets import (
    QWidget, QGraphicsDropShadowEffect, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsBlurEffect,
)


class GlassPanel(QWidget):

    clicked = Signal()

    def __init__(self, parent=None, corner_radius=33.0, bg_alpha=0.02,
                 border_alpha=0.05, enable_backdrop_blur=True):
        super().__init__(parent)
        self._corner_radius = float(corner_radius)
        self._bg_alpha = float(bg_alpha)
        self._border_alpha = float(border_alpha)
        self._backdrop_pixmap = None
        self.enable_backdrop_blur = enable_backdrop_blur

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)

        # Outer drop shadow so the panel reads as floating above the desktop.
        # (This is separate from the *inset* shadows in the Figma spec, which
        # are drawn by hand in paintEvent below.)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 130))
        self.setGraphicsEffect(shadow)

    # ---------------- animatable properties ----------------
    def _get_corner_radius(self):
        return self._corner_radius

    def _set_corner_radius(self, value):
        self._corner_radius = float(value)
        self.update()

    cornerRadius = Property(float, _get_corner_radius, _set_corner_radius)

    def _get_bg_alpha(self):
        return self._bg_alpha

    def _set_bg_alpha(self, value):
        self._bg_alpha = float(value)
        self.update()

    bgAlpha = Property(float, _get_bg_alpha, _set_bg_alpha)

    # ---------------- backdrop blur (approximated) ----------------
    def refresh_backdrop(self):
        """Grab whatever is currently behind this panel and blur it, to
        approximate `backdrop-filter: blur(5px)`. Call this whenever the
        panel is about to become visible or has moved/resized."""
        if not self.enable_backdrop_blur or self.width() <= 0 or self.height() <= 0:
            self._backdrop_pixmap = None
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self._backdrop_pixmap = None
            return
        top_left = self.mapToGlobal(QPoint(0, 0))
        try:
            snapshot = screen.grabWindow(0, top_left.x(), top_left.y(),
                                          self.width(), self.height())
        except Exception:
            self._backdrop_pixmap = None
            return
        self._backdrop_pixmap = self._blur_pixmap(snapshot, radius=5)
        self.update()

    @staticmethod
    def _blur_pixmap(pixmap: QPixmap, radius: float) -> QPixmap:
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(pixmap)
        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(radius)
        item.setGraphicsEffect(blur)
        scene.addItem(item)

        result = QPixmap(pixmap.size())
        result.fill(Qt.transparent)
        painter = QPainter(result)
        scene.render(painter, QRectF(0, 0, pixmap.width(), pixmap.height()),
                     QRectF(0, 0, pixmap.width(), pixmap.height()))
        painter.end()
        return result

    # ---------------- painting ----------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(0.25, 0.25, self.width() - 0.5, self.height() - 0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self._corner_radius, self._corner_radius)

        painter.save()
        painter.setClipPath(path)

        if self.enable_backdrop_blur and self._backdrop_pixmap is not None:
            painter.drawPixmap(0, 0, self._backdrop_pixmap)

        # glass fill
        painter.fillRect(self.rect(), QColor(255, 255, 255, int(255 * self._bg_alpha)))

        # inset dark highlight along the top edge (box-shadow inset 0 -2px 4px rgba(0,0,0,.2))
        top_shadow = QLinearGradient(0, 0, 0, self.height() * 0.35)
        top_shadow.setColorAt(0.0, QColor(0, 0, 0, int(255 * 0.20)))
        top_shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(self.rect(), top_shadow)

        # inset light highlight along the bottom edge (box-shadow inset 0 2px 4px rgba(255,255,255,.4))
        bottom_highlight = QLinearGradient(0, self.height() * 0.55, 0, self.height())
        bottom_highlight.setColorAt(0.0, QColor(255, 255, 255, 0))
        bottom_highlight.setColorAt(1.0, QColor(255, 255, 255, int(255 * 0.40)))
        painter.fillRect(self.rect(), bottom_highlight)

        painter.restore()

        # hairline border
        pen = QPen(QColor(255, 255, 255, int(255 * self._border_alpha)))
        pen.setWidthF(0.5)
        painter.setPen(pen)
        painter.drawPath(path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_backdrop()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
