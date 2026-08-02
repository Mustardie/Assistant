"""
NovaOverlay — the frameless liquid-glass overlay window.

THIS FILE IS UI ONLY. It has no idea how Nova listens, thinks, or speaks.
It talks to the rest of your assistant through two callbacks you pass in:

    on_submit(text: str) -> str
        Called when the user hits Enter in the input bar. Send `text` to
        your existing assistant brain and return its reply as a string.

    on_speak(text: str) -> None
        Called with the reply text as soon as it's shown. Hand this
        straight to your existing TTS system.

Nothing about your memory, tools, or backend logic needs to change --
you just construct this with your own functions:

    from ui.nova_window import NovaOverlay
    overlay = NovaOverlay(on_submit=brain.handle_text, on_speak=tts.speak)
    overlay.install_hotkey()
    app.exec()

States: HIDDEN -> BUBBLE (Ctrl+Space) -> INPUT (click bubble) -> RESPONSE (Enter)
"""
from enum import Enum, auto

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QGuiApplication, QShortcut, QKeySequence
from PySide6.QtWidgets import QWidget

from .widgets.glass_panel import GlassPanel
from .widgets.glass_input import GlassInputBar, GlassResponseLabel
from .animations import animate_geometry, animate_glass_state


class NovaState(Enum):
    HIDDEN = auto()
    BUBBLE = auto()
    INPUT = auto()
    RESPONSE = auto()


# --- exact values pulled from Figma node 6:65 ---
BUBBLE_SIZE = (216, 97)     # Frame 1 (voice-listening bubble)
BUBBLE_RADIUS = 33.0
BUBBLE_BG_ALPHA = 0.02

INPUT_SIZE = (391, 53)      # Frame 2 (text input)
INPUT_RADIUS = 15.0
INPUT_BG_ALPHA = 0.35

ANIM_MS = 380
TOP_MARGIN = 40             # distance from top of screen once dropped down


class NovaOverlay(QWidget):

    def __init__(self, on_submit=None, on_speak=None, parent=None):
        super().__init__(parent)
        self.on_submit = on_submit
        self.on_speak = on_speak
        self.state = NovaState.HIDDEN
        self._shortcut = None

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.panel = GlassPanel(
            self, corner_radius=BUBBLE_RADIUS, bg_alpha=BUBBLE_BG_ALPHA
        )
        self.panel.clicked.connect(self._on_bubble_clicked)

        self.input_bar = GlassInputBar(self.panel)
        self.input_bar.hide()
        self.input_bar.submitted.connect(self._handle_submit)

        self.response_label = GlassResponseLabel(self.panel)
        self.response_label.hide()

        self.resize(*BUBBLE_SIZE)

    # ---------------- layout ----------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.panel.setGeometry(self.rect())
        self.input_bar.setGeometry(self.panel.rect())
        self.response_label.setGeometry(self.panel.rect())

    def _centered_rect(self, size):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        w, h = size
        x = screen.x() + (screen.width() - w) // 2
        y = screen.y() + TOP_MARGIN
        return QRect(x, y, w, h)

    def _above_screen_rect(self, size):
        r = self._centered_rect(size)
        r.moveTop(-size[1] - 20)
        return r

    # ---------------- public API ----------------
    def toggle(self):
        """Ctrl+Space entry point: open if hidden, close otherwise."""
        if self.state == NovaState.HIDDEN:
            self.show_bubble()
        else:
            self.hide_overlay()

    def show_bubble(self):
        """Drop the bubble down smoothly from above the screen (interaction 1)."""
        self.state = NovaState.BUBBLE
        self.input_bar.hide()
        self.response_label.hide()

        self.panel.cornerRadius = BUBBLE_RADIUS
        self.panel.bgAlpha = BUBBLE_BG_ALPHA

        start_rect = self._above_screen_rect(BUBBLE_SIZE)
        target_rect = self._centered_rect(BUBBLE_SIZE)

        self.setGeometry(start_rect)
        self.show()
        self.raise_()
        self.activateWindow()
        self.panel.refresh_backdrop()

        animate_geometry(self, target_rect, duration=ANIM_MS)

    def hide_overlay(self):
        target_rect = self._above_screen_rect((self.width(), self.height()))
        animate_geometry(self, target_rect, duration=ANIM_MS - 60,
                          finished_cb=self._finish_hide)

    def _finish_hide(self):
        self.hide()
        self.state = NovaState.HIDDEN
        self.input_bar.clear()
        self.response_label.set_text("")

    # ---------------- state transitions ----------------
    def _on_bubble_clicked(self):
        """Bubble -> input, with a smooth morph instead of an instant swap (interaction 2)."""
        if self.state != NovaState.BUBBLE:
            return
        self.state = NovaState.INPUT

        target_rect = self._centered_rect(INPUT_SIZE)

        def reveal_input():
            self.input_bar.show()
            self.input_bar.focus_input()
            self.panel.refresh_backdrop()

        animate_geometry(self, target_rect, duration=ANIM_MS, finished_cb=reveal_input)
        animate_glass_state(self.panel, INPUT_RADIUS, INPUT_BG_ALPHA, duration=ANIM_MS)

    def _handle_submit(self, text: str):
        """Enter -> response state (interaction 3), then trigger TTS (interaction 4)."""
        self.input_bar.hide()
        self.state = NovaState.RESPONSE

        reply = ""
        if self.on_submit is not None:
            reply = self.on_submit(text) or ""

        self.response_label.set_text(reply)
        self.response_label.show()
        self.panel.refresh_backdrop()

        if self.on_speak is not None and reply:
            self.on_speak(reply)

    # ---------------- keyboard ----------------
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide_overlay()
        else:
            super().keyPressEvent(event)

    # ---------------- hotkey ----------------
    def install_hotkey(self):
        """
        Wires Ctrl+Space -> toggle() while Nova's own window has focus
        (via QShortcut). This is enough for testing and for the common
        case where Nova is the active window when you invoke it.

        For a TRUE global hotkey that works even when some other app is
        focused, you need an OS-level hook -- Qt doesn't provide one.
        Common options (kept out of this UI-only module deliberately,
        since they carry OS-specific permission requirements):

            pip install keyboard
            import keyboard
            keyboard.add_hotkey("ctrl+space", lambda: overlay.toggle())
            # (call this from a background thread; hop back onto the Qt
            #  thread with QTimer.singleShot(0, overlay.toggle) if needed)

        Swap in whichever global-hotkey approach fits your existing
        assistant's OS/security model.
        """
        self._shortcut = QShortcut(QKeySequence("Ctrl+Space"), self)
        self._shortcut.activated.connect(self.toggle)
