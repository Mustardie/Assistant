"""Text entry row used in the panel's TEXT mode."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLineEdit

from ui.styles.theme import METRICS as M
from ui.styles.qss import text_input_qss
from ui.widgets.icon_button import SendButton


class TextInputRow(QWidget):
    submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(M.padding_sm)

        self.input = QLineEdit(self)
        self.input.setObjectName("promptInput")
        self.input.setFixedHeight(M.input_height)
        self.input.setPlaceholderText("Ask me anything…")
        self.input.setStyleSheet(text_input_qss())
        self.input.textChanged.connect(self._on_text_changed)
        self.input.returnPressed.connect(self._submit)

        self.send_button = SendButton(diameter=M.input_height)
        self.send_button.clicked.connect(self._submit)

        layout.addWidget(self.input, stretch=1)
        layout.addWidget(self.send_button)

    def _on_text_changed(self, text: str) -> None:
        self.send_button.setEnabled(bool(text.strip()))

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.submitted.emit(text)
        self.input.clear()

    def focus(self) -> None:
        self.input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def clear(self) -> None:
        self.input.clear()
