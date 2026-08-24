"""Native settings surface for the JARVIS intelligence interface.

This deliberately does not reuse the legacy Nova settings panel.  It keeps
the same persisted schema so existing choices continue to work, while using
the JARVIS palette, navigation, and interaction language throughout.
"""

from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ui.jarvis.controls import AnimatedIconButton
from ui.jarvis.styles import CYAN, FONT, TEXT, TEXT_FAINT, TEXT_SOFT, button_style


PROVIDERS = ["Ollama", "OpenRouter", "Gemini", "OpenAI", "Anthropic", "Groq", "DeepSeek"]
PROVIDER_MODELS = {
    "Ollama": "qwen2.5vl:7b",
    "OpenRouter": "meta-llama/llama-3.3-70b-instruct",
    "Gemini": "gemini-2.5-flash",
    "OpenAI": "gpt-4o-mini",
    "Anthropic": "claude-3-5-haiku-latest",
    "Groq": "llama-3.3-70b-versatile",
    "DeepSeek": "deepseek-chat",
}
PIPER_VOICES = [
    ("Ryan (high)", "Ryan — en_US-ryan-high"),
    ("Amy (medium)", "Amy — en_US-amy-medium"),
    ("Kristin (medium)", "Kristin — en_US-kristin-medium"),
    ("LibriTTS (high)", "LibriTTS — en_US-libritts-high"),
]
KOKORO_VOICES = [
    ("Heart", "Heart — af_heart"), ("Nova", "Nova — af_nova"),
    ("Bella", "Bella — af_bella"), ("Puck", "Puck — am_puck"),
    ("Michael", "Michael — am_michael"), ("Emma", "Emma — bf_emma"),
]


def _text(value: str, *, soft: bool = False, size: int = 10, wrap: bool = True) -> QLabel:
    label = QLabel(value)
    label.setWordWrap(wrap)
    label.setStyleSheet(
        f"color:{TEXT_SOFT if soft else TEXT}; background:transparent; font:{size}px '{FONT}';"
    )
    return label


class _SettingsCard(QFrame):
    def __init__(self, title: str, description: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("jarvisSettingsCard")
        self.setStyleSheet(
            "QFrame#jarvisSettingsCard { background:rgba(4,17,27,205);"
            " border:1px solid rgba(103,228,238,48); border-radius:12px; }"
        )
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 16)
        self.body.setSpacing(10)
        heading = _text(title.upper(), size=11)
        heading.setStyleSheet(heading.styleSheet() + "font-weight:700; letter-spacing:2px;")
        self.body.addWidget(heading)
        if description:
            self.body.addWidget(_text(description, soft=True, size=9))

    def row(self, label: str, control: QWidget, description: str = "") -> None:
        row = QHBoxLayout()
        copy = QVBoxLayout()
        copy.setSpacing(2)
        copy.addWidget(_text(label, size=10))
        if description:
            copy.addWidget(_text(description, soft=True, size=8))
        row.addLayout(copy, 1)
        row.addWidget(control)
        self.body.addLayout(row)


