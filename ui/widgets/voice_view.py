"""Body content shown while the panel is in VOICE mode."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout

from ui.styles.theme import METRICS as M
from ui.widgets.waveform import WaveformWidget


class VoiceView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(M.padding_lg, M.padding_md, M.padding_lg, M.padding_lg)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.waveform = WaveformWidget()
        layout.addWidget(self.waveform, alignment=Qt.AlignmentFlag.AlignCenter)

    def start(self) -> None:
        self.waveform.start()

    def stop(self) -> None:
        self.waveform.stop()
