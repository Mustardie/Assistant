"""
ConnectionsPage — the integrations view of the redesign.

Lists every registered adapter with its live status (connected /
requires_auth / not_configured / unavailable), the authentication type
and the capabilities it exposes. Each row has a Connect / Disconnect
action that talks to the ConnectionManager.

Connect/disconnect run off the UI thread — OAuth flows wait on a local
callback server, so a blocking call here would freeze the window.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QRectF, Property, QPropertyAnimation, QEasingCurve, Signal, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
    QInputDialog, QLineEdit, QSizePolicy,
)

from .. import icons, theme
from .glass import GlassButton, GlassIconButton, label, SectionHeader

# name -> (icon name, tint color)
_BRAND_ICONS = {
    "google": ("globe", theme.ACCENT),
    "microsoft": ("keyboard", theme.ACCENT_2),
    "spotify": ("volume", theme.STATUS_SPEAKING),
    "apple_music": ("music", theme.STATUS_SPEAKING),
    "slack": ("chat", theme.ACCENT_2),
    "discord": ("chat", theme.ACCENT),
    "telegram": ("send", theme.ACCENT),
    "whatsapp": ("chat", theme.STATUS_SPEAKING),
    "notion": ("book-open", theme.TEXT_SOFT),
    "todoist": ("check", theme.ACCENT),
    "vscode": ("code", theme.ACCENT_2),
    "filesystem": ("folder", theme.TERTIARY),
    "windows": ("monitor", theme.ACCENT),
}

_STATUS_STYLE = {
    "connected": ("Connected", theme.STATUS_SPEAKING),
    "connecting": ("Connecting…", theme.ACCENT),
    "requires_auth": ("Needs authorization", theme.ACCENT),
    "not_configured": ("Not configured", theme.TEXT_FAINT),
    "unavailable": ("Unavailable", theme.STATUS_ERROR),
    "disconnected": ("Disconnected", theme.TEXT_FAINT),
    "unknown": ("Unknown", theme.TEXT_FAINT),
}

_AUTH_LABELS = {
    "oauth": "OAuth",
    "api_key": "API key",
    "local": "Local app",
    "none": "Built-in",
}


class _ElideLabel(QLabel):
    """A QLabel that elides its text on the right when space is tight.
    (QLabel.setTextElideMode requires a newer Qt than this build ships.)"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full = text

    def setFullText(self, text: str):
        self._full = str(text)
        self._apply_elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self):
        width = self.width() - 2
        if width <= 0:
            return
        elided = self.fontMetrics().elidedText(
            self._full, Qt.ElideRight, width)
        if elided != self.text():
            self.setText(elided)


