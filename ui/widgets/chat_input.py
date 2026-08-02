"""
ChatInputBar — the premium floating input dock, the centerpiece of Nova.

A rounded glass card that contains:
    attach button | auto-growing text area | mic | send

- subtle glow ring while focused
- attachment chips row (files / images / screenshots / context)
- pulsing accent mic while listening
- gradient send button that lights up when there's text

Emits:
    submitted(str)          — user pressed Enter / send
    attachRequested()       — attach (paperclip) clicked
    voicePressed()          — mic clicked
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QSize, QTimer, Signal
from PySide6.QtGui import (
    QColor, QPainter, QPen, QLinearGradient, QPainterPath, QTextCursor, QFont,
)
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QTextEdit, QScrollArea, QFrame,
)

from .. import icons
from ..theme import (
    BG_1, BG_2, BG_3, BG_4, BORDER, BORDER_FOCUS, TEXT, TEXT_FAINT, TEXT_SOFT,
    ACCENT, ACCENT_2, ACCENT_GLOW, STATUS_LISTENING, STATUS_THINKING,
    STATUS_SPEAKING, FONT_FAMILY, FONT_BODY, FONT_SMALL, FONT_TINY,
    INPUT_H, INPUT_MAX_H, R_LG, S_SM, S_MD, S_LG, ASSISTANT_NAME, rgba,
)
from ..animations import animate_property


def _input_qss(font_size=FONT_BODY, color=TEXT):
    return f"""
    QTextEdit {{
        background: transparent;
        border: none;
        color: {color};
        font-family: {FONT_FAMILY};
        font-size: {font_size}px;
        font-weight: 400;
        selection-background-color: {rgba(ACCENT, 90)};
        padding: 0px;
    }}
    QTextEdit::scrollbar:vertical {{
        background: transparent;
        width: 6px;
        border: none;
        margin: 2px 0px 2px 0px;
    }}
    QTextEdit::scrollbar-handle:vertical {{
        background: rgba(255,255,255,0.14);
        border-radius: 3px;
        min-height: 24px;
    }}
    QTextEdit::scrollbar-handle:vertical:hover {{
        background: rgba(255,255,255,0.24);
    }}
    QTextEdit::scrollbar-add-line:vertical, QTextEdit::scrollbar-sub-line:vertical {{
        height: 0px;
    }}
    """


class _Chip(QWidget):
    """A small attachment chip: icon + label + remove."""

    removed = Signal(str)

    def __init__(self, kind: str, label: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.label_text = label
        self.setFixedHeight(26)
        self._hovered = False

    def sizeHint(self):
        return QSize(12 + 14 + 6 + len(self.label_text) * 6.5 + 16 + 8, 26)

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
            self.removed.emit(self.label_text)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        painter.fillPath(path, QColor(BG_3))
        pen = QPen(QColor(BORDER), 1.0)
        painter.setPen(pen)
        painter.drawPath(path)

        icon_px = icons.pixmap(
            {"file": "file", "image": "image", "screenshot": "monitor",
             "context": "target"}.get(self.kind, "file"),
            "#9AA6B8", 13,
        )
        painter.drawPixmap(6, (self.height() - icon_px.height()) // 2, icon_px)

        painter.setPen(QColor(TEXT_SOFT))
        font = painter.font()
        font.setFamily(FONT_FAMILY)
        font.setPointSizeF(8.2)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        painter.drawText(
            QRectF(24, 0, self.width() - 42, self.height()),
            Qt.AlignVCenter | Qt.AlignLeft, self.label_text,
        )

        x_icon = icons.pixmap("x", "#5E6A7D", 10)
        painter.drawPixmap(
            self.width() - x_icon.width() - 6,
            (self.height() - x_icon.height()) // 2, x_icon,
        )


class _SendButton(QWidget):
    """Gradient circular send button, lights up when text is present."""

    clicked = Signal()

    def __init__(self, size=40, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._enabled = False
        self._hovered = False

    def set_enabled_state(self, enabled: bool):
        if enabled != self._enabled:
            self._enabled = enabled
            self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._enabled:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)

        if self._enabled:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor(ACCENT))
            gradient.setColorAt(1.0, QColor(ACCENT_2))
            painter.setBrush(gradient)
            painter.setPen(Qt.NoPen)
            if self._hovered:
                glow = QColor(ACCENT)
                glow.setAlpha(70)
                painter.setBrush(glow)
                painter.drawEllipse(rect.adjusted(-5, -5, 5, 5))
                painter.setBrush(gradient)
        else:
            painter.setBrush(QColor(BG_3))
            painter.setPen(QPen(QColor(BORDER), 1.0))
        painter.drawEllipse(rect)

        icon_px = icons.pixmap(
            "arrow-up", "#FFFFFF" if self._enabled else "#5E6A7D",
            int(self.width() * 0.42),
        )
        painter.drawPixmap(
            (self.width() - icon_px.width()) // 2,
            (self.height() - icon_px.height()) // 2, icon_px,
        )


class ChatInputBar(QWidget):
    """The full-width input dock."""

    submitted = Signal(str)
    attachRequested = Signal()
    voicePressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._focused = False
        self._chips = []
        self._voice_state = "idle"
        self._pulse_phase = 0.0
        self._name = ASSISTANT_NAME

        self.setAttribute(Qt.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -------- card container (painted) --------
        self._card = QWidget(self)
        self._card.setAttribute(Qt.WA_TranslucentBackground)
        outer.addWidget(self._card)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(8, 8, 8, 8)
        card_layout.setSpacing(4)

        # attachment chips row
        self._chip_scroll = QScrollArea(self._card)
        self._chip_scroll.setWidgetResizable(True)
        self._chip_scroll.setFixedHeight(0)
        self._chip_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._chip_scroll.setStyleSheet("background: transparent; border: none;")
        self._chip_host = QWidget()
        self._chip_host.setAttribute(Qt.WA_TranslucentBackground)
        self._chip_host.setStyleSheet("background: transparent;")
        self._chip_layout = QHBoxLayout(self._chip_host)
        self._chip_layout.setContentsMargins(8, 0, 8, 0)
        self._chip_layout.setSpacing(6)
        self._chip_layout.addStretch(1)
        self._chip_scroll.setWidget(self._chip_host)
        card_layout.addWidget(self._chip_scroll)

        # -------- controls row --------
        row = QHBoxLayout()
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(6)

        from .icon_button import IconButton

        self.attach_btn = IconButton(
            "paperclip", size=36, tooltip="Attach files, images or context",
        )
        self.attach_btn.clicked.connect(self.attachRequested.emit)
        row.addWidget(self.attach_btn)

        # text edit + placeholder
        text_wrap = QWidget(self._card)
        text_wrap.setAttribute(Qt.WA_TranslucentBackground)
        text_wrap.setStyleSheet("background: transparent;")
        text_layout = QVBoxLayout(text_wrap)
        text_layout.setContentsMargins(4, 0, 4, 0)
        text_layout.setSpacing(0)

        self.editor = QTextEdit(text_wrap)
        self.editor.setFrameStyle(0)
        self.editor.setStyleSheet(_input_qss())
        self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor.setAcceptRichText(False)
        self.editor.setTabChangesFocus(False)
        self.editor.document().setDocumentMargin(0)
        text_layout.addWidget(self.editor)

        self.placeholder = QLabel(
            f"Message {self._name} — ask anything, or press the mic to talk",
            self.editor,
        )
        self.placeholder.setStyleSheet(
            f"color: {TEXT_FAINT}; font-family: {FONT_FAMILY};"
            f"font-size: {FONT_BODY}px; background: transparent; font-weight: 400;"
        )
        self.placeholder.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._update_placeholder_pos()

        row.addWidget(text_wrap, 1)

        # mic
        self.mic_btn = IconButton("mic", size=36, tooltip="Talk to Nova")
        self.mic_btn.clicked.connect(self.voicePressed.emit)
        row.addWidget(self.mic_btn)

        # send
        self.send_btn = _SendButton(size=40)
        self.send_btn.clicked.connect(self._emit_submit)
        row.addWidget(self.send_btn)

        card_layout.addLayout(row)

        # -------- signals --------
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.cursorPositionChanged.connect(self._update_placeholder_pos)
        self.editor.focusInEvent = self._wrap_focus_in(self.editor)
        self.editor.focusOutEvent = self._wrap_focus_out(self.editor)
        self.editor.keyPressEvent = self._wrap_key(self.editor)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._tick_pulse)

        self._auto_grow()
        self._update_send_state()

    # ------------------------------------------------------------------ #
    # name (driven by settings)
    # ------------------------------------------------------------------ #
    def set_name(self, name: str):
        self._name = (name or "").strip() or ASSISTANT_NAME
        state = self._voice_state
        self._voice_state = None
        self.set_voice_state(state)

    # ------------------------------------------------------------------ #
    # focus handling (wrap QTextEdit events so the card can glow)
    # ------------------------------------------------------------------ #
    def _wrap_focus_in(self, editor):
        def handler(event):
            self._focused = True
            self.update()
            QTextEdit.focusInEvent(editor, event)
        return handler

    def _wrap_focus_out(self, editor):
        def handler(event):
            self._focused = False
            self.update()
            QTextEdit.focusOutEvent(editor, event)
        return handler

    # ------------------------------------------------------------------ #
    # keys — Enter sends, Shift+Enter inserts a newline
    # ------------------------------------------------------------------ #
    def _wrap_key(self, editor):
        def handler(event):
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if not (event.modifiers() & Qt.ShiftModifier):
                    self._emit_submit()
                    event.accept()
                    return
            QTextEdit.keyPressEvent(editor, event)
        return handler

    # ------------------------------------------------------------------ #
    # text / send state
    # ------------------------------------------------------------------ #
    def _on_text_changed(self):
        self._update_placeholder_pos()
        self._update_send_state()
        self._auto_grow()

    def _update_placeholder_pos(self):
        if self.editor.toPlainText() or self._voice_state != "idle":
            self.placeholder.hide()
        else:
            self.placeholder.show()
            self.placeholder.adjustSize()
            rect = self.editor.rect()
            vm = self.editor.viewportMargins()
            x = self.editor.document().documentMargin() + 6
            y = rect.top() + vm.top() + 4
            self.placeholder.move(x, y)

    def _update_send_state(self):
        self.send_btn.set_enabled_state(bool(self.editor.toPlainText().strip()))

    def _auto_grow(self):
        doc = self.editor.document()
        doc_height = int(doc.size().height())
        desired = max(INPUT_H - 16, doc_height + 14)
        if desired >= INPUT_MAX_H - 16:
            desired = INPUT_MAX_H - 16
            self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        else:
            self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor.setMinimumHeight(desired)
        self.editor.setMaximumHeight(desired)
        # vertical centering: pad the viewport so short text sits mid-height
        pad = max(0, (desired - doc_height) // 2)
        self.editor.setViewportMargins(0, pad, 0, pad)
        self.updateGeometry()

    # ------------------------------------------------------------------ #
    # voice state
    # ------------------------------------------------------------------ #
    def set_voice_state(self, state: str):
        self._voice_state = state
        if state == "listening":
            self.mic_btn.set_active(True)
            self.placeholder.setText("Listening… speak now")
            self._pulse_timer.start(30)
        elif state == "thinking":
            self.mic_btn.set_active(False)
            self.placeholder.setText(f"{self._name} is thinking…")
            self._pulse_timer.stop()
        elif state == "speaking":
            self.mic_btn.set_active(False)
            self.placeholder.setText(f"{self._name} is speaking…")
            self._pulse_timer.stop()
        else:
            self.mic_btn.set_active(False)
            self._pulse_timer.stop()
            self.placeholder.setText(
                f"Message {self._name} — ask anything, or press the mic to talk"
            )
        self._update_placeholder_pos()

    def _tick_pulse(self):
        self._pulse_phase = (self._pulse_phase + 0.06) % 1.0
        self.mic_btn.update()

    # ------------------------------------------------------------------ #
    # attachments
    # ------------------------------------------------------------------ #
    def add_attachment(self, kind: str, label: str):
        chip = _Chip(kind, label)
        chip.removed.connect(self.remove_attachment)
        self._chip_layout.insertWidget(self._chip_layout.count() - 1, chip)
        self._chips.append(chip)
        self._chip_scroll.setFixedHeight(34)
        self.updateGeometry()

    def remove_attachment(self, label: str):
        for chip in self._chips:
            if chip.label_text == label:
                self._chip_layout.removeWidget(chip)
                self._chips.remove(chip)
                chip.deleteLater()
                break
        if not self._chips:
            self._chip_scroll.setFixedHeight(0)
        self.updateGeometry()

    def attachments(self):
        return [(c.kind, c.label_text) for c in self._chips]

    def clear_attachments(self):
        for chip in list(self._chips):
            self._chip_layout.removeWidget(chip)
            chip.deleteLater()
        self._chips = []
        self._chip_scroll.setFixedHeight(0)

    # ------------------------------------------------------------------ #
    # submission
    # ------------------------------------------------------------------ #
    def _emit_submit(self):
        text = self.editor.toPlainText().strip()
        if not text:
            return
        extra = []
        for kind, label in self.attachments():
            extra.append(f"[attached {kind}: {label}]")
        if extra:
            text = f"{text}\n\n{' '.join(extra)}"
        self.clear_attachments()
        self.editor.clear()
        self._update_placeholder_pos()
        self.submitted.emit(text)

    def focus_input(self):
        self.editor.setFocus(Qt.OtherFocusReason)
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.editor.setTextCursor(cursor)

    # ------------------------------------------------------------------ #
    # painting — the glass card + glow
    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # outer glow when focused
        if self._focused:
            glow = QColor(ACCENT)
            glow.setAlpha(26 + int(14 * (0.5 - 0.5 * ((self._voice_state == "listening") * 1))))
            painter.setBrush(glow)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(
                QRectF(0, 0, self.width(), self.height()), R_LG + 6, R_LG + 6
            )

        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, R_LG, R_LG)

        # body
        body = QLinearGradient(rect.topLeft(), rect.bottomRight())
        body.setColorAt(0.0, QColor(BG_2))
        body.setColorAt(1.0, QColor(BG_1))
        painter.fillPath(path, body)

        # top highlight
        highlight = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        highlight.setColorAt(0.0, QColor(255, 255, 255, 20))
        highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, highlight)

        border_color = BORDER_FOCUS if self._focused else BORDER
        pen = QPen(QColor(border_color), 1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
