"""
LibraryPage — the knowledge library of the redesign.

Collections (folder cards) and recent items (prompt / snippet / note
cards with tag chips). The content is sample data matching the mockup;
item cards support selection.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
    QGridLayout,
)

from .. import icons, theme
from .glass import GlassLineEdit, GlassIconButton, label, SectionHeader

COLLECTIONS = [
    ("System Prompts", 12, "prompt"),
    ("React Snippets", 45, "code"),
    ("Meeting Notes", 8, "notes"),
    ("PDFs & Docs", 23, "file"),
]

LIBRARY_ITEMS = [
    ("Code Review Checklist", "prompt", ["review", "engineering"],
     "2h ago", "Weekly checklist for peer code reviews"),
    ("useSyncExternalStore pattern", "snippet", ["react", "hooks"],
     "5h ago", "React 18 external store subscription"),
    ("Team Standup Notes", "notes", ["meetings"], "1d ago",
     "Actions and blockers from Monday standup"),
    ("JSON Schema Validator", "snippet", ["python", "pydantic"],
     "2d ago", "Pydantic-based request validation"),
    ("Meeting with Design Sync", "notes", ["meetings", "design"],
     "3d ago", "Design review — navigation rework"),
    ("Weekly Newsletter Draft", "prompt", ["writing"], "4d ago",
     "Template for the product newsletter"),
]

_BADGE_STYLE = {
    "prompt": ("terminal", theme.ACCENT_2),
    "snippet": ("code", theme.ACCENT),
    "code": ("code", theme.ACCENT),
    "notes": ("file-text", theme.TERTIARY),
    "file": ("file", theme.STATUS_SPEAKING),
}


class _FolderCard(QWidget):
    clicked = Signal(str)

    def __init__(self, name: str, count: int, kind: str, parent=None):
        super().__init__(parent)
        self._name = name
        self._count = count
        self._kind = kind
        self._hover = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(96)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        icon_box = QWidget()
        icon_box.setFixedSize(34, 34)
        icon_name, icon_color = _BADGE_STYLE[kind]
        icon_box.setStyleSheet(f"background: {theme.rgba(icon_color, 40)};"
                               f"border-radius: 9px;")
        ib_lay = QVBoxLayout(icon_box)
        ib_lay.setContentsMargins(0, 0, 0, 0)
        ic = QLabel()
        ic.setAlignment(Qt.AlignCenter)
        ic.setPixmap(icons.pixmap(icon_name, icon_color, 17))
        ib_lay.addWidget(ic)
        lay.addWidget(icon_box)
        self._icon_box = icon_box
        self._icon_lab = ic
        name_lab = label(name, size=13.5, weight=600, color=theme.TEXT, wrap=False)
        lay.addWidget(name_lab)
        self._name_lab = name_lab
        count_lab = label(f"{count} items", size=11.5, weight=400,
                          color=theme.TEXT_FAINT, wrap=False)
        lay.addWidget(count_lab)
        self._count_lab = count_lab
        lay.addStretch(1)

    def _anim_to(self, prop, target):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(160)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._name)
        super().mouseReleaseEvent(event)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)

    def apply_theme(self):
        icon_name, icon_color = _BADGE_STYLE[self._kind]
        self._icon_box.setStyleSheet(
            f"background: {theme.rgba(icon_color, 40)}; border-radius: 9px;")
        self._icon_lab.setPixmap(icons.pixmap(icon_name, icon_color, 17))
        self._name_lab.setStyleSheet(
            theme.text_qss(size=13.5, weight=600, color=theme.TEXT))
        self._count_lab.setStyleSheet(
            theme.text_qss(size=11.5, weight=400, color=theme.TEXT_FAINT))
        self.update()

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


class _ItemCard(QWidget):
    clicked = Signal(str)

    def __init__(self, title: str, kind: str, tags: list, updated: str,
                 desc: str, parent=None):
        super().__init__(parent)
        self._title = title
        self._hover = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(0, 170)

        icon_name, icon_color = _BADGE_STYLE[kind]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        top = QHBoxLayout()
        icon_box = QWidget()
        icon_box.setFixedSize(30, 30)
        icon_box.setStyleSheet(f"background: {theme.rgba(icon_color, 40)};"
                               f"border-radius: 8px;")
        ib_lay = QVBoxLayout(icon_box)
        ib_lay.setContentsMargins(0, 0, 0, 0)
        ic = QLabel()
        ic.setAlignment(Qt.AlignCenter)
        ic.setPixmap(icons.pixmap(icon_name, icon_color, 15))
        ib_lay.addWidget(ic)
        top.addWidget(icon_box)
        self._icon_box = icon_box
        self._icon_lab = ic
        self._icon_name = icon_name
        self._icon_color = icon_color
        top.addStretch(1)
        time_lab = label(updated, size=11, weight=400, color=theme.TEXT_FAINT,
                         wrap=False)
        top.addWidget(time_lab)
        self._time_lab = time_lab
        lay.addLayout(top)

        title_lab = label(title, size=13.5, weight=600, color=theme.TEXT, wrap=False)
        lay.addWidget(title_lab)
        self._title_lab = title_lab

        desc_lab = label(desc, size=12, weight=400, color=theme.TEXT_SOFT)
        lay.addWidget(desc_lab)
        self._desc_lab = desc_lab
        lay.addStretch(1)

        tags_row = QHBoxLayout()
        tags_row.setSpacing(6)
        self._chips: list[QLabel] = []
        for tag in tags:
            chip = QLabel(f"#{tag}")
            chip.setStyleSheet(
                f"background: {theme.rgba(theme.ACCENT, 26)};"
                f"color: {theme.TEXT_SOFT}; border-radius: 8px;"
                f"padding: 3px 9px; font-size: 10.5px;"
                f"font-family: {theme.FONT_FAMILY}; font-weight: 500;")
            tags_row.addWidget(chip)
            self._chips.append(chip)
        tags_row.addStretch(1)
        lay.addLayout(tags_row)

    def _anim_to(self, prop, target):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(160)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._title)
        super().mouseReleaseEvent(event)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)

    def apply_theme(self):
        self._icon_box.setStyleSheet(
            f"background: {theme.rgba(self._icon_color, 40)}; border-radius: 8px;")
        self._icon_lab.setPixmap(
            icons.pixmap(self._icon_name, self._icon_color, 15))
        self._time_lab.setStyleSheet(
            theme.text_qss(size=11, weight=400, color=theme.TEXT_FAINT))
        self._title_lab.setStyleSheet(
            theme.text_qss(size=13.5, weight=600, color=theme.TEXT))
        self._desc_lab.setStyleSheet(
            theme.text_qss(size=12, weight=400, color=theme.TEXT_SOFT))
        chip_qss = (
            f"background: {theme.rgba(theme.ACCENT, 26)};"
            f"color: {theme.TEXT_SOFT}; border-radius: 8px;"
            f"padding: 3px 9px; font-size: 10.5px;"
            f"font-family: {theme.FONT_FAMILY}; font-weight: 500;")
        for chip in self._chips:
            chip.setStyleSheet(chip_qss)
        self.update()

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


class LibraryPage(QWidget):
    itemRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 18, 28, 18)
        lay.setSpacing(14)

        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self._title = label("Library", size=21, weight=700, color=theme.TEXT)
        title_box.addWidget(self._title)
        self._subtitle = label(
            "Prompts, snippets, and notes — everything you've saved.",
            size=12.5, weight=400, color=theme.TEXT_SOFT)
        title_box.addWidget(self._subtitle)
        head.addLayout(title_box)
        head.addStretch(1)
        self._search = GlassLineEdit("Search library…", max_width=280)
        head.addWidget(self._search)
        self._filter_btn = GlassIconButton("filter", size=38, icon_size=17,
                                           tooltip="Filter")
        head.addWidget(self._filter_btn)
        self._sort_btn = GlassIconButton("sort", size=38, icon_size=17,
                                         tooltip="Sort")
        head.addWidget(self._sort_btn)
        lay.addLayout(head)

        # ---- collections ----
        self._body = QWidget(self)
        self._body.setAttribute(Qt.WA_TranslucentBackground)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 4, 0, 10)
        body_layout.setSpacing(10)

        body_layout.addWidget(SectionHeader("Collections"))
        self._collections_grid = QGridLayout()
        self._collections_grid.setSpacing(12)
        for idx, (name, count, kind) in enumerate(COLLECTIONS):
            card = _FolderCard(name, count, kind)
            card.clicked.connect(self.itemRequested.emit)
            self._collections_grid.addWidget(card, idx // 4, idx % 4)
        body_layout.addLayout(self._collections_grid)
        body_layout.addSpacing(8)

        body_layout.addWidget(SectionHeader("Recent Items"))
        self._items_grid = QGridLayout()
        self._items_grid.setSpacing(12)
        for idx, (title, kind, tags, updated, desc) in enumerate(LIBRARY_ITEMS):
            card = _ItemCard(title, kind, tags, updated, desc)
            card.clicked.connect(self.itemRequested.emit)
            self._items_grid.addWidget(card, idx // 3, idx % 3)
        body_layout.addLayout(self._items_grid)
        body_layout.addStretch(1)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(theme.scroll_qss())
        self._scroll.setWidget(self._body)
        lay.addWidget(self._scroll, 1)

    def apply_theme(self):
        self._title.setStyleSheet(theme.text_qss(size=21, weight=700, color=theme.TEXT))
        self._subtitle.setStyleSheet(
            theme.text_qss(size=12.5, weight=400, color=theme.TEXT_SOFT))
        self._search.apply_theme()
        self._scroll.setStyleSheet(theme.scroll_qss())
        for w in self._body.findChildren(QWidget):
            if hasattr(w, "apply_theme"):
                try:
                    w.apply_theme()
                except Exception:
                    pass
        self.update()


class TemplatesPage(QWidget):
    """Placeholder — the Templates view (nav tab) lives here."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 18, 28, 18)
        lay.setSpacing(10)
        self._title = label("Templates", size=21, weight=700, color=theme.TEXT)
        lay.addWidget(self._title)
        lay.addStretch(1)

        center = QVBoxLayout()
        center.setSpacing(12)
        center.addStretch(1)
        icon_lab = QLabel()
        icon_lab.setAlignment(Qt.AlignCenter)
        icon_lab.setPixmap(icons.pixmap("puzzle", theme.TEXT_FAINT, 40))
        center.addWidget(icon_lab)
        self._icon_lab = icon_lab
        msg = label("Your saved templates will appear here.",
                    size=14, weight=400, color=theme.TEXT_FAINT)
        msg.setAlignment(Qt.AlignCenter)
        center.addWidget(msg)
        sub = label("Ask Nova to save a workflow as a reusable template.",
                    size=12, weight=400, color=theme.TEXT_FAINT)
        sub.setAlignment(Qt.AlignCenter)
        center.addWidget(sub)
        center.addStretch(3)
        lay.addLayout(center)
        lay.addStretch(1)

    def apply_theme(self):
        self._title.setStyleSheet(
            theme.text_qss(size=21, weight=700, color=theme.TEXT))
        self._icon_lab.setPixmap(icons.pixmap("puzzle", theme.TEXT_FAINT, 40))
        for w in self.findChildren(QWidget):
            if hasattr(w, "_text_style"):
                try:
                    w.apply_theme()
                except Exception:
                    pass
        self.update()