class _AdapterRow(QWidget):
    """A single integration row: icon, name, status pill, capabilities,
    a connect/disconnect action, and a click-to-expand description."""

    actionRequested = Signal(str, bool)  # (adapter name, connect?)

    def __init__(self, status: dict, parent=None):
        super().__init__(parent)
        self.name = status.get("name", "")
        self._status = status
        self._hover = 0.0
        self._expanded = False
        self.setMinimumHeight(76)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(16, 12, 14, 12)
        header.setSpacing(12)

        icon_name, icon_color = _BRAND_ICONS.get(
            self.name, ("data", theme.ACCENT))
        icon_box = QWidget()
        icon_box.setFixedSize(40, 40)
        icon_box.setStyleSheet(
            f"background: {theme.rgba(icon_color, 40)}; border-radius: 11px;")
        ib_lay = QVBoxLayout(icon_box)
        ib_lay.setContentsMargins(0, 0, 0, 0)
        ic = QLabel()
        ic.setAlignment(Qt.AlignCenter)
        ic.setPixmap(icons.pixmap(icon_name, icon_color, 19))
        ib_lay.addWidget(ic)
        header.addWidget(icon_box)
        self._icon_box = icon_box
        self._icon_lab = ic
        self._icon_name = icon_name
        self._icon_color = icon_color

        info = QVBoxLayout()
        info.setSpacing(3)
        info.setContentsMargins(0, 0, 0, 0)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        display = status.get("display_name") or self.name.title()
        name_lab = _ElideLabel(display)
        name_lab.setStyleSheet(
            theme.text_qss(size=13.5, weight=600, color=theme.TEXT))
        name_lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        name_lab.setMinimumWidth(0)
        name_row.addWidget(name_lab, 1)
        name_row.addWidget(self._pill(status.get("status", "unknown")))
        self._chevron = QLabel("▸")
        self._chevron.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 11px;"
            f"font-family: {theme.FONT_FAMILY};")
        name_row.addWidget(self._chevron)
        info.addLayout(name_row)
        self._name_lab = name_lab

        auth = _AUTH_LABELS.get(status.get("authentication", "none"), "none")
        detail = (status.get("detail") or "").strip()
        sub_text = f"{auth}"
        if detail:
            sub_text = f"{sub_text} · {detail}"
        sub_lab = _ElideLabel(sub_text)
        sub_lab.setStyleSheet(
            theme.text_qss(size=11.5, weight=400, color=theme.TEXT_FAINT))
        sub_lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        sub_lab.setMinimumWidth(0)
        info.addWidget(sub_lab)
        self._sub_lab = sub_lab

        caps = status.get("capabilities") or []
        if caps:
            caps_box = QWidget()
            caps_box.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            caps_lay = QHBoxLayout(caps_box)
            caps_lay.setContentsMargins(0, 0, 0, 0)
            caps_lay.setSpacing(4)
            shown = caps[:3]
            for cap in shown:
                chip = _ElideLabel(cap.replace("_", " "))
                chip.setStyleSheet(
                    f"background: {theme.rgba(theme.ACCENT, 22)};"
                    f"color: {theme.TEXT_SOFT}; border-radius: 7px;"
                    f"padding: 3px 8px; font-size: 10px;"
                    f"font-family: {theme.FONT_FAMILY}; font-weight: 500;")
                chip.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                chip.setMinimumWidth(0)
                chip.setMaximumWidth(104)
                caps_lay.addWidget(chip)
            if len(caps) > 3:
                more = QLabel(f"+{len(caps) - 3}")
                more.setStyleSheet(
                    f"color: {theme.TEXT_FAINT}; font-size: 10px;"
                    f"font-family: {theme.FONT_FAMILY};")
                caps_lay.addWidget(more)
            caps_lay.addStretch(1)
            info.addWidget(caps_box)
            self._caps_box = caps_box
            self._chips = [chip for chip in caps_box.findChildren(QLabel)]

        header.addLayout(info, 1)

        connected = status.get("connected", False)
        label_text = "Disconnect" if connected else "Connect"
        self._action = GlassButton(label_text, icon_name=None,
                                   variant="primary" if not connected else "ghost",
                                   radius=10)
        self._action.setMinimumWidth(110)
        self._action.setEnabled(not (status.get("status") == "unavailable"))
        self._action.clicked.connect(
            lambda: self.actionRequested.emit(self.name, not connected))
        header.addWidget(self._action)
        self._action.setCursor(Qt.ArrowCursor)
        root.addLayout(header)

        desc = (status.get("description") or "").strip()
        self._desc_lab = label(desc, size=12, weight=400, color=theme.TEXT_SOFT,
                               wrap=True)
        self._desc_lab.setWordWrap(True)
        self._desc_lab.setContentsMargins(58, 0, 118, 12)
        self._desc_lab.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._desc_lab.setMaximumHeight(0)
        self._desc_lab.hide()
        root.addWidget(self._desc_lab)
        self._desc_text = desc

    # ------------------------------------------------------------------ #
    def _pill(self, status: str) -> QLabel:
        text, color = _STATUS_STYLE.get(status, _STATUS_STYLE["unknown"])
        pill = QLabel(text)
        pill.setStyleSheet(
            f"background: {theme.rgba(color, 28)}; color: {color};"
            f"border-radius: 8px; padding: 2px 9px; font-size: 10px;"
            f"font-family: {theme.FONT_FAMILY}; font-weight: 600;")
        return pill

    def set_busy(self, busy: bool, message: str = ""):
        self._action.setEnabled(not busy)
        if busy:
            self._action.setText("Working…")
        else:
            connected = self._status.get("connected", False)
            self._action.setText("Disconnect" if connected else "Connect")
            self._action.setEnabled(
                not (self._status.get("status") == "unavailable"))
        if message:
            self._sub_lab.setText(message)

    # ------------------------------------------------------------------ #
    # Click-to-expand description
    # ------------------------------------------------------------------ #
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._desc_text:
            self.toggle_details()
        super().mouseReleaseEvent(event)

    def toggle_details(self):
        self._expanded = not self._expanded
        self._chevron.setText("▾" if self._expanded else "▸")
        if self._expanded:
            self._desc_lab.show()
            height = min(self._desc_lab.sizeHint().height(), 150)
        else:
            height = 0
        anim = QPropertyAnimation(self._desc_lab, b"maximumHeight", self)
        anim.setDuration(170)
        anim.setStartValue(self._desc_lab.maximumHeight())
        anim.setEndValue(height)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        if not self._expanded:
            anim.finished.connect(self._desc_lab.hide)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _anim_to(self, prop, target):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(150)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        p.fillPath(path, QColor(theme.GLASS))
        if self._hover > 0:
            p.fillPath(path, QColor(theme.HOVER))
        border = QColor(theme.BORDER)
        if self._hover > 0.5:
            border = QColor(theme.BORDER_STRONG)
        p.setPen(QPen(border, 1.0))
        p.drawPath(path)

    def apply_theme(self):
        icon_name, icon_color = _BRAND_ICONS.get(
            self.name, ("data", theme.ACCENT))
        self._icon_box.setStyleSheet(
            f"background: {theme.rgba(icon_color, 40)}; border-radius: 11px;")
        self._icon_lab.setPixmap(icons.pixmap(icon_name, icon_color, 19))
        self._name_lab.setStyleSheet(
            theme.text_qss(size=13.5, weight=600, color=theme.TEXT))
        self._sub_lab.setStyleSheet(
            theme.text_qss(size=11.5, weight=400, color=theme.TEXT_FAINT))
        self._chevron.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 11px;"
            f"font-family: {theme.FONT_FAMILY};")
        self._desc_lab.setStyleSheet(
            theme.text_qss(size=12, weight=400, color=theme.TEXT_SOFT))
        self._action.update()
        self.update()