class JarvisSettingsView(QWidget):
    """Full JARVIS control matrix with category navigation."""

    changed = Signal(dict)
    closeRequested = Signal()
    actionRequested = Signal(str, object)

    DEFAULTS = {
        "assistant_name": "JARVIS",
        "personality": "decisive, concise, proactive",
        "provider": "Ollama",
        "models": dict(PROVIDER_MODELS),
        "api_keys": {name: "" for name in PROVIDERS if name != "Ollama"},
        "voice_engine": "piper",
        "voice": "Ryan — en_US-ryan-high",
        "speed": 1.0,
        "voice_preload": True,
        "auto_speak": True,
        "hotkey": "ctrl+space",
        "voice_device": "",
        "theme": "space_black",
        "accent": 0,
        "compact": False,
        "always_on_top": False,
        "show_timestamps": True,
        "core_animation": True,
        "widget_hover_effects": True,
        "auto_open_widgets": True,
        "show_live_transcript": True,
        "reduced_motion": False,
        "layout_snap": True,
        "targeted_memory": True,
        "show_memory_sources": True,
        "memory_limit": 5,
        "confirm_destructive": True,
        "confirm_external_actions": False,
        "confirm_commands": True,
        "discord_bot_token": "",
        "discord_default_channel": "",
        "whatsapp_access_token": "",
        "whatsapp_phone_number_id": "",
        "whatsapp_api_version": "v23.0",
        "weather_location": "",
        "notes_autosave": True,
        "activity_history": True,
        "developer_mode": False,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = deepcopy(self.DEFAULTS)
        self._loading = False
        self._controls: dict[str, QWidget] = {}
        self._nav: list[QPushButton] = []
        self._provider_handlers_connected = False
        self.setObjectName("jarvisSettingsView")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#jarvisSettingsView {{ background:#030c14; border:1px solid rgba(103,228,238,95); border-radius:14px; }}"
            f"QLineEdit,QComboBox {{ color:{TEXT}; background:rgba(2,10,17,220); border:1px solid rgba(103,228,238,55); border-radius:8px; padding:8px; min-width:180px; }}"
            f"QLineEdit:focus,QComboBox:focus {{ border-color:{CYAN}; }}"
            f"QComboBox QAbstractItemView {{ color:{TEXT}; background:#06131d; selection-background-color:#123847; }}"
            f"QCheckBox {{ color:{TEXT_SOFT}; spacing:8px; }} QCheckBox::indicator {{ width:18px; height:18px; border:1px solid rgba(103,228,238,85); border-radius:5px; background:rgba(2,10,17,220); }}"
            f"QCheckBox::indicator:checked {{ background:{CYAN}; border-color:{CYAN}; }}"
            "QScrollArea { border:0; background:transparent; } QScrollArea > QWidget > QWidget { background:transparent; }"
            "QScrollBar:vertical { background:transparent; width:7px; } QScrollBar::handle:vertical { background:rgba(103,228,238,70); border-radius:3px; min-height:28px; }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(20, 16, 14, 12)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = _text("JARVIS // CONTROL MATRIX", size=13, wrap=False)
        title.setStyleSheet(title.styleSheet() + "font-weight:700; letter-spacing:3px;")
        title_box.addWidget(title)
        title_box.addWidget(_text("All assistant systems in one native interface", soft=True, size=8))
        header.addLayout(title_box)
        header.addStretch(1)
        close = AnimatedIconButton("x", size=34, tooltip="Close settings")
        close.clicked.connect(self.closeRequested)
        header.addWidget(close)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setContentsMargins(12, 0, 12, 12)
        body.setSpacing(12)
        nav = QVBoxLayout()
        nav.setSpacing(6)
        categories = ["Identity", "Intelligence", "Voice", "Interface", "Memory", "Connectors", "Safety & Services"]
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background:transparent;")
        builders = [self._identity, self._intelligence, self._voice, self._interface, self._memory, self._connectors, self._safety]
        for index, (name, builder) in enumerate(zip(categories, builders)):
            button = QPushButton(name.upper())
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedWidth(138)
            button.clicked.connect(lambda checked=False, i=index: self._select(i))
            self._nav.append(button)
            nav.addWidget(button)
            self.stack.addWidget(self._page(name, builder))
        nav.addStretch(1)
        reset = QPushButton("RESET DEFAULTS")
        reset.setStyleSheet(button_style(danger=True))
        reset.clicked.connect(self._reset)
        nav.addWidget(reset)
        body.addLayout(nav)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        self._select(0)

    def _page(self, title: str, builder) -> QScrollArea:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(5, 4, 8, 8)
        layout.setSpacing(10)
        label = _text(title.upper(), size=16)
        label.setStyleSheet(label.styleSheet() + f"color:{CYAN}; font-weight:300; letter-spacing:3px;")
        layout.addWidget(label)
        builder(layout)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    def _line(self, key: str, placeholder: str = "", *, secret: bool = False) -> QLineEdit:
        control = QLineEdit()
        control.setPlaceholderText(placeholder)
        if secret:
            control.setEchoMode(QLineEdit.Password)
        control.textChanged.connect(lambda value, k=key: self._set(k, value))
        self._controls[key] = control
        return control

    def _combo(self, key: str, values: list[str]) -> QComboBox:
        control = QComboBox()
        control.addItems(values)
        control.currentTextChanged.connect(lambda value, k=key: self._set(k, value))
        self._controls[key] = control
        return control

    def _toggle(self, key: str) -> QCheckBox:
        control = QCheckBox("ENABLED")
        control.toggled.connect(lambda value, k=key: self._set(k, bool(value)))
        self._controls[key] = control
        return control

    def _identity(self, layout):
        card = _SettingsCard("Assistant identity", "Controls how the assistant identifies itself and communicates.")
        card.row("Assistant name", self._line("assistant_name", "JARVIS"))
        card.row("Personality directive", self._line("personality", "decisive, concise, proactive"))
        card.row("Global hotkey", self._line("hotkey", "ctrl+space"), "Wake or focus JARVIS from anywhere")
        layout.addWidget(card)

    def _intelligence(self, layout):
        card = _SettingsCard("Model runtime", "Changing provider or model rebuilds the assistant runtime.")
        self._provider = self._combo("provider", PROVIDERS)
        self._provider.currentTextChanged.connect(self._provider_changed)
        card.row("Provider", self._provider)
        self._model = self._line("_active_model", "Model identifier")
        card.row("Active model", self._model)
        self._api_key = self._line("_active_key", "Local provider needs no key", secret=True)
        card.row("Provider API key", self._api_key, "Stored locally; never displayed in status widgets")
        test = QPushButton("TEST ACTIVE PROVIDER")
        test.setStyleSheet(button_style(accent=True))
        test.clicked.connect(lambda: self.actionRequested.emit("test_provider", {"provider": self._settings["provider"]}))
        card.body.addWidget(test, 0, Qt.AlignRight)
        layout.addWidget(card)

    def _voice(self, layout):
        card = _SettingsCard("Speech systems", "Microphone, transcription, and synthesized voice behavior.")
        self._engine = self._combo("voice_engine", ["piper", "kokoro"])
        self._engine.currentTextChanged.connect(self._voice_engine_changed)
        card.row("Voice engine", self._engine)
        self._voice_choice = QComboBox()
        self._voice_choice.currentIndexChanged.connect(self._voice_selected)
        self._controls["voice"] = self._voice_choice
        card.row("Voice", self._voice_choice)
        speed = QSlider(Qt.Horizontal)
        speed.setRange(60, 160)
        speed.valueChanged.connect(lambda value: self._set("speed", value / 100.0))
        self._controls["speed"] = speed
        card.row("Speaking speed", speed)
        card.row("Input device", self._line("voice_device", "System default"))
        card.row("Preload speech models", self._toggle("voice_preload"))
        card.row("Speak responses automatically", self._toggle("auto_speak"))
        test = QPushButton("TEST MICROPHONE / VOICE")
        test.setStyleSheet(button_style(accent=True))
        test.clicked.connect(lambda: self.actionRequested.emit("test_voice", {}))
        card.body.addWidget(test, 0, Qt.AlignRight)
        layout.addWidget(card)

    def _interface(self, layout):
        card = _SettingsCard("Intelligence interface", "Visual behavior for the core, workspace, and widgets.")
        for key, label, detail in [
            ("core_animation", "Animated intelligence core", "Listening and task-state motion"),
            ("widget_hover_effects", "Widget hover energy", "Glow and focus transitions"),
            ("auto_open_widgets", "Intent-driven widgets", "Open only panels useful to the request"),
            ("show_live_transcript", "Live voice transcript", "Show captured speech under the core"),
            ("layout_snap", "Snap widget layout", "Align panels while dragging"),
            ("always_on_top", "Always on top", "Keep JARVIS above other applications"),
            ("reduced_motion", "Reduced motion", "Disable continuous and hover animations"),
            ("show_timestamps", "Message timestamps", "Show time metadata in detailed chat"),
        ]:
            card.row(label, self._toggle(key), detail)
        layout.addWidget(card)

    def _memory(self, layout):
        card = _SettingsCard("Context and memory", "Memory is retrieved only when it can help the current task.")
        card.row("Targeted retrieval", self._toggle("targeted_memory"))
        card.row("Show memory sources", self._toggle("show_memory_sources"))
        card.row("Retain activity timeline", self._toggle("activity_history"))
        limit = QSlider(Qt.Horizontal)
        limit.setRange(1, 10)
        limit.valueChanged.connect(lambda value: self._set("memory_limit", value))
        self._controls["memory_limit"] = limit
        card.row("Maximum recalled items", limit)
        inspect = QPushButton("OPEN MEMORY RECALL")
        inspect.setStyleSheet(button_style(accent=True))
        inspect.clicked.connect(lambda: self.actionRequested.emit("open_widget", {"widget_type": "memory_recall"}))
        card.body.addWidget(inspect, 0, Qt.AlignRight)
        layout.addWidget(card)

    def _connectors(self, layout):
        discord = _SettingsCard(
            "Discord bot connection",
            "Uses Discord's official bot API. The bot can read and send in channels it can access, including DMs sent to the bot. Normal user-account tokens are not used.",
        )
        discord.row("Bot token", self._line("discord_bot_token", "Discord Developer Portal bot token", secret=True))
        discord.row("Default channel ID", self._line("discord_default_channel", "Optional server or bot-DM channel ID"))
        test_discord = QPushButton("TEST DISCORD CONNECTION")
        test_discord.setStyleSheet(button_style(accent=True))
        test_discord.clicked.connect(lambda: self.actionRequested.emit("test_connector", {"name": "discord"}))
        discord.body.addWidget(test_discord, 0, Qt.AlignRight)
        layout.addWidget(discord)

        whatsapp = _SettingsCard(
            "WhatsApp connection",
            "Live send/receive uses Meta's WhatsApp Business Cloud API. Personal chats can be read from an exported chat file without exposing account credentials.",
        )
        whatsapp.row("Cloud API access token", self._line("whatsapp_access_token", "Permanent/system-user access token", secret=True))
        whatsapp.row("Phone-number ID", self._line("whatsapp_phone_number_id", "Meta WhatsApp phone-number ID"))
        whatsapp.row("Graph API version", self._line("whatsapp_api_version", "v23.0"))
        test_whatsapp = QPushButton("TEST WHATSAPP CONNECTION")
        test_whatsapp.setStyleSheet(button_style(accent=True))
        test_whatsapp.clicked.connect(lambda: self.actionRequested.emit("test_connector", {"name": "whatsapp"}))
        whatsapp.body.addWidget(test_whatsapp, 0, Qt.AlignRight)
        layout.addWidget(whatsapp)

        behavior = _SettingsCard(
            "Message behavior",
            "An explicit voice/text command or SEND button is authorization to send immediately. Drafts and inferred recipients never send by themselves.",
        )
        behavior.body.addWidget(_text("Tokens are stored only in the existing local JARVIS settings file. Environment variables can be used instead.", soft=True, size=9))
        layout.addWidget(behavior)

    def _safety(self, layout):
        safety = _SettingsCard("Safety gates", "Destructive operations keep their safeguards. Explicitly requested messages do not receive a redundant approval dialog.")
        safety.row("Confirm destructive file actions", self._toggle("confirm_destructive"))
        safety.row("Confirm inferred external actions", self._toggle("confirm_external_actions"), "Does not affect an explicit SEND command or button")
        safety.row("Confirm command execution", self._toggle("confirm_commands"))
        safety.row("Developer controls", self._toggle("developer_mode"))
        layout.addWidget(safety)
        services = _SettingsCard("Service preferences", "Defaults used by live widgets and connector requests.")
        services.row("Weather location", self._line("weather_location", "City or region"))
        services.row("Autosave notes", self._toggle("notes_autosave"))
        connectors = QPushButton("OPEN CONNECTIONS")
        connectors.setStyleSheet(button_style(accent=True))
        connectors.clicked.connect(lambda: self.actionRequested.emit("open_widget", {"widget_type": "connectors"}))
        services.body.addWidget(connectors, 0, Qt.AlignRight)
        layout.addWidget(services)

    def _select(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for number, button in enumerate(self._nav):
            button.setChecked(number == index)
            button.setStyleSheet(
                button_style(accent=number == index)
                + (f"QPushButton {{ text-align:left; padding:10px; color:{CYAN}; }}" if number == index else "QPushButton { text-align:left; padding:10px; }")
            )

    def _set(self, key: str, value) -> None:
        if self._loading or key.startswith("_"):
            return
        self._settings[key] = value
        self.changed.emit(self.settings())

    def _provider_changed(self, provider: str) -> None:
        if self._loading:
            return
        self._settings["provider"] = provider
        self._sync_provider_fields()
        self.changed.emit(self.settings())

    def _sync_provider_fields(self) -> None:
        provider = self._settings.get("provider", "Ollama")
        self._model.blockSignals(True)
        self._api_key.blockSignals(True)
        self._model.setText(self._settings.setdefault("models", {}).get(provider, PROVIDER_MODELS.get(provider, "")))
        self._api_key.setText(self._settings.setdefault("api_keys", {}).get(provider, ""))
        self._api_key.setEnabled(provider != "Ollama")
        self._api_key.setPlaceholderText("Local provider · no API key" if provider == "Ollama" else "API key")
        self._model.blockSignals(False)
        self._api_key.blockSignals(False)
        if not self._provider_handlers_connected:
            self._model.textChanged.connect(self._active_model_changed)
            self._api_key.textChanged.connect(self._active_key_changed)
            self._provider_handlers_connected = True

    def _active_model_changed(self, value: str) -> None:
        if self._loading:
            return
        self._settings.setdefault("models", {})[self._settings["provider"]] = value
        self.changed.emit(self.settings())

    def _active_key_changed(self, value: str) -> None:
        if self._loading or self._settings.get("provider") == "Ollama":
            return
        self._settings.setdefault("api_keys", {})[self._settings["provider"]] = value
        self.changed.emit(self.settings())

    def _voice_engine_changed(self, engine: str) -> None:
        if self._loading:
            return
        self._settings["voice_engine"] = engine
        self._populate_voices(engine, self._settings.get("voice", ""))
        self.changed.emit(self.settings())

    def _populate_voices(self, engine: str, selected: str) -> str:
        options = KOKORO_VOICES if engine == "kokoro" else PIPER_VOICES
        values = {value for _name, value in options}
        if selected not in values:
            selected = "Puck — am_puck" if engine == "kokoro" else "Ryan — en_US-ryan-high"
        self._voice_choice.blockSignals(True)
        self._voice_choice.clear()
        for label, value in options:
            self._voice_choice.addItem(label, value)
        self._voice_choice.setCurrentIndex(max(0, self._voice_choice.findData(selected)))
        self._voice_choice.blockSignals(False)
        self._settings["voice"] = selected
        return selected

    def _voice_selected(self) -> None:
        if self._loading:
            return
        self._settings["voice"] = self._voice_choice.currentData() or self._voice_choice.currentText()
        self.changed.emit(self.settings())

    def load(self, settings: dict) -> None:
        self._loading = True
        try:
            merged = deepcopy(self.DEFAULTS)
            incoming = dict(settings or {})
            for key, value in incoming.items():
                if key == "models" and isinstance(value, dict):
                    merged["models"].update(value)
                elif key == "api_keys" and isinstance(value, dict):
                    merged["api_keys"].update(value)
                elif key in merged:
                    merged[key] = value
            self._settings = merged
            for key, control in self._controls.items():
                if key.startswith("_") or key in {"provider", "voice_engine", "voice"}:
                    continue
                value = merged.get(key)
                if isinstance(control, QLineEdit):
                    control.setText(str(value or ""))
                elif isinstance(control, QCheckBox):
                    control.setChecked(bool(value))
                elif isinstance(control, QSlider):
                    control.setValue(int(float(value) * 100) if key == "speed" else int(value))
                elif isinstance(control, QComboBox):
                    control.setCurrentText(str(value))
            self._provider.setCurrentText(merged["provider"])
            self._engine.setCurrentText(merged["voice_engine"])
            self._populate_voices(merged["voice_engine"], merged["voice"])
            self._sync_provider_fields()
        finally:
            self._loading = False

    def settings(self) -> dict:
        return deepcopy(self._settings)

    def _reset(self) -> None:
        self.load(deepcopy(self.DEFAULTS))
        self.changed.emit(self.settings())
