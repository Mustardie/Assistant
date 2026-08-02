"""
Content widgets placed *inside* a GlassPanel.

GlassInputBar reproduces the Figma "Frame 2" content spec:
    flex row, padding: 0 6px, gap: 10px
    placeholder "Type here": Inter 12px / 700 / #FFF

GlassResponseLabel is the response-display state. No exact spec was given
for it in Figma, so it reuses the same panel styling and just swaps the
content for word-wrapped response text.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QLabel


class GlassInputBar(QWidget):
    """The 391x53 text-input content (lives inside the panel once morphed)."""

    submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(10)

        self.line_edit = QLineEdit(self)
        self.line_edit.setPlaceholderText("Type here")
        self.line_edit.setFrame(False)
        self.line_edit.setAttribute(Qt.WA_TranslucentBackground)
        self.line_edit.setStyleSheet(
            """
            QLineEdit {
                background: transparent;
                border: none;
                color: #FFFFFF;
                font-family: 'Inter';
                font-size: 12px;
                font-weight: 700;
                padding: 0 4px;
            }
            """
        )
        layout.addWidget(self.line_edit)
        self.line_edit.returnPressed.connect(self._on_return)

    def _on_return(self):
        text = self.line_edit.text().strip()
        if text:
            self.submitted.emit(text)

    def focus_input(self):
        self.line_edit.setFocus(Qt.OtherFocusReason)

    def clear(self):
        self.line_edit.clear()


class GlassResponseLabel(QWidget):
    """Response-display content: word-wrapped text inside the same glass shape."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)

        self.label = QLabel("", self)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet(
            """
            QLabel {
                color: #FFFFFF;
                font-family: 'Inter';
                font-size: 13px;
                font-weight: 600;
                background: transparent;
            }
            """
        )
        layout.addWidget(self.label)

    def set_text(self, text: str):
        self.label.setText(text)