class ConnectionsPage(QWidget):
    """The Connections view: every integration and its status."""

    refreshRequested = Signal()
    notice = Signal(str)
    resultReady = Signal(bool, str)  # (success, message) from a worker

    def __init__(self, manager=None, parent=None):
        super().__init__(parent)
        if manager is None:
            from connections.manager import connection_manager
            manager = connection_manager
        self._manager = manager
        self._rows: dict[str, _AdapterRow] = {}
        self._busy: set[str] = set()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 18, 28, 18)
        lay.setSpacing(14)

        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self._title = label("Connections", size=21, weight=700, color=theme.TEXT)
        title_box.addWidget(self._title)
        self._subtitle = label(
            "Connect the apps and services Nova can reach on your behalf.",
            size=12.5, weight=400, color=theme.TEXT_SOFT)
        title_box.addWidget(self._subtitle)
        head.addLayout(title_box)
        head.addStretch(1)
        self._summary = label("", size=12, weight=500, color=theme.TEXT_SOFT,
                              wrap=False)
        head.addWidget(self._summary)
        self._refresh_btn = GlassIconButton("refresh", size=38, icon_size=17,
                                            tooltip="Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self._refresh_btn)
        lay.addLayout(head)

        self._notice_lab = label("", size=12, weight=500, color=theme.STATUS_ERROR,
                                 wrap=False)
        self._notice_lab.setWordWrap(True)
        self._notice_lab.hide()
        lay.addWidget(self._notice_lab)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(9000)
        self._notice_timer.timeout.connect(self._notice_lab.hide)

        self._body = QWidget(self)
        self._body.setAttribute(Qt.WA_TranslucentBackground)
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 4, 0, 10)
        self._body_lay.setSpacing(8)
        self._body_lay.setAlignment(Qt.AlignTop)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(theme.scroll_qss())
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setWidget(self._body)
        lay.addWidget(self._scroll, 1)

        self._empty = QWidget(self)
        self._empty.setAttribute(Qt.WA_TranslucentBackground)
        empty_lay = QVBoxLayout(self._empty)
        empty_lay.setContentsMargins(0, 0, 0, 0)
        empty_lay.setSpacing(10)
        empty_lay.addStretch(1)
        empty_icon = QLabel()
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_icon.setPixmap(icons.pixmap("data", theme.TEXT_FAINT, 40))
        empty_lay.addWidget(empty_icon)
        empty_msg = label(
            "No integrations are registered yet.",
            size=14, weight=500, color=theme.TEXT_FAINT)
        empty_msg.setAlignment(Qt.AlignCenter)
        empty_lay.addWidget(empty_msg)
        empty_sub = label(
            "Restart Nova after enabling integrations to populate this page.",
            size=12, weight=400, color=theme.TEXT_FAINT)
        empty_sub.setAlignment(Qt.AlignCenter)
        empty_lay.addWidget(empty_sub)
        empty_lay.addStretch(3)
        self._empty_icon = empty_icon
        lay.addWidget(self._empty, 1)
        self._empty.hide()

        self.refreshRequested.connect(self.refresh)
        self.notice.connect(lambda msg: self.show_notice(msg, error=True))
        self.resultReady.connect(self._on_result)
        self.refresh()

    def _on_result(self, ok: bool, message: str):
        self.show_notice(message, error=not ok)

    # ------------------------------------------------------------------ #
    def refresh(self):
        statuses = self._manager.get_all_statuses()
        if not statuses:
            self._scroll.hide()
            self._empty.show()
            self._summary.setText("0 services")
            return
        self._empty.hide()
        self._scroll.show()
        seen = {s["name"] for s in statuses}
        for name in list(self._rows.keys()):
            if name not in seen:
                self._rows[name].setParent(None)
                self._rows.pop(name)
        for status in statuses:
            name = status["name"]
            if name in self._rows:
                self._rows[name]._status = status
            else:
                row = _AdapterRow(status)
                row.actionRequested.connect(self._on_action)
                self._rows[name] = row
                self._body_lay.addWidget(row)
            self._rows[name].set_busy(name in self._busy)
        self._update_summary(statuses)

    def _update_summary(self, statuses):
        connected = sum(1 for s in statuses if s.get("connected"))
        needs = sum(1 for s in statuses
                    if s.get("status") in ("requires_auth", "not_configured"))
        self._summary.setText(f"{len(statuses)} services · "
                              f"{connected} connected · {needs} to set up")

    def _on_action(self, name: str, connect: bool):
        if name in self._busy:
            return
        row = self._rows.get(name)
        if connect and row:
            status = row._status
            if (status.get("status") == "not_configured"
                    and status.get("authentication") == "api_key"):
                adapter = self._manager.get(name)
                saver = getattr(adapter, "save_api_key", None)
                if saver is None:
                    self.show_notice(
                        f"{status.get('display_name') or name} can't store "
                        "an API key from here.", error=True)
                    return
                token, ok = QInputDialog.getText(
                    self, f"Connect {status.get('display_name') or name}",
                    "Paste the API key / token:",
                    QLineEdit.Password)
                if not ok or not token.strip():
                    return
                saver(token.strip())
        self._busy.add(name)
        if row:
            row.set_busy(True)
        threading.Thread(target=self._run_action, args=(name, connect),
                         daemon=True).start()

    def _run_action(self, name: str, connect: bool):
        try:
            if connect:
                result = self._manager.connect(name)
            else:
                result = self._manager.disconnect(name)
            message = result.get("message") or result.get("error") or ""
            ok = bool(result.get("success"))
            self.refreshRequested.emit()
            if message:
                self.resultReady.emit(ok, message)
        finally:
            self._busy.discard(name)
            self.refreshRequested.emit()

    def show_notice(self, text: str, error: bool = False):
        """Show a transient status line under the page header."""
        self._notice_color = theme.STATUS_ERROR if error else theme.STATUS_SPEAKING
        self._notice_lab.setText(text)
        self._notice_lab.setStyleSheet(
            theme.text_qss(size=12, weight=500, color=self._notice_color))
        self._notice_lab.show()
        self._notice_timer.start()

    def apply_theme(self):
        self._title.setStyleSheet(
            theme.text_qss(size=21, weight=700, color=theme.TEXT))
        self._subtitle.setStyleSheet(
            theme.text_qss(size=12.5, weight=400, color=theme.TEXT_SOFT))
        self._summary.setStyleSheet(
            theme.text_qss(size=12, weight=500, color=theme.TEXT_SOFT))
        if not self._notice_lab.isHidden():
            self._notice_lab.setStyleSheet(
                theme.text_qss(size=12, weight=500,
                               color=self._notice_color))
        self._scroll.setStyleSheet(theme.scroll_qss())
        self._empty_icon.setPixmap(icons.pixmap("data", theme.TEXT_FAINT, 40))
        for w in self._body.findChildren(QWidget):
            if hasattr(w, "apply_theme"):
                try:
                    w.apply_theme()
                except Exception:
                    pass
        self.update()
