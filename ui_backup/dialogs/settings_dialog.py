"""Settings dialog.

Purely presentational for now. Populate `sections` with real settings
controls once the backend exposes a settings/config surface — this dialog
intentionally has no knowledge of config.settings or any backend module.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
)

from ui.styles.theme import Fonts, METRICS as M
from ui.styles.qss import dialog_qss
from ui.widgets.icon_button import IconButton


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(360, 320)
        self.setStyleSheet(dialog_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(M.padding_lg, M.padding_md, M.padding_lg, M.padding_lg)
        layout.setSpacing(M.padding_md)

        header = QHBoxLayout()
        title = QLabel("Settings")
        title.setFont(Fonts.base(16, Fonts.base().weight()))
        close_btn = IconButton("fa6s.xmark", tooltip="Close")
        close_btn.clicked.connect(self.close)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_btn)
        layout.addLayout(header)

        placeholder = QLabel(
            "Settings controls will appear here once wired to the "
            "backend's configuration (config.settings)."
        )
        placeholder.setWordWrap(True)
        placeholder.setFont(Fonts.body(12))
        layout.addWidget(placeholder)
        layout.addStretch(1)
