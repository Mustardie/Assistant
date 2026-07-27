"""Top-level assistant window.

This is the single point of contact for a backend integration: it exposes
exactly the seven signals requested (voicePressed, textSubmitted,
historyRequested, settingsRequested, closeRequested, activateAssistant,
stopListening) and nothing else backend-shaped. Internally it owns the
bubble<->panel morph animation and forwards the panel's local signals
up to this level.

Backend wiring happens entirely in backend/bridge.py + main.py — this
module has zero imports from backend/.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QPoint, QTimer, Signal, QEasingCurve
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from ui.styles.theme import METRICS as M, MOTION
from ui.animations.transitions import animate_geometry, animate_window_opacity
from ui.widgets.bubble import FloatingBubble
from ui.widgets.panel import AssistantPanel

TOP_MARGIN = 28


class AssistantWindow(QWidget):
    # -- the seven required backend-facing signals ----------------------- #
    voicePressed = Signal()
    textSubmitted = Signal(str)
    historyRequested = Signal()
    settingsRequested = Signal()
    closeRequested = Signal()
    activateAssistant = Signal()
    stopListening = Signal()

    # internal, thread-safe hotkey trampoline (see trigger_hotkey())
    _hotkeyTriggered = Signal()

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)

        self._expanded = False
        self._animating = False

        self.bubble = FloatingBubble(self)
        self.panel = AssistantPanel(self)
        self.panel.hide()

        self.bubble.clicked.connect(self._on_bubble_clicked)
        self._hotkeyTriggered.connect(self._on_hotkey_triggered)

        self.panel.textSubmitted.connect(self.textSubmitted.emit)
        self.panel.historyRequested.connect(self.historyRequested.emit)
        self.panel.settingsRequested.connect(self.settingsRequested.emit)
        self.panel.voicePressed.connect(self.voicePressed.emit)
        self.panel.stopListening.connect(self._on_stop_listening)
        self.panel.closeRequested.connect(self._on_close_requested)

        self._place_at_idle_anchor()

    # -- public entry points for a backend / main.py --------------------- #

    def trigger_hotkey(self) -> None:
        """Thread-safe entry point for a global hotkey callback (e.g. the
        `keyboard` library's hotkey thread). Safe to call from any thread:
        Qt auto-queues delivery to the GUI thread since this window lives
        there."""
        self._hotkeyTriggered.emit()

    def set_state(self, state: str) -> None:
        """state in {'idle', 'listening', 'thinking', 'speaking'}.
        Call this from the backend (e.g. the existing on_listening /
        on_transcribing / on_thinking / on_speaking / on_idle callbacks in
        main.py) to reflect real assistant state in the UI."""
        self.bubble.set_state(state)
        if self._expanded:
            self.panel.set_state(state)
        if state == "idle" and self._expanded and self.panel.mode == "voice":
            self.panel.voice_view.stop()

    def show_history(self, entries: list[dict]) -> None:
        from ui.dialogs.history_dialog import HistoryDialog

        dialog = HistoryDialog(self)
        dialog.populate(entries)
        self._show_dialog_near_panel(dialog)

    def show_settings(self) -> None:
        from ui.dialogs.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self)
        self._show_dialog_near_panel(dialog)

    def _show_dialog_near_panel(self, dialog: QWidget) -> None:
        anchor = self.geometry()
        x = anchor.center().x() - dialog.width() // 2
        y = anchor.bottom() + 12
        dialog.move(max(0, x), y)
        dialog.show()

    # -- internal reactions ------------------------------------------------ #

    def _on_bubble_clicked(self) -> None:
        if self._animating:
            return
        self.expand(mode="text")

    def _on_hotkey_triggered(self) -> None:
        if self._animating:
            return

        self.show()
        self.raise_()
        self.activateWindow()

        if self._expanded and self.panel.mode == "voice":
            self._on_stop_listening()
            return

        self.activateAssistant.emit()
        self.expand(mode="voice")

    def _on_stop_listening(self) -> None:
        self.stopListening.emit()
        if self._expanded:
            self.panel.enter_text_mode()
        self.set_state("idle")

    def _on_close_requested(self) -> None:
        self.closeRequested.emit()
        self.hide()

    # -- layout anchoring ------------------------------------------------- #

    def _screen_geometry(self):
        screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

    def _place_at_idle_anchor(self) -> None:
        screen = self._screen_geometry()
        d = M.bubble_diameter
        x = screen.center().x() - d // 2
        y = screen.top() + TOP_MARGIN
        self.setGeometry(QRect(x, y, d, d))
        self.bubble.setGeometry(0, 0, d, d)

    def _idle_rect(self) -> QRect:
        screen = self._screen_geometry()
        d = M.bubble_diameter
        x = screen.center().x() - d // 2
        y = screen.top() + TOP_MARGIN
        return QRect(x, y, d, d)

    def _expanded_rect(self) -> QRect:
        screen = self._screen_geometry()
        w = M.panel_width
        h = self.panel.sizeHint().height() or M.panel_min_height
        h = max(M.panel_min_height, min(h, M.panel_max_height))
        x = screen.center().x() - w // 2
        y = screen.top() + TOP_MARGIN
        return QRect(x, y, w, h)

    def resizeEvent(self, event) -> None:  # noqa: N802
        # Whichever surface is showing should always exactly fill the window.
        self.bubble.setGeometry(self._local_bubble_rect())
        self.panel.setGeometry(self.rect())
        super().resizeEvent(event)

    def _local_bubble_rect(self) -> QRect:
        d = M.bubble_diameter
        cx = self.rect().center().x()
        return QRect(cx - d // 2, 0, d, d)

    # -- expand / collapse morph ------------------------------------------ #

    def expand(self, mode: str = "text") -> None:
        if self._animating:
            return
        if self._expanded:
            # already open — just switch mode
            if mode == "voice":
                self.panel.enter_voice_mode()
            else:
                self.panel.enter_text_mode()
            return

        self._animating = True
        self._expanded = True
        self.show()
        self.raise_()

        start_rect = self._idle_rect()
        end_rect = self._expanded_rect()

        # Stop the (about to fade out) bubble from eating clicks meant for
        # the panel that's morphing in underneath it.
        self.bubble.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # Window-level fade-out timed to finish exactly as the resize does,
        # so there's no gap where the window is blank mid-resize.
        animate_window_opacity(
            self, 1.0, 0.0, duration=MOTION.expand_ms,
            on_finished=self.bubble.hide,
        )

        def on_geometry_done() -> None:
            self.panel.show()
            if mode == "voice":
                self.panel.enter_voice_mode()
            else:
                self.panel.enter_text_mode()
            animate_window_opacity(self, 0.0, 1.0, duration=MOTION.fade_ms)
            self._animating = False

        animate_geometry(
            self,
            start_rect,
            end_rect,
            duration=MOTION.expand_ms,
            easing=QEasingCurve.Type.OutCubic,
            on_finished=on_geometry_done,
        )

    def collapse(self) -> None:
        if self._animating or not self._expanded:
            return

        self._animating = True
        self.panel.voice_view.stop()

        start_rect = self.geometry()
        end_rect = self._idle_rect()

        animate_window_opacity(self, 1.0, 0.0, duration=MOTION.collapse_ms)

        def on_geometry_done() -> None:
            self.panel.hide()
            self._expanded = False
            self.bubble.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            self.bubble.show()
            self.bubble.set_state("idle")
            animate_window_opacity(self, 0.0, 1.0, duration=MOTION.fade_ms)
            self._animating = False

        animate_geometry(
            self,
            start_rect,
            end_rect,
            duration=MOTION.collapse_ms,
            easing=QEasingCurve.Type.InCubic,
            on_finished=on_geometry_done,
        )

    # -- keyboard ----------------------------------------------------------- #

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            if self.panel.mode == "voice":
                self._on_stop_listening()
            elif self._expanded:
                self.collapse()
        super().keyPressEvent(event)
