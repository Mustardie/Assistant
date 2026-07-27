"""History dialog.

Displays a list of past exchanges. Call `populate(entries)` with data from
the backend's memory manager (e.g. MemoryManager.get_conversation_history())
once wired up in backend/bridge.py — this widget has no import on that
module itself, keeping UI and backend separate.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QWidget,
)

from ui.styles.theme import PALETTE as P, Fonts, METRICS as M
from ui.styles.qss import dialog_qss, scrollarea_qss
from ui.widgets.icon_button import IconButton


class HistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(380, 420)
        self.setStyleSheet(dialog_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(M.padding_lg, M.padding_md, M.padding_lg, M.padding_lg)
        layout.setSpacing(M.padding_md)

        header = QHBoxLayout()
        title = QLabel("History")
        title.setFont(Fonts.base(16))
        close_btn = IconButton("fa6s.xmark", tooltip="Close")
        close_btn.clicked.connect(self.close)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_btn)
        layout.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet(scrollarea_qss())
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(M.padding_sm)
        self._list_layout.addStretch(1)
        self.scroll.setWidget(self._list_widget)

        layout.addWidget(self.scroll, stretch=1)
        self._empty_state()

    def _empty_state(self) -> None:
        empty = QLabel("No conversation history yet.")
        empty.setFont(Fonts.body(12))
        empty.setStyleSheet(f"color: {P.text_secondary};")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._list_layout.insertWidget(0, empty)

    def populate(self, entries: list[dict]) -> None:
        """entries: list of {"role": "user"|"assistant", "content": str}"""
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not entries:
            self._empty_state()
            return

        for entry in entries:
            role = entry.get("role", "assistant")
            content = str(entry.get("content", ""))
            label = QLabel(content)
            label.setWordWrap(True)
            label.setFont(Fonts.body(12))
            color = P.text_primary if role == "user" else P.text_secondary
            prefix = "You" if role == "user" else "Assistant"
            label.setText(f"{prefix}: {content}")
            label.setStyleSheet(f"color: {color};")
            self._list_layout.insertWidget(self._list_layout.count() - 1, label)
