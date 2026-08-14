"""
HistoryPage — conversation history grouped by day.

Groups: Today / Yesterday / Last Week / Older. Each row is a card with
title, two-line preview, relative time, and hover actions (pin/delete).
A search field filters by title or preview text.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QRectF, Signal, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
)

from .. import icons, theme
from .glass import GlassLineEdit, GlassIconButton, label, SectionHeader
from .message_bubble import relative_time


class _HistoryRow(QWidget):
    openRequested = Signal(str)
    pinRequested = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self._item = item
        self._hover = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(78)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 12, 12, 12)
        lay.setSpacing(14)

        self._pin_icon = QLabel()
        self._pin_icon.setFixedWidth(18)
        self._pin_icon.setAlignment(Qt.AlignTop)
        self._update_pin_icon()
        lay.addWidget(self._pin_icon, 0, Qt.AlignVCenter)

        body = QVBoxLayout()
        body.setSpacing(4)
        self._title = QLabel(item.get("title", "Conversation"))
        self._title.setStyleSheet(theme.text_qss(size=14, weight=600, color=theme.TEXT))
        body.addWidget(self._title)

        self._preview = QLabel(item.get("preview", ""))
        self._preview.setStyleSheet(
            theme.text_qss(size=12, weight=400, color=theme.TEXT_SOFT))
        self._preview.setWordWrap(True)
        self._preview.setMaximumHeight(34)
        body.addWidget(self._preview)

        self._time = QLabel(relative_time(item.get("updated", time.time())))
        self._time.setStyleSheet(theme.hint_qss(size=11))
        body.addWidget(self._time)
        lay.addLayout(body, 1)

        actions = QHBoxLayout()
        actions.setSpacing(2)
        self.pin_btn = GlassIconButton("pin", size=28, icon_size=14,
                                       tooltip="Pin", parent=self)
        self.pin_btn.clicked.connect(lambda: self.pinRequested.emit(self._item["id"]))
        actions.addWidget(self.pin_btn)
        self.delete_btn = GlassIconButton("trash", size=28, icon_size=14,
                                          tooltip="Delete", parent=self)
        self.delete_btn.clicked.connect(
            lambda: self.deleteRequested.emit(self._item["id"]))
        actions.addWidget(self.delete_btn)
        self._actions_box = QWidget(self)
        self._actions_box.setLayout(actions)
        self._actions_box.setVisible(False)
        lay.addWidget(self._actions_box, 0, Qt.AlignVCenter)

    def _update_pin_icon(self):
        if self._item.get("pinned"):
            self._pin_icon.setPixmap(icons.pixmap("pin", theme.ACCENT, 16))
        else:
            self._pin_icon.setPixmap(icons.pixmap("pin", theme.BG_4, 16))

    def set_pinned(self, pinned: bool):
        self._item["pinned"] = pinned
        self._update_pin_icon()

    def _anim_to(self, prop, target):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(160)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        self._actions_box.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        self._actions_box.setVisible(False)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.openRequested.emit(self._item["id"])
        super().mouseReleaseEvent(event)

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
        if self._item.get("pinned"):
            border = QColor(theme.ACCENT_GLOW) if self._hover > 0.5 else QColor(theme.BORDER)
        else:
            border = QColor(theme.BORDER)
        p.setPen(QPen(border, 1.0))
        p.drawPath(path)

    def apply_theme(self):
        self._title.setStyleSheet(
            theme.text_qss(size=14, weight=600, color=theme.TEXT))
        self._preview.setStyleSheet(
            theme.text_qss(size=12, weight=400, color=theme.TEXT_SOFT))
        self._time.setStyleSheet(theme.hint_qss(size=11))
        self._update_pin_icon()
        self.update()


class HistoryPage(QWidget):
    openRequested = Signal(str)
    deleteRequested = Signal(str)
    pinToggled = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[dict] = []
        self._query = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 18, 28, 18)
        lay.setSpacing(14)

        # ---- header ----
        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self._title = label("History", size=21, weight=700, color=theme.TEXT)
        title_box.addWidget(self._title)
        self._subtitle = label("", size=12.5, weight=400, color=theme.TEXT_SOFT)
        title_box.addWidget(self._subtitle)
        head.addLayout(title_box)
        head.addStretch(1)

        self._search = GlassLineEdit("Search conversations…", max_width=280)
        self._search.changed.connect(self._on_search)
        head.addWidget(self._search)
        self._filter_btn = GlassIconButton("filter", size=38, icon_size=17,
                                           tooltip="Filter")
        head.addWidget(self._filter_btn)
        self._sort_btn = GlassIconButton("sort", size=38, icon_size=17,
                                         tooltip="Sort")
        head.addWidget(self._sort_btn)
        lay.addLayout(head)

        # ---- groups ----
        self._body_host = QWidget(self)
        self._body_layout = QVBoxLayout(self._body_host)
        self._body_layout.setContentsMargins(0, 4, 0, 10)
        self._body_layout.setSpacing(6)

        self._empty_label = label(
            "No conversations yet — start a chat and it will show up here.",
            size=13, weight=400, color=theme.TEXT_FAINT)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setVisible(False)
        self._body_layout.addWidget(self._empty_label)
        self._body_layout.addStretch(1)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(theme.scroll_qss())
        self._scroll.setWidget(self._body_host)
        lay.addWidget(self._scroll, 1)

    # ------------------------------------------------------------------ #
    def set_items(self, items: list[dict]):
        self._items = list(items)
        self._rebuild()

    def _on_search(self, text: str):
        self._query = (text or "").strip().lower()
        self._rebuild()

    def _rebuild(self):
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None and w is not self._empty_label:
                w.deleteLater()

        items = self._items
        if self._query:
            items = [it for it in items
                     if self._query in (it.get("title") or "").lower()
                     or self._query in (it.get("preview") or "").lower()]

        pinned = [it for it in items if it.get("pinned")]
        others = [it for it in items if not it.get("pinned")]
        groups: list[tuple[str, list]] = []
        if pinned:
            groups.append(("Pinned", pinned))
        now = time.time()
        today = [it for it in others if _is_today(it.get("updated", 0), now)]
        yesterday = [it for it in others if _is_yesterday(it.get("updated", 0), now)]
        last_week = [it for it in others if _is_last_week(it.get("updated", 0), now)]
        older = [it for it in others
                 if it.get("updated", 0) and it not in today
                 and it not in yesterday and it not in last_week]
        for name, group in (("Today", today), ("Yesterday", yesterday),
                            ("Last Week", last_week), ("Older", older)):
            if group:
                groups.append((name, group))

        shown = 0
        for name, group in groups:
            header = SectionHeader(name)
            self._body_layout.addWidget(header)
            for it in group:
                row = _HistoryRow(it)
                row.openRequested.connect(self.openRequested.emit)
                row.pinRequested.connect(self.pinToggled.emit)
                row.deleteRequested.connect(self.deleteRequested.emit)
                self._body_layout.addWidget(row)
                shown += 1

        self._empty_label.setVisible(shown == 0)
        self._subtitle.setText(
            f"{shown} conversation{'s' if shown != 1 else ''}"
            + (" · filtered" if self._query else ""))
        self._body_layout.addWidget(self._empty_label)
        self._body_layout.addStretch(1)

    def apply_theme(self):
        self._title.setStyleSheet(theme.text_qss(size=21, weight=700, color=theme.TEXT))
        self._subtitle.setStyleSheet(
            theme.text_qss(size=12.5, weight=400, color=theme.TEXT_SOFT))
        self._search.apply_theme()
        self._scroll.setStyleSheet(theme.scroll_qss())
        count = self._body_layout.count()
        for i in range(count):
            w = self._body_layout.itemAt(i).widget()
            if w is not None and hasattr(w, "apply_theme"):
                try:
                    w.apply_theme()
                except Exception:
                    pass
        self.update()


def _day(ts: float):
    from datetime import date
    return date.fromtimestamp(ts)


_ONE_DAY = __import__("datetime", fromlist=["timedelta"]).timedelta(days=1)


def _is_today(ts: float, now: float) -> bool:
    return _day(ts) == _day(now)


def _is_yesterday(ts: float, now: float) -> bool:
    return _day(ts) == (_day(now) - _ONE_DAY)


def _is_last_week(ts: float, now: float) -> bool:
    return 0 <= (now - ts) < 7 * 86400
