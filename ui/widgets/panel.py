"""The expanded assistant panel.

Owns purely visual/local state (which body view is showing, status text).
All cross-cutting intent is surfaced as Qt signals that ui/main_window.py
listens to and re-emits at the top level — this widget never talks to a
backend directly.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget,
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QStackedLayout,
    QGraphicsDropShadowEffect,
)

from ui.styles.theme import PALETTE as P, METRICS as M, Fonts
from ui.styles.qss import panel_qss, header_label_qss
from ui.widgets.icon_button import IconButton
from ui.widgets.voice_view import VoiceView
from ui.widgets.text_input import TextInputRow

STATE_LABELS = {
    "idle": "Ready",
    "listening": "Listening…",
    "thinking": "Thinking…",
    "speaking": "Speaking…",
    "text": "Type a message",
}


class AssistantPanel(QFrame):
    """Expanded assistant surface. Emits high-level intent signals."""

    voicePressed = Signal()
    textSubmitted = Signal(str)
    historyRequested = Signal()
    settingsRequested = Signal()
    closeRequested = Signal()
    stopListening = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panelSurface")
        self.setStyleSheet(panel_qss())
        self._mode = "text"  # "voice" | "text"

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 140))
        self.setGraphicsEffect(shadow)

        root = QVBoxLayout(self)
        root.setContentsMargins(
            M.padding_md, M.padding_sm, M.padding_md, M.padding_md
        )
        root.setSpacing(M.padding_sm)

        root.addLayout(self._build_header())
        root.addLayout(self._build_body())

    # -- header ------------------------------------------------------ #

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(M.padding_sm)

        self.mode_toggle = IconButton(
            "fa6s.keyboard", tooltip="Switch input mode"
        )
        self.mode_toggle.clicked.connect(self._toggle_mode)

        self.status_dot = QLabel()
        self.status_dot.setFixedSize(8, 8)
        self._set_dot_color(P.idle_dot)

        self.status_label = QLabel(STATE_LABELS["text"])
        self.status_label.setObjectName("statusLabel")
        self.status_label.setFont(Fonts.status())
        self.status_label.setStyleSheet(header_label_qss())

        self.history_btn = IconButton("fa6s.clock-rotate-left", tooltip="History")
        self.settings_btn = IconButton("fa6s.gear", tooltip="Settings")
        self.close_btn = IconButton("fa6s.xmark", tooltip="Close")

        self.history_btn.clicked.connect(self.historyRequested.emit)
        self.settings_btn.clicked.connect(self.settingsRequested.emit)
        self.close_btn.clicked.connect(self.closeRequested.emit)

        header.addWidget(self.mode_toggle)
        header.addSpacing(4)
        header.addWidget(self.status_dot)
        header.addWidget(self.status_label)
        header.addStretch(1)
        header.addWidget(self.history_btn)
        header.addWidget(self.settings_btn)
        header.addWidget(self.close_btn)
        return header

    def _set_dot_color(self, hex_color: str) -> None:
        self.status_dot.setStyleSheet(
            f"background-color: {hex_color}; border-radius: 4px;"
        )

    # -- body -------------------------------------------------------- #

    def _build_body(self) -> QVBoxLayout:
        wrapper = QVBoxLayout()
        wrapper.setContentsMargins(0, 0, 0, 0)

        self.body_container = QWidget()
        self.stack = QStackedLayout(self.body_container)
        self.stack.setContentsMargins(0, 0, 0, 0)

        self.voice_view = VoiceView()
        self.text_input = TextInputRow()
        self.text_input.submitted.connect(self.textSubmitted.emit)

        self.stack.addWidget(self.voice_view)
        self.stack.addWidget(self.text_input)
        self.stack.setCurrentWidget(self.text_input)

        wrapper.addWidget(self.body_container)
        return wrapper

    # -- mode management ----------------------------------------------- #

    def _toggle_mode(self) -> None:
        if self._mode == "text":
            self.voicePressed.emit()
            self.enter_voice_mode()
        else:
            self.stopListening.emit()
            self.enter_text_mode()

    def enter_voice_mode(self) -> None:
        self._mode = "voice"
        self._swap_icon(self.mode_toggle, "fa6s.keyboard")
        self.stack.setCurrentWidget(self.voice_view)
        self.voice_view.start()
        self.set_state("listening")

    def enter_text_mode(self) -> None:
        self._mode = "text"
        self.voice_view.stop()
        self._swap_icon(self.mode_toggle, "fa6s.microphone")
        self.stack.setCurrentWidget(self.text_input)
        self.set_state("text")
        self.text_input.focus()

    @staticmethod
    def _swap_icon(button: IconButton, icon_name: str) -> None:
        button._icon_name = icon_name
        button._apply_icon(button._icon_color)

    # -- state / status ------------------------------------------------- #

    def set_state(self, state: str) -> None:
        """state in {'idle','listening','thinking','speaking','text'}"""
        self.status_label.setText(STATE_LABELS.get(state, state))
        color = {
            "idle": P.idle_dot,
            "listening": P.listening,
            "thinking": P.thinking,
            "speaking": P.speaking,
            "text": P.idle_dot,
        }.get(state, P.idle_dot)
        self._set_dot_color(color)

    @property
    def mode(self) -> str:
        return self._mode
