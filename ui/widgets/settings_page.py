"""
SettingsPage — the full-page settings surface of the redesign.

Sections (matching the mockup):
    Identity        — assistant name
    Voice Settings  — engine, persona, speed slider + live value, test button
    Themes          — Light / Dark cards
    AI Providers    — provider select, model, API key (saved to .env),
                      YouTube + Tavily keys
    Preferences     — speak-aloud toggle, timestamps toggle, voice hotkey

Every control writes into a local settings dict (persisted by the
window) and emits `changed(settings)` — the same contract as the old
slide-over panel.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QRectF, Signal, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame, QLineEdit,
)

from .. import icons, theme
from .glass import (
    GlassLineEdit, GlassCombo, GlassToggle, GlassSlider, GlassButton,
    GlassIconButton, GlassCard, SectionHeader, label, divider,
)
from config.env_file import read_env, update_env_file
from ui.theme import ASSISTANT_NAME as ASSISTANT_NAME_DEFAULT

PROVIDERS = ["Ollama", "Ollama cloud", "OpenRouter", "Gemini", "OpenAI",
             "Anthropic", "Groq", "DeepSeek"]
LOCAL_PROVIDERS = {"Ollama", "Ollama cloud"}

PROVIDER_ENV_KEY = {
    "OpenRouter": "OPENROUTER_API_KEY",
    "Gemini": "GEMINI_API_KEY",
    "OpenAI": "OPENAI_API_KEY",
    "Anthropic": "ANTHROPIC_API_KEY",
    "Groq": "GROQ_API_KEY",
    "DeepSeek": "DEEPSEEK_API_KEY",
}

PROVIDER_DEFAULT_MODELS = {
    "Ollama": "qwen2.5vl:7b",
    "Ollama cloud": "qwen3-8b",
    "OpenRouter": "meta-llama/llama-3.3-70b-instruct",
    "Gemini": "gemini-2.5-flash",
    "OpenAI": "gpt-4o-mini",
    "Anthropic": "claude-3-5-haiku-latest",
    "Groq": "llama-3.3-70b-versatile",
    "DeepSeek": "deepseek-chat",
}

VOICE_OPTIONS = [
    ("Ryan — en_US-ryan-high", "Ryan (high)"),
    ("Amy — en_US-amy-medium", "Amy (medium)"),
    ("Kristin — en_US-kristin-medium", "Kristin (medium)"),
    ("LibriTTS — en_US-libritts-high", "LibriTTS (high)"),
]

ENGINE_OPTIONS = [
    ("kokoro", "Kokoro (recommended)"),
    ("piper", "Piper"),
]


class SettingsPage(QWidget):
    changed = Signal(dict)

    DEFAULTS = {
        "assistant_name": ASSISTANT_NAME_DEFAULT,
        "voice_engine": "kokoro",
        "voice": "Ryan — en_US-ryan-high",
        "speed": 1.0,
        "accent": 0,
        "compact": False,
        "provider": "Ollama",
        "models": dict(PROVIDER_DEFAULT_MODELS),
        "api_keys": {p: "" for p in PROVIDERS if p not in LOCAL_PROVIDERS},
        "theme": "dark",
        "hotkey": "ctrl+space",
        "auto_speak": True,
        "show_timestamps": True,
        "always_on_top": False,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = dict(self.DEFAULTS)
        self._loading = False
        self._env = read_env()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ---- header ----
        header = QHBoxLayout()
        header.setContentsMargins(28, 18, 28, 10)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self._title = label("Settings", size=21, weight=700, color=theme.TEXT)
        title_box.addWidget(self._title)
        self._subtitle = label(
            "Manage Nova's identity, voice, and models.",
            size=12.5, weight=400, color=theme.TEXT_SOFT)
        title_box.addWidget(self._subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        lay.addLayout(header)

        # ---- scroll body ----
        self._body = QWidget()
        self._body.setAttribute(Qt.WA_TranslucentBackground)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(28, 4, 28, 24)
        self._body_layout.setSpacing(8)
        self._body_layout.setAlignment(Qt.AlignTop)

        self._build_identity()
        self._build_voice()
        self._build_themes()
        self._build_providers()
        self._build_preferences()
        self._body_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(theme.scroll_qss())
        self._scroll.setWidget(self._body)
        lay.addWidget(self._scroll, 1)

    # ================================================================== #
    # sections
    # ================================================================== #
    def _section_header(self, text: str):
        self._body_layout.addSpacing(10)
        self._body_layout.addWidget(SectionHeader(text))

    def _build_identity(self):
        self._section_header("Identity")
        card = GlassCard()
        self._name_edit = GlassLineEdit("Assistant name")
        self._name_edit.changed.connect(self._on_name_changed)
        card.body.addWidget(self._name_edit)
        hint = label("This is how Nova introduces itself and signs replies.",
                     size=12, weight=400, color=theme.TEXT_FAINT)
        card.body.addWidget(hint)
        self._body_layout.addWidget(card)

    def _build_voice(self):
        self._section_header("Voice Settings")
        card = GlassCard()

        engine_row = QHBoxLayout()
        engine_row.setSpacing(12)
        engine_row.addWidget(label("Engine", size=13.5, weight=600, color=theme.TEXT,
                                   wrap=False))
        engine_row.addStretch(1)
        self._engine_combo = GlassCombo([label for _, label in ENGINE_OPTIONS])
        self._engine_combo.setFixedWidth(230)
        self._engine_combo.changed.connect(self._on_voice_changed)
        engine_row.addWidget(self._engine_combo)
        card.body.addLayout(engine_row)

        card.body.addWidget(divider())

        persona_row = QHBoxLayout()
        persona_row.setSpacing(12)
        persona_row.addWidget(label("Voice", size=13.5, weight=600, color=theme.TEXT,
                                    wrap=False))
        persona_row.addStretch(1)
        self._voice_combo = GlassCombo([lbl for _, lbl in VOICE_OPTIONS])
        self._voice_combo.setFixedWidth(230)
        self._voice_combo.changed.connect(self._on_voice_changed)
        persona_row.addWidget(self._voice_combo)
        card.body.addLayout(persona_row)

        card.body.addWidget(divider())

        speed_row = QHBoxLayout()
        speed_row.setSpacing(12)
        speed_row.addWidget(label("Speed", size=13.5, weight=600, color=theme.TEXT,
                                  wrap=False))
        speed_row.addStretch(1)
        self._speed_slider = GlassSlider(minimum=0.5, maximum=3.0, step=0.05,
                                         value=1.0)
        self._speed_slider.setFixedWidth(220)
        self._speed_slider.changed.connect(self._on_speed_changed)
        speed_row.addWidget(self._speed_slider)
        self._speed_value = label("1.00×", size=12.5, weight=600, color=theme.ACCENT,
                                  wrap=False)
        self._speed_value.setFixedWidth(52)
        speed_row.addWidget(self._speed_value)
        card.body.addLayout(speed_row)

        card.body.addWidget(divider())

        test_row = QHBoxLayout()
        test_row.setSpacing(12)
        self._test_btn = GlassButton("Test voice", icon_name="play", icon_size=13,
                                     variant="ghost", pill=True)
        self._test_btn.setMinimumHeight(32)
        self._test_btn.clicked.connect(self._on_test_voice)
        test_row.addWidget(self._test_btn)
        self._test_status = label("", size=12, weight=400, color=theme.TEXT_SOFT)
        test_row.addWidget(self._test_status)
        test_row.addStretch(1)
        card.body.addLayout(test_row)

        self._body_layout.addWidget(card)

    def _build_themes(self):
        self._section_header("Themes")
        card = GlassCard()
        themes_row = QHBoxLayout()
        themes_row.setSpacing(14)
        self._theme_cards = {}
        for key in ("dark", "light"):
            tc = _ThemeCard(key, theme.THEME_NAMES[key])
            tc.clicked.connect(lambda k=key: self._on_theme_picked(k))
            themes_row.addWidget(tc, 1)
            self._theme_cards[key] = tc
        card.body.addLayout(themes_row)
        self._body_layout.addWidget(card)

    def _build_providers(self):
        self._section_header("AI Providers & API Keys")
        card = GlassCard()

        provider_row = QHBoxLayout()
        provider_row.setSpacing(12)
        provider_row.addWidget(label("Provider", size=13.5, weight=600,
                                     color=theme.TEXT, wrap=False))
        provider_row.addStretch(1)
        self._provider_combo = GlassCombo(PROVIDERS)
        self._provider_combo.setFixedWidth(230)
        self._provider_combo.changed.connect(self._on_provider_changed)
        provider_row.addWidget(self._provider_combo)
        card.body.addLayout(provider_row)

        card.body.addWidget(divider())

        model_row = QHBoxLayout()
        model_row.setSpacing(12)
        model_row.addWidget(label("Model", size=13.5, weight=600, color=theme.TEXT,
                                  wrap=False))
        model_row.addStretch(1)
        self._model_edit = GlassLineEdit("model name", max_width=230)
        self._model_edit.changed.connect(self._on_model_changed)
        model_row.addWidget(self._model_edit)
        card.body.addLayout(model_row)

        self._key_block = QWidget()
        key_lay = QVBoxLayout(self._key_block)
        key_lay.setContentsMargins(0, 0, 0, 0)
        key_lay.setSpacing(10)

        api_row = QHBoxLayout()
        api_row.setSpacing(12)
        api_row.addWidget(label("API key", size=13.5, weight=600, color=theme.TEXT,
                                wrap=False))
        api_row.addStretch(1)
        self._key_edit = GlassLineEdit("sk-…", max_width=230)
        self._key_edit.editor.setEchoMode(QLineEdit.EchoMode.Password)
        api_row.addWidget(self._key_edit)
        self._key_save = GlassButton("Save", variant="ghost", pill=True)
        self._key_save.setMinimumHeight(30)
        self._key_save.clicked.connect(self._on_save_provider_key)
        api_row.addWidget(self._key_save)
        self._key_status = label("", size=11.5, weight=400, color=theme.TEXT_SOFT)
        self._key_status.setFixedWidth(60)
        api_row.addWidget(self._key_status)
        key_lay.addLayout(api_row)
        key_hint = label(
            "Saved to your local .env file — never stored in the cloud.",
            size=12, weight=400, color=theme.TEXT_FAINT)
        key_lay.addWidget(key_hint)
        card.body.addWidget(self._key_block)

        card.body.addWidget(divider())

        for env_key, title, placeholder, hint in (
            ("YOUTUBE_API_KEY", "YouTube Data API key", "YouTube API key",
             "Used for transcript search and video metadata."),
            ("TAVILY_API_KEY", "Tavily API key", "Tavily API key",
             "Used for web search and source-cited answers."),
        ):
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addWidget(label(title, size=13.5, weight=600, color=theme.TEXT,
                                wrap=False))
            row.addStretch(1)
            edit = GlassLineEdit(placeholder, max_width=230)
            row.addWidget(edit)
            btn = GlassButton("Save", variant="ghost", pill=True)
            btn.setMinimumHeight(30)
            row.addWidget(btn)
            status = label("", size=11.5, weight=400, color=theme.TEXT_SOFT)
            status.setFixedWidth(60)
            row.addWidget(status)
            card.body.addLayout(row)
            hint_lab = label(hint, size=12, weight=400, color=theme.TEXT_FAINT)
            card.body.addWidget(hint_lab)
            edit.setText(self._env.get(env_key, ""))
            btn.clicked.connect(
                lambda _=False, k=env_key, e=edit, s=status: self._save_env_key(k, e, s))

        self._body_layout.addWidget(card)

    def _build_preferences(self):
        self._section_header("Preferences")
        card = GlassCard()

        self._auto_speak_toggle = GlassToggle(True)
        card.body.addLayout(self._pref_row("Speak responses aloud",
                                           "Nova reads replies with the chosen voice",
                                           self._auto_speak_toggle,
                                           self._on_auto_speak_changed))

        self._timestamps_toggle = GlassToggle(True)
        card.body.addLayout(self._pref_row("Show timestamps",
                                           "Display a timestamp on every message",
                                           self._timestamps_toggle,
                                           self._on_timestamps_changed))

        card.body.addWidget(divider())

        hotkey_row = QHBoxLayout()
        hotkey_row.setSpacing(12)
        hotkey_row.addWidget(label("Voice hotkey", size=13.5, weight=600,
                                   color=theme.TEXT, wrap=False))
        hotkey_row.addStretch(1)
        self._hotkey_edit = GlassLineEdit("ctrl+space", max_width=180)
        self._hotkey_edit.changed.connect(self._on_hotkey_changed)
        hotkey_row.addWidget(self._hotkey_edit)
        card.body.addLayout(hotkey_row)
        hotkey_hint = label(
            "Show Nova and start voice mode from anywhere, even when the "
            "window is hidden.", size=12, weight=400, color=theme.TEXT_FAINT)
        card.body.addWidget(hotkey_hint)

        self._body_layout.addWidget(card)

    def _pref_row(self, title: str, hint: str, toggle: GlassToggle, slot) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        text_box.addWidget(label(title, size=13.5, weight=600, color=theme.TEXT))
        text_box.addWidget(label(hint, size=12, weight=400, color=theme.TEXT_FAINT))
        row.addLayout(text_box, 1)
        toggle.toggled.connect(slot)
        row.addWidget(toggle, 0, Qt.AlignVCenter)
        return row

    # ================================================================== #
    # load / collect
    # ================================================================== #
    def load(self, settings: dict):
        self._loading = True
        self._settings = dict(self.DEFAULTS)
        self._settings.update({k: v for k, v in settings.items()
                               if k in self.DEFAULTS})
        merged_models = dict(self.DEFAULTS["models"])
        merged_models.update(self._settings.get("models") or {})
        self._settings["models"] = merged_models
        merged_keys = dict(self.DEFAULTS["api_keys"])
        merged_keys.update(self._settings.get("api_keys") or {})
        self._settings["api_keys"] = merged_keys

        self._name_edit.setText(self._settings.get("assistant_name", ""))

        engine = (self._settings.get("voice_engine") or "kokoro").strip().lower()
        engine_idx = next((i for i, (k, _) in enumerate(ENGINE_OPTIONS) if k == engine), 0)
        self._engine_combo.setCurrentIndex(engine_idx)

        voice = self._settings.get("voice") or ""
        voice_idx = next((i for i, (v, _) in enumerate(VOICE_OPTIONS) if v == voice), 0)
        self._voice_combo.setCurrentIndex(voice_idx)

        speed = self._settings.get("speed", 1.0)
        self._speed_slider.setValue(speed)
        self._speed_value.setText(f"{speed:.2f}".rstrip("0").rstrip(".") + "×")

        provider = self._settings.get("provider") or "Ollama"
        if provider not in PROVIDERS:
            provider = "Ollama"
        self._provider_combo.setCurrentText(provider)
        self._on_provider_changed(self._provider_combo.currentIndex(), emit=False)

        theme_key = theme._resolve_theme(self._settings.get("theme") or "dark")
        self._settings["theme"] = theme_key
        for key, card in self._theme_cards.items():
            card.set_selected(key == theme_key)

        self._auto_speak_toggle.setChecked(bool(self._settings.get("auto_speak", True)))
        self._timestamps_toggle.setChecked(
            bool(self._settings.get("show_timestamps", True)))
        self._hotkey_edit.setText(self._settings.get("hotkey", "ctrl+space"))
        self._loading = False

    def _on_provider_changed(self, idx: int, emit: bool = True):
        provider = PROVIDERS[idx]
        models = self._settings.setdefault("models", dict(PROVIDER_DEFAULT_MODELS))
        if provider not in models:
            models[provider] = PROVIDER_DEFAULT_MODELS.get(provider, "")
        self._model_edit.setText(models.get(provider, ""))
        local = provider in LOCAL_PROVIDERS
        self._key_block.setVisible(not local)
        if not local:
            keys = self._settings.setdefault("api_keys", {})
            self._key_edit.setText(keys.get(provider, ""))
        self._emit()

    def _on_name_changed(self, text: str):
        self._settings["assistant_name"] = text
        self._emit()

    def _on_voice_changed(self, _idx: int):
        engine_key, _ = ENGINE_OPTIONS[self._engine_combo.currentIndex()]
        self._settings["voice_engine"] = engine_key
        value, _ = VOICE_OPTIONS[self._voice_combo.currentIndex()]
        self._settings["voice"] = value
        self._emit()

    def _on_speed_changed(self, value: float):
        self._settings["speed"] = round(value, 2)
        v = self._settings["speed"]
        self._speed_value.setText(f"{v:.2f}×")
        self._emit()

    def _on_model_changed(self, text: str):
        provider = PROVIDERS[self._provider_combo.currentIndex()]
        self._settings.setdefault("models", {})[provider] = text
        self._emit()

    def _on_theme_picked(self, key: str):
        for k, card in self._theme_cards.items():
            card.set_selected(k == key)
        self._settings["theme"] = key
        self._emit()

    def _on_auto_speak_changed(self, checked: bool):
        self._settings["auto_speak"] = checked
        self._emit()

    def _on_timestamps_changed(self, checked: bool):
        self._settings["show_timestamps"] = checked
        self._emit()

    def _on_hotkey_changed(self, text: str):
        self._settings["hotkey"] = text
        self._emit()

    def _on_test_voice(self):
        self._test_status.setText("Playing preview…")
        QTimer.singleShot(2600, lambda: self._test_status.setText(""))

    def _on_save_provider_key(self):
        provider = PROVIDERS[self._provider_combo.currentIndex()]
        if provider in LOCAL_PROVIDERS:
            return
        key = self._key_edit.text().strip()
        self._settings.setdefault("api_keys", {})[provider] = key
        self._save_env_key(PROVIDER_ENV_KEY[provider], self._key_edit,
                           self._key_status)
        self._emit()

    def _save_env_key(self, env_key: str, edit: GlassLineEdit, status: QLabel):
        value = edit.text().strip()
        try:
            update_env_file({env_key: value})
            self._env[env_key] = value
            status.setText("Saved ✓")
        except Exception:
            status.setText("Failed")
        QTimer.singleShot(2200, lambda: status.setText(""))

    def _emit(self):
        if not self._loading:
            self.changed.emit(dict(self._settings))

    # ================================================================== #
    def apply_theme(self):
        self._title.setStyleSheet(theme.text_qss(size=21, weight=700, color=theme.TEXT))
        self._subtitle.setStyleSheet(
            theme.text_qss(size=12.5, weight=400, color=theme.TEXT_SOFT))
        self._scroll.setStyleSheet(theme.scroll_qss())
        self._name_edit.apply_theme()
        self._engine_combo.apply_theme()
        self._voice_combo.apply_theme()
        self._provider_combo.apply_theme()
        self._model_edit.apply_theme()
        self._key_edit.apply_theme()
        self._speed_value.setStyleSheet(
            theme.text_qss(size=12.5, weight=600, color=theme.ACCENT))
        for card in self._theme_cards.values():
            card.apply_theme()
        for w in self._body.findChildren(QWidget):
            if hasattr(w, "_text_style"):
                try:
                    w.apply_theme()
                except Exception:
                    pass
        self._body.update()
        self.update()


class _ThemeCard(QWidget):
    clicked = Signal(str)

    def __init__(self, key: str, name: str, parent=None):
        super().__init__(parent)
        self._key = key
        self._name = name
        self._selected = False
        self._hover = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(88)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        preview = QWidget(self)
        preview.setFixedHeight(52)
        if key == "dark":
            preview.setStyleSheet(
                "background: #1E1E1E; border: 1px solid rgba(255,255,255,0.12);"
                "border-radius: 12px;")
        else:
            preview.setStyleSheet(
                "background: #FFFFFF; border: 1px solid rgba(28,27,31,0.14);"
                "border-radius: 12px;")
        lay.addWidget(preview)

        row = QHBoxLayout()
        row.setSpacing(8)
        name_lab = label(name, size=13, weight=600, color=theme.TEXT, wrap=False)
        row.addWidget(name_lab)
        row.addStretch(1)
        self._check = QLabel()
        self._check.setFixedSize(16, 16)
        self._update_check()
        row.addWidget(self._check)
        lay.addLayout(row)

    def set_selected(self, selected: bool):
        self._selected = selected
        self._update_check()
        self.update()

    def _update_check(self):
        if self._selected:
            self._check.setPixmap(icons.pixmap("check", theme.ACCENT, 14))
        else:
            self._check.setPixmap(icons.pixmap("check", theme.BG_4, 14))

    def _anim_to(self, prop, target):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(160)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event):
        self._anim_to(b"hover", 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._anim_to(b"hover", 0.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._key)
        super().mouseReleaseEvent(event)

    def _get_hover(self):
        return self._hover

    def _set_hover(self, v):
        self._hover = float(v)
        self.update()

    hover = Property(float, _get_hover, _set_hover)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        p.fillPath(path, QColor(theme.GLASS))
        if self._hover > 0:
            p.fillPath(path, QColor(theme.HOVER))
        border = QColor(theme.BORDER_FOCUS if self._selected else theme.BORDER)
        if self._selected:
            p.setPen(QPen(border, 1.6))
        else:
            p.setPen(QPen(border, 1.0))
        p.drawPath(path)

    def apply_theme(self):
        self._update_check()
        self.update()
