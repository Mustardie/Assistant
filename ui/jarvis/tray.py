"""Visible system-tray controls for JARVIS background mode."""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


def _status_icon(status: str) -> QIcon:
    colors = {
        "idle": "#67E4EE", "listening": "#63F2B3", "thinking": "#F4C95D",
        "working": "#A98BFF", "speaking": "#63F2B3", "error": "#FF6B7A",
        "paused": "#8A94A6",
    }
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(colors.get(status, colors["idle"])))
    painter.setPen(QColor("#14202A"))
    painter.drawEllipse(4, 4, 24, 24)
    painter.setPen(QColor("#071015"))
    painter.drawText(pixmap.rect(), 0x84, "J")
    painter.end()
    return QIcon(pixmap)


class JarvisTrayController(QObject):
    """Owns the tray icon and keeps background behavior explicit."""

    def __init__(self, window, context_service, parent=None):
        super().__init__(parent or window)
        self.window = window
        self.context_service = context_service
        self.available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        self._quitting = False
        self.icon = None
        if not self.available:
            return

        self.icon = QSystemTrayIcon(_status_icon("idle"), self)
        menu = QMenu()
        show = QAction("Open JARVIS", menu)
        show.triggered.connect(window.show_and_raise)
        self.pause_action = QAction("Pause desktop awareness", menu)
        self.pause_action.triggered.connect(self._toggle_monitoring)
        status = QAction("Desktop awareness status", menu)
        status.triggered.connect(self._show_status)
        quit_action = QAction("Quit JARVIS", menu)
        quit_action.triggered.connect(self.quit)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(self.pause_action)
        menu.addAction(status)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._activated)
        self.icon.setToolTip("JARVIS · idle · desktop awareness off")
        self.icon.show()
        window._jarvis_tray = self
        self.refresh_monitoring()

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.window.show_and_raise()

    def _toggle_monitoring(self) -> None:
        status = self.context_service.status()
        if status.state.value == "running":
            self.context_service.pause()
        elif status.state.value == "paused":
            self.context_service.resume()
        else:
            # Starting observation is opt-in and must go through the visible
            # confirmation surface in the main window.
            self.window.show_and_raise()
            self.window.request_widget("privacy_monitoring")
        self.refresh_monitoring()

    def _show_status(self) -> None:
        self.window.show_and_raise()
        self.window.request_widget("privacy_monitoring")

    def refresh_monitoring(self) -> None:
        if not self.icon:
            return
        state = self.context_service.status().state.value
        label = "Resume desktop awareness" if state == "paused" else "Pause desktop awareness" if state == "running" else "Enable desktop awareness…"
        self.pause_action.setText(label)
        self.icon.setToolTip(f"JARVIS · desktop awareness {state}")

    def set_status(self, status: str) -> None:
        if self.icon:
            normalized = "working" if status == "executing_tool" else status
            self.icon.setIcon(_status_icon(normalized))
            monitoring = self.context_service.status().state.value
            self.icon.setToolTip(f"JARVIS · {normalized} · desktop awareness {monitoring}")

    def quit(self) -> None:
        self._quitting = True
        self.context_service.record_system_lifecycle("shutdown")
        self.context_service.stop(confirm=True, disable=False)
        if self.icon:
            self.icon.hide()
        QApplication.instance().quit()
