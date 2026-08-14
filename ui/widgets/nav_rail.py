"""
NavRail — the left navigation column of the redesign.

Top: Nova orb + name + subtitle. Then a "New Thread" primary button,
the page tabs (Chat / History / Templates / Library), and a footer with
the live system-status card and a Settings row.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel

from .. import icons, theme
from .nova_orb import NovaOrb
from .glass import GlassButton

TABS = [
    ("chat", "chat", "Chat"),
    ("history", "history", "History"),
    ("templates", "puzzle", "Templates"),
    ("library", "library", "Library"),
    ("connections", "data", "Connections"),
]


class NavTab(QWidget):
    clicked = Signal(str)

    def __init__(self, key: str, icon_name: str, text: str, parent=None):
        super().__init__(parent)
        self._key = key
        self._icon_name = icon_name
        self._text = text
        self._active = False
        self._hover = 0.0
        self.setFixedHeight(theme.NAV_ITEM_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_active(self, active: bool):
        self._active = active
        self.update()

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
            self.clicked.emit(self._key)
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
        path.addRoundedRect(rect, 11, 11)

        if self._active:
            p.fillPath(path, QColor(theme.HOVER_STRONG))
        elif self._hover > 0:
            p.fillPath(path, QColor(theme.HOVER))

        if self._active:
            # accent tick on the left edge
            p.setBrush(QColor(theme.ACCENT))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(1, 12, 3.5, self.height() - 24), 2, 2)

        color = QColor(theme.TEXT if self._active else theme.TEXT_SOFT)
        pm = icons.pixmap(self._icon_name, color.name(), 17)
        p.drawPixmap(18, int((self.height() - pm.height()) / 2), pm)

        p.setPen(color)
        font = p.font()
        font.setFamily(theme.FONT_FAMILY)
        font.setPointSizeF(9.6)
        font.setWeight(QFont.Weight.Medium if self._active else QFont.Weight.Normal)
        p.setFont(font)
        p.drawText(QRectF(46, 0, self.width() - 52, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self._text)


class NavRail(QWidget):
    pageSelected = Signal(str)
    newThreadRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(theme.SIDEBAR_W)
        self.setAttribute(Qt.WA_TranslucentBackground)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 20, 16, 16)
        lay.setSpacing(0)

        # ---- brand: orb + name ----
        brand = QHBoxLayout()
        brand.setSpacing(12)
        self.orb = NovaOrb(size=44)
        brand.addWidget(self.orb, 0, Qt.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(1)
        self._name_label = QLabel(theme.ASSISTANT_NAME)
        self._name_label.setStyleSheet(
            theme.text_qss(size=15.5, weight=700, color=theme.TEXT)
        )
        names.addWidget(self._name_label)
        self._sub_label = QLabel("AI assistant")
        self._sub_label.setStyleSheet(theme.hint_qss(size=11.5))
        names.addWidget(self._sub_label)
        brand.addLayout(names)
        brand.addStretch(1)
        lay.addLayout(brand)
        lay.addSpacing(22)

        # ---- new thread ----
        self.new_btn = GlassButton("New thread", icon_name="plus", icon_size=15,
                                   variant="primary", pill=True)
        self.new_btn.setMinimumHeight(42)
        self.new_btn.clicked.connect(self.newThreadRequested.emit)
        lay.addWidget(self.new_btn)
        lay.addSpacing(18)

        # ---- tabs ----
        self._tabs: dict[str, NavTab] = {}
        for key, icon_name, text in TABS:
            tab = NavTab(key, icon_name, text)
            tab.clicked.connect(self.pageSelected.emit)
            lay.addWidget(tab)
            self._tabs[key] = tab
        lay.addSpacing(6)

        lay.addStretch(1)

        # ---- status card ----
        self._status_card = QWidget(self)
        self._status_card.setFixedHeight(66)
        status_lay = QVBoxLayout(self._status_card)
        status_lay.setContentsMargins(14, 10, 14, 10)
        status_lay.setSpacing(3)
        self._status_label = QLabel(theme.STATUS_LABELS["idle"])
        self._status_label.setStyleSheet(
            theme.text_qss(size=12, weight=600, color=theme.TEXT)
        )
        status_lay.addWidget(self._status_label)
        self._status_sub = QLabel("·")
        self._status_sub.setStyleSheet(theme.hint_qss(size=10.5))
        status_lay.addWidget(self._status_sub)
        lay.addWidget(self._status_card)
        self._status_card.paintEvent = self._status_paint

        # ---- settings ----
        self._settings_tab = NavTab("settings", "gear", "Settings")
        self._settings_tab.clicked.connect(lambda _k: self.settingsRequested.emit())
        lay.addWidget(self._settings_tab)

    # ------------------------------------------------------------------ #
    def _status_paint(self, event):
        """Glass box behind the status labels."""
        p = QPainter(self._status_card)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self._status_card.width() - 1,
                      self._status_card.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 13, 13)
        p.fillPath(path, QColor(theme.GLASS))
        p.setPen(QPen(QColor(theme.BORDER), 1.0))
        p.drawPath(path)

    def set_active(self, page: str):
        for key, tab in self._tabs.items():
            tab.set_active(key == page)

    def set_name(self, name: str):
        self._name_label.setText(name)

    def set_status(self, state: str):
        label = theme.STATUS_LABELS.get(state, theme.STATUS_LABELS["idle"])
        self._status_label.setText(label)
        color = getattr(theme, f"STATUS_{state.upper()}", theme.STATUS_IDLE)
        self._status_label.setStyleSheet(
            theme.text_qss(size=12, weight=600, color=color)
        )
        self.orb.set_state(state)

    def set_model(self, provider: str, model: str):
        short = model.split(":")[0].split("/")[-1].strip() if model else provider
        self._status_sub.setText(f"{provider} · {short}")

    def apply_theme(self):
        self._name_label.setStyleSheet(
            theme.text_qss(size=15.5, weight=700, color=theme.TEXT)
        )
        self._sub_label.setStyleSheet(theme.hint_qss(size=11.5))
        self.set_status("idle")
        for tab in self._tabs.values():
            tab.update()
        self.update()
