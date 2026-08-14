"""
InputDock — the floating input pill of the redesign.

A rounded-2xl surface pill: attach button, auto-growing text area,
mic button (with per-state styling), and a round accent send button.
Below it, a quiet footnote: "Nova can make mistakes — verify important
info."
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal, QTimer, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath, QFont, QPalette
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLabel, QMenu, QFileDialog,
    QSizePolicy,
)

from .. import icons, theme
from .glass import GlassIconButton

_MIC_COLORS = {
    "idle": None,
    "listening": "STATUS_LISTENING",
    "thinking": "STATUS_THINKING",
    "speaking": "STATUS_SPEAKING",
}


class InputDock(QWidget):
    submitted = Signal(str)
    voicePressed = Signal()
    attachRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._voice_state = "idle"
        self._focused = False
        self._hovered = False
        self._attachments: list[tuple[str, str]] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.pill = QWidget(self)
        self.pill.setAttribute(Qt.WA_TranslucentBackground)
        self.pill.setMinimumHeight(56)
        self.pill.setMaximumHeight(130)
        self.pill.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        pill_lay = QHBoxLayout(self.pill)
        pill_lay.setContentsMargins(10, 8, 10, 8)
        pill_lay.setSpacing(6)

        self.attach_btn = GlassIconButton("paperclip", size=36, icon_size=17,
                                          tooltip="Attach", parent=self.pill)
        self.attach_btn.clicked.connect(self.attachRequested.emit)
        pill_lay.addWidget(self.attach_btn, 0, Qt.AlignVCenter)

        self.editor = QTextEdit(self.pill)
        self.editor.setPlaceholderText(
            f"Message {theme.ASSISTANT_NAME}…"
        )
        self.editor.setStyleSheet(theme.edit_qss())
        self.editor.setFrameShape(QTextEdit.Shape.NoFrame)
        self._apply_editor_palette()
        self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor.setAcceptRichText(False)
        self.editor.setFixedHeight(40)
        self.editor.document().setDocumentMargin(8)
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.setTabChangesFocus(False)
        pill_lay.addWidget(self.editor, 1)
        self.mic_btn = GlassIconButton("mic", size=38, icon_size=18, tooltip="Voice mode")
        self.mic_btn.clicked.connect(self.voicePressed.emit)
        pill_lay.addWidget(self.mic_btn, 0, Qt.AlignVCenter)

        self.send_btn = _SendButton(self.pill)
        self.send_btn.clicked.connect(self._submit)
        pill_lay.addWidget(self.send_btn, 0, Qt.AlignVCenter)

        lay.addWidget(self.pill)

        self.hint = QLabel("Nova can make mistakes — verify important info.")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet(theme.hint_qss(size=10.5, color=theme.TEXT_FAINT))
        lay.addWidget(self.hint)

        self._attach_menu = None
        self._update_height()

    # ------------------------------------------------------------------ #
    def _apply_editor_palette(self):
        pal = self.editor.palette()
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.TEXT_FAINT))
        self.editor.setPalette(pal)

    def _has_text(self) -> bool:
        return bool(self.editor.toPlainText().strip())

    def _submit(self):
        text = self.editor.toPlainText().strip()
        if not text:
            return
        self.editor.clear()
        self.submitted.emit(text)

    def _on_text_changed(self):
        self._update_height()
        self.send_btn.update()

    def _update_height(self):
        doc = self.editor.document()
        dh = doc.size().height()
        if doc.lineCount() <= 1:
            pad = max(2, int((40 - dh) / 2))
            self.editor.setViewportMargins(0, pad, 0, pad)
            self.editor.setFixedHeight(40)
        else:
            self.editor.setViewportMargins(0, 4, 0, 4)
            self.editor.setFixedHeight(max(40, min(88, int(dh) + 8)))
        self.pill.setMaximumHeight(self.editor.height() + 20)
        parent = self.parentWidget()
        while parent is not None:
            parent.updateGeometry()
            if parent.layout() is not None:
                parent.layout().invalidate()
            parent = parent.parentWidget()

    # ------------------------------------------------------------------ #
    def set_name(self, name: str):
        self.editor.setPlaceholderText(f"Message {name}…")

    def set_voice_state(self, state: str):
        self._voice_state = state if state in _MIC_COLORS else "idle"
        self.mic_btn._color = None
        key = _MIC_COLORS[self._voice_state]
        if key is not None:
            self.mic_btn._color = getattr(theme, key)
        self.mic_btn._icon_name = "mic"
        if state == "listening":
            self.mic_btn._color = theme.STATUS_LISTENING
            self.editor.setPlaceholderText("Listening…")
        elif state == "thinking":
            self.editor.setPlaceholderText("Thinking…")
        elif state == "speaking":
            self.editor.setPlaceholderText("Speaking…")
        else:
            self.editor.setPlaceholderText(f"Message {theme.ASSISTANT_NAME}…")
        self.mic_btn.update()

    def focus_input(self):
        self.editor.setFocus()
        from PySide6.QtGui import QTextCursor
        self.editor.moveCursor(QTextCursor.MoveOperation.End)

    def add_attachment(self, kind: str, name: str):
        self._attachments.append((kind, name))

    def clear_attachments(self):
        self._attachments.clear()

    def show_attach_menu(self, menu: QMenu):
        menu.setStyleSheet(theme.menu_qss())
        self._attach_menu = menu
        menu.popup(self.attach_btn.mapToGlobal(
            self.attach_btn.rect().bottomLeft()))

    def text(self) -> str:
        return self.editor.toPlainText()

    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.pill.width() - 1, self.pill.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 24, 24)
        p.fillPath(path, QColor(theme.SURFACE))
        border = QColor(theme.BORDER_FOCUS) if self._focused else QColor(theme.BORDER_STRONG)
        p.setPen(QPen(border, 1.0))
        p.drawPath(path)

    def paintEvent_pill(self):
        pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_height()

    # send button is painted inside pill's paintEvent? It's a child widget
    # with its own paint, so we paint it here on demand via the pill overlay.
    def _paint_send(self):
        return

    def apply_theme(self):
        self.editor.setStyleSheet(theme.edit_qss())
        self._apply_editor_palette()
        self.hint.setStyleSheet(theme.hint_qss(size=10.5, color=theme.TEXT_FAINT))
        self.editor.setPlaceholderText(f"Message {theme.ASSISTANT_NAME}…")
        self.update()
        self.send_btn.update()
        if self._attach_menu is not None:
            self._attach_menu.setStyleSheet(theme.menu_qss())


# Backwards-compatible name used by old imports / QA scripts.
class ChatInputBar(InputDock):
    pass


class _SendButton(QWidget):
    """The round accent send button; disabled-looking when empty."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(38, 38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = 0.0

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)

    def enterEvent(self, event):
        anim = QPropertyAnimation(self, b"hover", self)
        anim.setDuration(140)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        super().enterEvent(event)

    def leaveEvent(self, event):
        anim = QPropertyAnimation(self, b"hover", self)
        anim.setDuration(140)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)

        from .. import theme as _t
        dock = self.parent()
        while dock is not None and not hasattr(dock, "_has_text"):
            dock = dock.parent()
        empty = dock is None or not dock._has_text()
        color = QColor(_t.ACCENT)
        if empty:
            color.setAlpha(110)
        elif self._hover > 0:
            color = color.lighter(int(100 + 12 * self._hover))
        p.fillPath(path, color)
        pm = icons.pixmap("arrow-up", _t.ON_ACCENT, 18)
        p.drawPixmap(int((self.width() - 18) / 2), int((self.height() - 18) / 2), pm)
