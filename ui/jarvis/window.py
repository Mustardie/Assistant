"""Voice-first JARVIS shell with animated core and bounded widgets."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QTimer, QUrl, Signal, Slot, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QWidget

from config.paths import get_nova_app_file
from ui.jarvis.controller import JarvisUIController
from ui.jarvis.controls import AnimatedIconButton, WidgetPalette
from ui.jarvis.core import JarvisCore
from ui.jarvis.events import JarvisEvent, JarvisEventBus, JarvisEventType
from ui.jarvis.manager import WidgetManager
from ui.jarvis.models import JarvisState
from ui.jarvis.registry import build_default_registry
from ui.jarvis.styles import BG, FONT, TEXT, TEXT_FAINT, TEXT_SOFT
from ui.jarvis.settings_view import JarvisSettingsView
from ui.jarvis.workspace import WidgetWorkspace

logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_FILE = _ROOT / "data" / "nova_settings.json"


def _ensure_font_database() -> None:
    if QFontDatabase.families():
        return
    candidate = Path(r"C:\Windows\Fonts\segoeui.ttf")
    if candidate.is_file():
        QFontDatabase.addApplicationFont(str(candidate))


class _WindowEventRelay(QObject):
    eventReceived = Signal(object)


class JarvisWindow(QWidget):
    textSubmitted = Signal(str)
    voicePressed = Signal()
    settingsChanged = Signal(dict)
    confirmationResolved = Signal(str, bool)
    taskCancelled = Signal()
    widgetAction = Signal(str, str, object)
    agentEvent = Signal(str, object)
    runtimeStatus = Signal(object)
    widgetBackendResult = Signal(str, object)

    def __init__(self, parent=None, *, layout_path: Path | str | None = None, demo_mode: bool = False):
        super().__init__(parent)
        _ensure_font_database()
        self.setWindowTitle("JARVIS Intelligence Interface")
        self.setMinimumSize(1000, 650)
        self.resize(1360, 820)
        self.setStyleSheet(f"font-family:'{FONT}'; background:transparent;")
        self.events = JarvisEventBus()
        self.registry = build_default_registry()
        self.widget_manager = WidgetManager(
            self.registry,
            event_bus=self.events,
            layout_path=layout_path or get_nova_app_file("jarvis_widget_layout.json"),
        )
        self.controller = JarvisUIController(self.widget_manager, self.events)
        self.workspace = WidgetWorkspace(self.widget_manager, self.events, self)
        self.workspace.setGeometry(self.rect())
        self.core = JarvisCore(self.workspace)
        self.core.activated.connect(self._voice_toggle)
        self.workspace.chatSubmitted.connect(self._chat_from_widget)
        self.workspace.taskCancelled.connect(self.taskCancelled)
        self.workspace.confirmationResolved.connect(self._confirmation_resolved)
        self.workspace.widgetAction.connect(self._on_workspace_widget_action)
        self.agentEvent.connect(self.controller.handle_agent_event)
        self.runtimeStatus.connect(lambda values: self.update_system_status(**dict(values or {})))
        self.widgetBackendResult.connect(self._apply_widget_backend_result)
        self._pending_widget_confirmations: dict[str, str] = {}

        self.brand = QLabel("J  /  A  /  R  /  V  /  I  /  S", self)
        self.brand.setStyleSheet(f"color:{TEXT}; font-size:14px; font-weight:600; letter-spacing:5px; background:transparent;")
        self.mode = QLabel("LOCAL INTELLIGENCE INTERFACE", self)
        self.mode.setStyleSheet(f"color:{TEXT_FAINT}; font-size:9px; letter-spacing:2px; background:transparent;")
        self.clock = QLabel(self)
        self.clock.setAlignment(Qt.AlignRight)
        self.clock.setStyleSheet(f"color:{TEXT_SOFT}; font-size:10px; letter-spacing:1px; background:transparent;")
        self.transcript = QLabel("Tap the core or microphone to begin", self)
        self.transcript.setAlignment(Qt.AlignCenter)
        self.transcript.setWordWrap(True)
        self.transcript.setStyleSheet(f"color:{TEXT_SOFT}; font-size:13px; background:transparent;")
        self.state_label = QLabel("STANDBY · VOICE FIRST", self)
        self.state_label.setAlignment(Qt.AlignCenter)
        self.state_label.setStyleSheet(f"color:{TEXT_FAINT}; font-size:9px; letter-spacing:2px; background:transparent;")

        self.chat_button = AnimatedIconButton("chat", tooltip="Open JARVIS console", parent=self)
        self.status_button = AnimatedIconButton("monitor", tooltip="Open system matrix", parent=self)
        self.catalog_button = AnimatedIconButton("menu", tooltip="Open widget matrix", parent=self)
        self.voice_button = AnimatedIconButton("mic", size=54, tooltip="Start or stop listening", parent=self)
        self.settings_button = AnimatedIconButton("gear", tooltip="Open all JARVIS settings", parent=self)
        self.reset_button = AnimatedIconButton("refresh", tooltip="Reset widget layout", parent=self)
        self.chat_button.clicked.connect(lambda: self.request_widget("chat"))
        self.status_button.clicked.connect(lambda: self.request_widget("system_status"))
        self.catalog_button.clicked.connect(self.toggle_palette)
        self.voice_button.clicked.connect(self._voice_toggle)
        self.settings_button.clicked.connect(self.toggle_settings)
        self.reset_button.clicked.connect(self._reset_layout)

        self.palette = WidgetPalette(self.registry.all(), self)
        self.palette.widgetSelected.connect(self._palette_selected)
        self.palette.closeRequested.connect(self.close_palette)
        self.palette.hide()
        self.settings_panel = JarvisSettingsView(self)
        self.settings_panel.closeRequested.connect(self.close_settings)
        self.settings_panel.changed.connect(self._on_settings_changed)
        self.settings_panel.actionRequested.connect(self._settings_action)
        self.settings_panel.hide()
        self._overlay_animations: list[QPropertyAnimation] = []
        self._settings = self._load_settings()
        self.settings_panel.load(self._settings)
        self._settings = self.settings_panel.settings()
        self._sync_connector_environment(self._settings)
        self._settings_emit_timer = QTimer(self)
        self._settings_emit_timer.setSingleShot(True)
        self._settings_emit_timer.timeout.connect(lambda: self.settingsChanged.emit(dict(self._settings)))
        self._apply_ui_settings(self._settings)

        self._relay = _WindowEventRelay(self)
        self._relay.eventReceived.connect(self._handle_event)
        self._unsubscribe = self.events.subscribe("*", self._relay.eventReceived.emit)
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

        self.workspace.restore()
        status = self.widget_manager.find_type("system_status")
        if status is None:
            status = self.widget_manager.create("system_status", position=(26, 92))
        self.widget_manager.update(
            status.widget_id,
            data={
                "model": "Initializing runtime…",
                "mic": "Initializing…",
                "stt": "Loading speech model…",
                "tts": "Loading voice…",
                "connectors": "Discovering…",
                "task": "Idle",
                "warning": "Runtime status updates as each subsystem becomes ready.",
            },
            error=None,
        )
        if demo_mode:
            self.load_demo()
        self._layout_current_geometry()

    def _load_settings(self) -> dict:
        values = dict(JarvisSettingsView.DEFAULTS)
        values["models"] = dict(JarvisSettingsView.DEFAULTS["models"])
        values["api_keys"] = dict(JarvisSettingsView.DEFAULTS["api_keys"])
        try:
            raw = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8")) if _SETTINGS_FILE.exists() else {}
            for key, value in raw.items():
                if key == "models" and isinstance(value, dict):
                    values["models"].update(value)
                elif key == "api_keys" and isinstance(value, dict):
                    values["api_keys"].update(value)
                elif key in JarvisSettingsView.DEFAULTS:
                    values[key] = value
        except Exception:
            logger.exception("Unable to load JARVIS settings")
        return values

    def _on_settings_changed(self, settings: dict):
        self._settings = dict(settings)
        try:
            _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = _SETTINGS_FILE.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self._settings, ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(_SETTINGS_FILE)
        except Exception:
            logger.exception("Unable to save JARVIS settings")
        self._sync_connector_environment(self._settings)
        self._apply_ui_settings(self._settings)
        self._settings_emit_timer.start(450)

    @staticmethod
    def _sync_connector_environment(settings: dict) -> None:
        connector_environment = {
            "discord_bot_token": "JARVIS_DISCORD_BOT_TOKEN",
            "discord_default_channel": "JARVIS_DISCORD_DEFAULT_CHANNEL",
            "whatsapp_access_token": "JARVIS_WHATSAPP_ACCESS_TOKEN",
            "whatsapp_phone_number_id": "JARVIS_WHATSAPP_PHONE_NUMBER_ID",
            "whatsapp_api_version": "JARVIS_WHATSAPP_API_VERSION",
        }
        for setting_name, environment_name in connector_environment.items():
            value = str(settings.get(setting_name) or "").strip()
            if value:
                os.environ[environment_name] = value
            else:
                os.environ.pop(environment_name, None)
        try:
            from connectors.defaults import reset_default_registry

            reset_default_registry()
        except Exception:
            logger.exception("Unable to reload connector configuration")

    def _apply_ui_settings(self, settings: dict):
        animate = bool(settings.get("core_animation", True)) and not bool(settings.get("reduced_motion", False))
        self.core.set_animation_enabled(animate)
        self.transcript.setVisible(bool(settings.get("show_live_transcript", True)))
        self.controller.auto_widgets = bool(settings.get("auto_open_widgets", True))
        self.workspace.snap_enabled = bool(settings.get("layout_snap", True))
        self.workspace.set_hover_effects(bool(settings.get("widget_hover_effects", True)) and not bool(settings.get("reduced_motion", False)))
        always_on_top = bool(settings.get("always_on_top", False))
        if bool(self.windowFlags() & Qt.WindowStaysOnTopHint) != always_on_top:
            visible = self.isVisible()
            self.setWindowFlag(Qt.WindowStaysOnTopHint, always_on_top)
            if visible:
                self.show()
        name = (settings.get("assistant_name") or "JARVIS").strip()
        self.setWindowTitle(f"{name} Intelligence Interface")

    def load_demo(self) -> None:
        self.widget_manager.create(
            "chat", position=(850, 92), size=(470, 350),
            data={"messages": [{"role": "assistant", "text": "Voice is primary. This console is available for detailed work."}]},
        )
        self.widget_manager.create(
            "task_progress", position=(28, 430),
            data={"goal": "Demonstrate the JARVIS workspace", "status": "executing", "progress": 62,
                  "steps": [{"label": "Initialize intelligence core", "status": "complete"},
                            {"label": "Restore widget layout", "status": "complete"},
                            {"label": "Await a voice request", "status": "active"}]},
        )
        self.controller.set_state(JarvisState.IDLE, detail="Demo mode · backends are not contacted")

    def request_widget(self, widget_type: str, **kwargs):
        if widget_type == "settings":
            self.open_settings()
            return None
        if widget_type == "weather":
            data = dict(kwargs.get("data") or {})
            data.setdefault("location", self._settings.get("weather_location", ""))
            kwargs["data"] = data
        state = self.widget_manager.create(widget_type, **kwargs)
        auto_refresh = {
            "system_status", "calendar", "reminders", "notes", "app_launcher",
            "transfers", "notifications", "email", "connectors", "tool_inspector",
            "plan_inspector", "automation", "skills", "system_monitor", "activity",
        }
        if widget_type == "weather" and state.data.get("location"):
            auto_refresh.add("weather")
        if widget_type in auto_refresh:
            QTimer.singleShot(0, lambda wid=state.widget_id: self.widgetAction.emit(wid, "refresh", {}))
        return state

    def _on_workspace_widget_action(self, widget_id: str, action: str, payload: dict):
        state = self.widget_manager.get(widget_id)
        if state is None:
            return
        widget_type = state.widget_type
        values = dict(payload or {})
        if action in {"interrupt", "cancel"} and widget_type in {"chat", "task_progress", "plan_inspector"}:
            self.taskCancelled.emit()
            self.widget_manager.update(widget_id, data={"status": "cancel_requested", "notice": "Cancellation requested"}, loading=False)
            return
        if action == "open" and widget_type in {"video_player", "audio_player", "media_review"}:
            filters = {
                "video_player": "Video files (*.mp4 *.mov *.mkv *.avi *.webm);;All files (*)",
                "audio_player": "Audio files (*.mp3 *.wav *.flac *.m4a *.ogg);;All files (*)",
                "media_review": "Media files (*.mp4 *.mov *.mkv *.avi *.webm);;All files (*)",
            }
            path, _chosen = QFileDialog.getOpenFileName(self, "Open local media", "", filters[widget_type])
            if path:
                self.widget_manager.update(widget_id, data={"path": path, "status": "Local media loaded"}, error=None)
            return
        if action == "open" and widget_type == "file_search":
            path = values.get("path") or values.get("selected")
            if isinstance(path, dict):
                path = path.get("path")
            if not path or not Path(path).exists():
                self.widget_manager.update(widget_id, error=f"Path not found: {path}")
                return
            values["path"] = str(path)
        if action == "reveal" and widget_type == "file_search":
            path = values.get("path") or values.get("selected")
            if isinstance(path, dict):
                path = path.get("path")
            target = Path(path) if path else None
            if not target or not target.exists():
                self.widget_manager.update(widget_id, error=f"Path not found: {path}")
                return
            values["path"] = str(target)
        if action in {"open", "open_file"} and widget_type in {"web_results", "email", "code_task"}:
            selected = values.get("selected")
            if widget_type == "code_task":
                path = selected.get("path") if isinstance(selected, dict) else selected
                if not path or not Path(path).exists():
                    self.widget_manager.update(widget_id, error="Choose an existing file first.")
                    return
                values["path"] = str(path)
                self.widgetAction.emit(widget_id, action, values)
                return
            url = selected.get("url") if isinstance(selected, dict) else selected
            if widget_type == "email" and not url:
                url = "https://mail.google.com/"
            if url:
                QDesktopServices.openUrl(QUrl(str(url)))
                self.widget_manager.update(widget_id, data={"notice": "Opened in your browser"}, error=None)
            else:
                self.widget_manager.update(widget_id, error="This result has no URL to open.")
            return
        if action in {"copy", "copy_report"}:
            text = str(values.get("text") or state.data.get("report") or state.data.get("text") or "")
            QApplication.clipboard().setText(text)
            self.widget_manager.update(widget_id, data={"status": "Copied to clipboard"}, error=None)
            return
        if widget_type == "clipboard" and action == "refresh":
            values["text"] = QApplication.clipboard().text()
        if widget_type == "voice_transcript" and action == "correct":
            text = str(values.get("text") or "").strip()
            if text:
                self.transcript.setText(text)
                self._chat_from_widget(text)
            return
        self.widgetAction.emit(widget_id, action, values)

    @Slot(str, object)
    def _apply_widget_backend_result(self, widget_id: str, result: dict):
        state = self.widget_manager.get(widget_id)
        if state is None:
            return
        result = dict(result or {})
        data = dict(result.get("data") or {})
        if result.get("notice"):
            data["notice"] = result["notice"]
        error = None if result.get("success") else str(result.get("error") or "Widget backend failed")
        self.widget_manager.update(widget_id, loading=False, empty=False, error=error, data=data)
        if result.get("open_settings"):
            self.open_settings()
        confirmation = result.get("confirmation")
        if isinstance(confirmation, dict):
            confirmation_id = f"widget:{widget_id}:{time.time_ns()}"
            pending = str(result.get("pending_prompt") or "")
            if pending:
                self._pending_widget_confirmations[confirmation_id] = pending
            self.controller.confirmation_required(
                str(confirmation.get("action") or "Sensitive widget action"),
                str(confirmation.get("risk") or "Changes external state"),
                str(confirmation.get("target") or "Not specified"),
                confirmation_id=confirmation_id,
            )
        elif result.get("prompt"):
            self._chat_from_widget(str(result["prompt"]))

    def update_system_status(self, **values) -> None:
        state = self.widget_manager.find_type("system_status")
        if state is None:
            state = self.widget_manager.create("system_status", position=(26, 92))
        error = values.pop("error", state.error)
        self.widget_manager.update(state.widget_id, data=values, error=error)

    def append_user(self, text: str, *, voice: bool = False) -> None:
        self.controller.user_message(text, voice=voice)

    def begin_reply(self) -> None:
        self.controller.set_state(JarvisState.THINKING)

    def append_assistant(self, text: str) -> None:
        self.controller.assistant_message(text)
        QTimer.singleShot(1200, lambda: self.controller.set_state(JarvisState.IDLE))

    def set_voice_state(self, state: str) -> None:
        self.controller.set_state(state)
        labels = {
            "listening": {"mic": "Listening now", "task": "Voice capture"},
            "thinking": {"mic": "Ready", "task": "Processing request"},
            "speaking": {"mic": "Ready", "task": "Speaking response"},
            "idle": {"mic": "Ready", "task": "Idle"},
        }
        if state in labels:
            self.update_system_status(**labels[state])

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def toggle(self) -> None:
        self.hide() if self.isVisible() else self.show_and_raise()

    def _chat_from_widget(self, text: str) -> None:
        if self.controller.auto_widgets:
            self.controller.route_request(text)
        self.transcript.setText(text)
        self.controller.set_state(JarvisState.THINKING)
        self.textSubmitted.emit(text)

    def _voice_toggle(self) -> None:
        if self.core.state != JarvisState.LISTENING:
            self.core.trigger_listening()
            self.voice_button.set_active(True)
            self.transcript.setText("Listening… speak naturally")
            self.events.publish(JarvisEventType.USER_VOICE_STARTED, source="ui")
        else:
            self.transcript.setText("Stopping voice capture…")
            self.events.publish(JarvisEventType.USER_VOICE_FINISHED, source="ui")
        self.voicePressed.emit()

    def _confirmation_resolved(self, confirmation_id: str, approved: bool) -> None:
        self.events.publish(JarvisEventType.CONFIRMATION_RESOLVED, {"confirmation_id": confirmation_id, "approved": approved}, source="ui")
        state = self.widget_manager.find_type("confirmation")
        if state:
            self.widget_manager.update(state.widget_id, data={"resolved": True, "approved": approved})
        self.controller.set_state(JarvisState.THINKING if approved else JarvisState.IDLE)
        pending_prompt = self._pending_widget_confirmations.pop(confirmation_id, None)
        if pending_prompt is not None:
            if approved:
                self._chat_from_widget(pending_prompt)
            return
        self.confirmationResolved.emit(confirmation_id, approved)

    @Slot(object)
    def _handle_event(self, event: JarvisEvent) -> None:
        if event.event_type == JarvisEventType.JARVIS_STATE_CHANGED.value:
            state = event.payload.get("state", "idle")
            self.core.set_state(state, event.payload.get("detail", ""))
            self.voice_button.set_active(state == "listening")
            detail = event.payload.get("detail") or state.replace("_", " ")
            self.state_label.setText(str(detail).upper())
        elif event.event_type == JarvisEventType.TRANSCRIPT_UPDATED.value:
            self.transcript.setText(str(event.payload.get("text") or ""))
        elif event.event_type == JarvisEventType.CONFIRMATION_REQUIRED.value:
            self.controller.handle_agent_event("confirmation_required", event.payload)

    def _palette_selected(self, widget_type: str):
        self.request_widget(widget_type)
        if widget_type != "settings":
            self.close_palette()

    def toggle_palette(self):
        self.close_settings()
        if self.palette.isVisible():
            self.close_palette()
        else:
            self.palette.show()
            self.palette.raise_()
            self.palette.search.setFocus()
            self._layout_overlays()

    def close_palette(self):
        self.palette.hide()

    def toggle_settings(self):
        self.close_palette()
        self.close_settings() if self.settings_panel.isVisible() else self.open_settings()

    def open_settings(self):
        self.close_palette()
        self.settings_panel.load(self._settings)
        self.settings_panel.show()
        self.settings_panel.raise_()
        self._layout_overlays()

    def close_settings(self):
        self.settings_panel.hide()

    def _settings_action(self, action: str, payload: dict):
        if action == "open_widget":
            self.close_settings()
            self.request_widget(str((payload or {}).get("widget_type") or "system_status"))
        elif action == "test_voice":
            self.close_settings()
            self._voice_toggle()
        elif action == "test_connector":
            self.close_settings()
            state = self.request_widget("connectors")
            if state is not None:
                self.widget_manager.update(state.widget_id, loading=True, error=None)
                self.widgetAction.emit(
                    state.widget_id,
                    "connect",
                    {"selected": {"name": str((payload or {}).get("name") or "")}},
                )
        else:
            self.widgetAction.emit("settings", action, dict(payload or {}))

    def _layout_overlays(self):
        top, bottom, inset = 84, 86, 18
        height = max(420, self.height() - top - bottom)
        palette_width = min(560, self.width() - inset * 2)
        settings_width = min(780, self.width() - inset * 2)
        self.palette.setGeometry(self.width() - palette_width - inset, top, palette_width, height)
        self.settings_panel.setGeometry(self.width() - settings_width - inset, top, settings_width, height)
        if self.palette.isVisible():
            self.palette.raise_()
        if self.settings_panel.isVisible():
            self.settings_panel.raise_()

    def _reset_layout(self) -> None:
        self.widget_manager.reset_layout()
        self.widget_manager.create("system_status", position=(26, 92), data={"model": "Runtime active", "mic": "Ready", "task": "Idle"})

    def _update_clock(self):
        self.clock.setText(time.strftime("%H:%M:%S   ·   %d %b %Y").upper())

    def _layout_current_geometry(self):
        width, height = self.width(), self.height()
        self.workspace.setGeometry(0, 0, width, height)
        core_size = min(440, max(320, int(min(width, height) * 0.52)))
        self.core.setGeometry((width - core_size) // 2, (height - core_size) // 2 - 25, core_size, core_size)
        self.core.lower()
        self.brand.setGeometry(28, 20, 390, 28)
        self.mode.setGeometry(29, 49, 360, 20)
        self.clock.setGeometry(width - 360, 23, 330, 24)
        self.transcript.setGeometry(width // 2 - 300, height - 146, 600, 36)
        self.state_label.setGeometry(width // 2 - 240, height - 108, 480, 22)
        buttons = [self.chat_button, self.status_button, self.catalog_button, self.voice_button, self.settings_button, self.reset_button]
        total = sum(button.width() for button in buttons) + 10 * (len(buttons) - 1)
        x = (width - total) // 2
        dock_y = height - 70
        for button in buttons:
            button.move(x, dock_y + (54 - button.height()) // 2)
            x += button.width() + 10
        self._layout_overlays()
        for control in (self.brand, self.mode, self.clock, self.transcript, self.state_label, *buttons):
            control.raise_()

    def resizeEvent(self, event):
        self._layout_current_geometry()
        super().resizeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self.settings_panel.isVisible():
                self.close_settings()
            elif self.palette.isVisible():
                self.close_palette()
            else:
                self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor("#03070C"))
        gradient.setColorAt(0.55, QColor(BG))
        gradient.setColorAt(1, QColor("#07131D"))
        painter.fillRect(self.rect(), gradient)
        painter.setPen(QPen(QColor(82, 183, 199, 12), 1))
        for x in range(0, self.width(), 42):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), 42):
            painter.drawLine(0, y, self.width(), y)
        painter.setPen(QPen(QColor(103, 228, 238, 45), 1))
        painter.drawLine(28, 73, self.width() - 28, 73)
        painter.drawLine(28, self.height() - 80, self.width() - 28, self.height() - 80)
