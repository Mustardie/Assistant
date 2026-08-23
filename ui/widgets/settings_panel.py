"""
SettingsPanel — a slide-over settings surface that feels like a real
desktop app (Linear / macOS System Settings style).

Sections:
    Assistant   — name + personality
    Model       — AI model selection cards
    Voice       — TTS engine, voice, speed
    Appearance  — accent color + density
    API         — provider + key
    Preferences — toggles

Every control writes into a local settings dict (persisted by the window)
and emits `changed(settings)`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal, QPropertyAnimation, QEasingCurve, Property, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QLineEdit,
    QComboBox, QSlider, QFrame, QPushButton,
)

from .. import icons
from ..theme import (
    BG_2, BG_3, BG_4, BORDER, BORDER_STRONG, TEXT, TEXT_SOFT, TEXT_FAINT,
    ACCENT, ACCENT_DEEP, ACCENT_2, FONT_FAMILY, FONT_BODY, FONT_SMALL, FONT_TINY, FONT_H3,
    THEME_NAMES, ASSISTANT_NAME as ASSISTANT_NAME_DEFAULT,
)
from .icon_button import IconButton
from config.env_file import read_env, update_env_file

# Selected provider -> the .env variable holding its API key.
PROVIDER_ENV_KEY = {
    "OpenRouter": "OPENROUTER_API_KEY",
    "Gemini": "GEMINI_API_KEY",
    "OpenAI": "OPENAI_API_KEY",
    "Anthropic": "ANTHROPIC_API_KEY",
    "Groq": "GROQ_API_KEY",
    "DeepSeek": "DEEPSEEK_API_KEY",
}

ACCENTS = [
    ("#6E8BFF", "#9B6DFF"),
    ("#34C6F2", "#5B8DFF"),
    ("#4CD6A5", "#2FAF8B"),
    ("#FF8A65", "#FF5C8A"),
    ("#E8D44D", "#E8A24D"),
]

PROVIDERS = ["Ollama", "OpenRouter", "Gemini", "OpenAI", "Anthropic", "Groq", "DeepSeek"]

# Sensible default model per provider -- shown/edited in the Provider
# section's "Model" field and used the first time a provider is selected.
# These are defaults only; the user's own value (persisted per-provider in
# settings["models"]) always wins once they've changed it.
PROVIDER_DEFAULT_MODELS = {
    "Ollama": "qwen2.5vl:7b",
    "OpenRouter": "meta-llama/llama-3.3-70b-instruct",
    "Gemini": "gemini-2.5-flash",
    "OpenAI": "gpt-4o-mini",
    "Anthropic": "claude-3-5-haiku-latest",
    "Groq": "llama-3.3-70b-versatile",
    "DeepSeek": "deepseek-chat",
}

PROVIDER_SUBTITLES = {
    "Ollama": "Local · no API key needed",
    "OpenRouter": "Cloud router · needs an API key",
    "Gemini": "Google cloud · needs an API key",
    "OpenAI": "OpenAI cloud · needs an API key",
    "Anthropic": "Anthropic cloud · needs an API key",
    "Groq": "Fast cloud inference · needs an API key",
    "DeepSeek": "DeepSeek cloud · needs an API key",
}

# Voice options: display label shown in the combo, value persisted in
# settings["voice"] (kept in the "Name — voice_id" format main.py parses).
PIPER_VOICE_OPTIONS = [
    ("Ryan — en_US-ryan-high", "Ryan (high)"),
    ("Amy — en_US-amy-medium", "Amy (medium)"),
    ("Kristin — en_US-kristin-medium", "Kristin (medium)"),
    ("LibriTTS — en_US-libritts-high", "LibriTTS (high)"),
]
KOKORO_VOICE_OPTIONS = [
    ("Heart — af_heart", "Heart"),
    ("Nova — af_nova", "Nova"),
    ("Bella — af_bella", "Bella"),
    ("Puck — am_puck", "Puck"),
    ("Michael — am_michael", "Michael"),
    ("Emma — bf_emma", "Emma"),
]
VOICE_OPTIONS = PIPER_VOICE_OPTIONS


class _Toggle(QWidget):
    """A premium switch, animated with a custom progress property."""

    toggled = Signal(bool)

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 22)
        self._checked = checked
        self._progress = 1.0 if checked else 0.0
        self._anim = None

    def _get_progress(self):
        return self._progress

    def _set_progress(self, v):
        self._progress = float(v)
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def setChecked(self, checked: bool):
        self._checked = checked
        self._stop_anim()
        anim = QPropertyAnimation(self, b"progress", self)
        self._anim = anim
        anim.setDuration(180)
        anim.setStartValue(self._progress)
        anim.setEndValue(1.0 if checked else 0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(self._on_animation_finished)
        anim.start()

    def _stop_anim(self):
        anim, self._anim = self._anim, None
        if anim is not None:
            anim.stop()

    def _on_animation_finished(self):
        if self.sender() is self._anim:
            self._anim = None

    def isChecked(self):
        return self._checked

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._checked = not self._checked
            self.setChecked(self._checked)
            self.toggled.emit(self._checked)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())

        color = QColor(ACCENT)
        color.setAlpha(200)
        knob_x = 2 + self._progress * (self.width() - self.height())
        if not self._checked and self._progress < 0.5:
            fill = QColor(BG_4)
            border = QColor(BORDER)
        else:
            gradient = QLinearGradient(rect.topLeft(), rect.topRight())
            gradient.setColorAt(0.0, QColor(ACCENT))
            gradient.setColorAt(1.0, QColor(ACCENT_2))
            painter.setBrush(gradient)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 11, 11)
            fill, border = None, None

        if fill is not None:
            painter.setBrush(fill)
            painter.setPen(QPen(border, 1.0))
            painter.drawRoundedRect(rect, 11, 11)

        knob = QRectF(knob_x, 2, self.height() - 4, self.height() - 4)
        painter.setBrush(QColor("#FFFFFF"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(knob)


class _OptionCard(QWidget):
    """Selectable card row used for model / engine choices."""

    clicked = Signal()

    def __init__(self, title: str, subtitle: str, selected=False, parent=None):
        super().__init__(parent)
        self.title = title
        self.subtitle = subtitle
        self._selected = selected
        self._hovered = False
        self.setFixedHeight(56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, selected: bool):
        self._selected = selected
        self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)

        if self._selected:
            base = QColor(ACCENT)
            base.setAlpha(38)
            painter.fillPath(path, base)
            pen_color = QColor(ACCENT)
            pen_color.setAlpha(150)
            pen = QPen(pen_color)
            pen.setWidthF(1.2)
            painter.setPen(pen)
        else:
            painter.fillPath(path, QColor(BG_3) if self._hovered else QColor(BG_2))
            painter.setPen(QPen(QColor(BORDER), 1.0))
        painter.drawPath(path)

        # radio dot
        dot_c = (20, self.height() // 2)
        if self._selected:
            painter.setBrush(QColor(ACCENT))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRectF(dot_c[0] - 5, dot_c[1] - 5, 10, 10))
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(QRectF(dot_c[0] - 2, dot_c[1] - 2, 4, 4))
        else:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(TEXT_FAINT), 1.4))
            painter.drawEllipse(QRectF(dot_c[0] - 5.5, dot_c[1] - 5.5, 11, 11))

        painter.setPen(QColor(TEXT))
        font = painter.font()
        font.setFamily(FONT_FAMILY)
        font.setPointSizeF(9.0)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(
            QRectF(38, 8, self.width() - 50, 18), Qt.AlignLeft | Qt.AlignVCenter,
            self.title,
        )
        painter.setPen(QColor(TEXT_FAINT))
        font.setWeight(QFont.Weight.Normal)
        font.setPointSizeF(7.8)
        painter.setFont(font)
        painter.drawText(
            QRectF(38, 28, self.width() - 50, 16), Qt.AlignLeft | Qt.AlignVCenter,
            self.subtitle,
        )


class _SectionCard(QWidget):
    """A titled card containing rows."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(
            f"color: {TEXT_SOFT}; font-family: {FONT_FAMILY};"
            f"font-size: {FONT_TINY}px; font-weight: 600;"
            "letter-spacing: 1.1px; background: transparent; padding: 2px 4px 6px 4px;"
        )
        layout.addWidget(self.title_label)

        self.body = QWidget()
        self.body.setAttribute(Qt.WA_TranslucentBackground)
        self.body.setStyleSheet("background: transparent;")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(10, 10, 10, 10)
        self.body_layout.setSpacing(8)
        layout.addWidget(self.body)


class _LabeledRow(QWidget):
    """Label + control on one row (used for API key, toggles, sliders).

    The label wraps (capped at 190px) so long labels never force the row
    wider than the panel, and the control stays pinned to the right edge."""

    def __init__(self, label: str, control: QWidget, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(10)
        lbl = QLabel(label)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(190)
        lbl.setStyleSheet(
            f"color: {TEXT}; font-family: {FONT_FAMILY}; font-size: {FONT_SMALL}px;"
            "font-weight: 500; background: transparent;"
        )
        layout.addWidget(lbl)
        layout.addStretch(1)
        layout.addWidget(control)


def _label(text, color=TEXT_SOFT, size=FONT_SMALL, weight=500, wrap=False):
    lbl = QLabel(text)
    lbl.setWordWrap(wrap)
    lbl.setStyleSheet(
        f"color: {color}; font-family: {FONT_FAMILY}; font-size: {size}px;"
        f"font-weight: {weight}; background: transparent;"
    )
    return lbl


def _combo(items, data=None) -> QComboBox:
    box = QComboBox()
    if data:
        for item, val in zip(items, data):
            box.addItem(item, val)
    else:
        box.addItems(items)
    box.setFixedHeight(30)
    box.setStyleSheet(
        f"""
        QComboBox {{
            background: {BG_3};
            border: 1px solid {BORDER};
            border-radius: 8px;
            color: {TEXT};
            font-family: {FONT_FAMILY};
            font-size: {FONT_SMALL}px;
            font-weight: 500;
            padding: 0 10px;
        }}
        QComboBox:hover {{ border: 1px solid {BORDER_STRONG}; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox::down-arrow {{
            image: none;
            border: none;
        }}
        QComboBox QAbstractItemView {{
            background: {BG_3};
            border: 1px solid {BORDER};
            border-radius: 8px;
            color: {TEXT};
            selection-background-color: rgba(110,139,255,0.35);
            outline: none;
            padding: 4px;
        }}
        """
    )
    return box


class SettingsPanel(QWidget):
    """The slide-over settings surface."""

    changed = Signal(dict)
    closeRequested = Signal()

    DEFAULTS = {
        "assistant_name": ASSISTANT_NAME_DEFAULT,
        "voice_engine": "piper",
        "voice": "Ryan — en_US-ryan-high",
        "speed": 1.0,
        "accent": 0,
        "compact": False,
        "provider": "Ollama",
        # Per-provider model + API key, so switching providers keeps every
        # other provider's saved model/key intact instead of overwriting a
        # single shared field.
        "models": dict(PROVIDER_DEFAULT_MODELS),
        "api_keys": {p: "" for p in PROVIDERS if p != "Ollama"},
        "theme": "space_black",
        "hotkey": "ctrl+space",
        "auto_speak": True,
        "show_timestamps": True,
        "always_on_top": False,
        "core_animation": True,
        "widget_hover_effects": True,
        "auto_open_widgets": True,
        "show_live_transcript": True,
        "reduced_motion": False,
        "voice_preload": True,
        "targeted_memory": True,
        "show_memory_sources": True,
        "confirm_destructive": True,
        "confirm_external_actions": True,
        "confirm_commands": True,
        "layout_snap": True,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = dict(self.DEFAULTS)
        self._loading = False
        self.setAttribute(Qt.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # header
        header = QHBoxLayout()
        header.setContentsMargins(22, 18, 14, 10)
        title = QLabel("JARVIS Control Matrix")
        title.setStyleSheet(
            f"color: {TEXT}; font-family: {FONT_FAMILY}; font-size: {FONT_H3}px;"
            "font-weight: 700; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch(1)
        self.close_btn = IconButton("x", size=32, tooltip="Close settings")
        self.close_btn.clicked.connect(self.closeRequested.emit)
        header.addWidget(self.close_btn)
        outer.addLayout(header)

        # scroll body
        self._body = QWidget()
        self._body.setAttribute(Qt.WA_TranslucentBackground)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(18, 4, 18, 24)
        body_layout.setSpacing(18)

        self._build_assistant(body_layout)
        self._build_voice(body_layout)
        self._build_appearance(body_layout)
        self._build_provider(body_layout)
        self._build_api_keys(body_layout)
        self._build_jarvis_runtime(body_layout)
        self._build_preferences(body_layout)
        body_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; border: none; margin: 2px 2px 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,0.12); border-radius: 3px; min-height: 24px; }"
            "QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.22); }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        )
        self._scroll.setWidget(self._body)
        outer.addWidget(self._scroll, 1)

    # ------------------------------------------------------------------ #
    # sections
    # ------------------------------------------------------------------ #
    def _build_assistant(self, layout):
        card = _SectionCard("ASSISTANT")
        self._name_edit = QLineEdit()
        self._name_edit.setText(self._settings["assistant_name"])
        self._name_edit.setFixedHeight(36)
        self._name_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background: {BG_3};
                border: 1px solid {BORDER};
                border-radius: 10px;
                color: {TEXT};
                font-family: {FONT_FAMILY};
                font-size: {FONT_BODY}px;
                padding: 0 12px;
            }}
            QLineEdit:focus {{ border: 1px solid rgba(124,130,255,0.55); }}
            """
        )
        self._name_edit.textChanged.connect(self._on_name_changed)
        card.body_layout.addWidget(_LabeledRow("Name", self._name_edit))
        card.body_layout.addWidget(
            _label(
                "Your personal AI companion — always available, "
                "private and tailored to how you work.",
                TEXT_FAINT, FONT_SMALL, 400, wrap=True,
            )
        )
        layout.addWidget(card)

    def _build_voice(self, layout):
        card = _SectionCard("VOICE")

        row_engine = QHBoxLayout()
        row_engine.setSpacing(10)
        row_engine.addWidget(_label("Engine"))
        self._engine_combo = _combo(["piper", "kokoro"])
        self._engine_combo.setMaximumWidth(240)
        self._engine_combo.setCurrentText(self._settings["voice_engine"])
        self._engine_combo.currentTextChanged.connect(self._on_engine_changed)
        row_engine.addWidget(self._engine_combo, 1)
        card.body_layout.addLayout(row_engine)

        row_voice = QHBoxLayout()
        row_voice.setSpacing(10)
        row_voice.addWidget(_label("Voice"))
        initial_options = KOKORO_VOICE_OPTIONS if self._settings["voice_engine"] == "kokoro" else PIPER_VOICE_OPTIONS
        self._voice_combo = _combo([label for _, label in initial_options], [value for value, _ in initial_options])
        self._voice_combo.setMaximumWidth(240)
        voice_idx = self._voice_combo.findData(self._settings["voice"])
        if voice_idx >= 0:
            self._voice_combo.setCurrentIndex(voice_idx)
        else:
            self._voice_combo.setCurrentText(self._settings["voice"])
        self._voice_combo.currentTextChanged.connect(self._on_voice_changed)
        row_voice.addWidget(self._voice_combo, 1)
        card.body_layout.addLayout(row_voice)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.addWidget(_label("Speed"))
        self._speed_slider = QSlider(Qt.Horizontal)
        self._speed_slider.setRange(60, 140)
        self._speed_slider.setValue(int(self._settings["speed"] * 100))
        self._speed_slider.setFixedHeight(22)
        self._speed_slider.setStyleSheet(
            f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: {BG_4};
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {ACCENT};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border-radius: 7px;
                background: #FFFFFF;
                border: 2px solid {ACCENT};
            }}
            """
        )
        self._speed_label = _label(f"{self._settings['speed']:.1f}×", TEXT_SOFT, FONT_TINY)
        self._speed_slider.valueChanged.connect(self._on_speed_changed)
        row2.addWidget(self._speed_slider, 1)
        row2.addWidget(self._speed_label)
        card.body_layout.addLayout(row2)
        layout.addWidget(card)

    def _build_appearance(self, layout):
        card = _SectionCard("APPEARANCE")
        self._theme_cards = []
        for key, title in [
            ("space_black", "Space Black — deep glass"),
            ("light_brown", "Light Brown — warm parchment"),
            ("space_mint", "Space Mint — teal glass"),
        ]:
            subtitle = THEME_NAMES.get(key, key)
            opt = _OptionCard(title, subtitle,
                              selected=(key == self._settings["theme"]))
            opt.clicked.connect(lambda k=key: self._on_theme_chosen(k))
            self._theme_cards.append(opt)
            card.body_layout.addWidget(opt)
        card.body_layout.addWidget(
            _label("Theme applies when the app restarts.", TEXT_FAINT, FONT_SMALL, 400,
                   wrap=True)
        )

        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(10)
        swatch_row.addStretch(1)
        self._swatches = []
        for idx, (a, b) in enumerate(ACCENTS):
            sw = _Swatch(a, b, selected=(idx == self._settings["accent"]))
            sw.clicked.connect(lambda i=idx: self._on_accent_chosen(i))
            swatch_row.addWidget(sw)
            self._swatches.append(sw)
        swatch_row.addStretch(1)
        card.body_layout.addLayout(swatch_row)
        card.body_layout.addWidget(
            _label("Accent color", TEXT_FAINT, FONT_SMALL, 400)
        )
        layout.addWidget(card)

    def _build_provider(self, layout):
        card = _SectionCard("PROVIDER")

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_label("Provider"))
        self._provider_combo = _combo(PROVIDERS)
        self._provider_combo.setCurrentText(self._settings["provider"])
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        row.addWidget(self._provider_combo, 1)
        card.body_layout.addLayout(row)

        self._provider_subtitle = _label(
            PROVIDER_SUBTITLES.get(self._settings["provider"], ""),
            TEXT_FAINT, FONT_TINY, 400, wrap=True,
        )
        card.body_layout.addWidget(self._provider_subtitle)

        def _line_edit(placeholder, password=False):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            if password:
                edit.setEchoMode(QLineEdit.Password)
            edit.setFixedHeight(36)
            edit.setStyleSheet(
                f"""
                QLineEdit {{
                    background: {BG_3};
                    border: 1px solid {BORDER};
                    border-radius: 10px;
                    color: {TEXT};
                    font-family: {FONT_FAMILY};
                    font-size: {FONT_SMALL}px;
                    padding: 0 12px;
                }}
                QLineEdit:focus {{ border: 1px solid {BORDER_STRONG}; }}
                QLineEdit:disabled {{ color: {TEXT_FAINT}; background: {BG_2}; }}
                """
            )
            return edit

        self._model_edit = _line_edit("Model id")
        self._model_edit.textChanged.connect(self._on_model_text_changed)
        card.body_layout.addWidget(_LabeledRow("Model", self._model_edit))

        # API key row: always visible. Local providers (Ollama) disable it;
        # cloud providers enable it. The Save button writes straight to the
        # project .env so the backend picks the key up on the next restart.
        key_line = QHBoxLayout()
        key_line.setSpacing(8)
        self._key_edit = _line_edit("API key (stored locally)", password=True)
        self._key_edit.textChanged.connect(self._on_key_changed)
        key_line.addWidget(self._key_edit, 1)
        self._key_save_btn = QPushButton("Save")
        self._key_save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._key_save_btn.setFixedHeight(36)
        self._key_save_btn.setStyleSheet(
            f"""
            QPushButton {{
                background: {ACCENT};
                border: none;
                border-radius: 10px;
                color: #FFFFFF;
                font-family: {FONT_FAMILY};
                font-size: {FONT_SMALL}px;
                font-weight: 600;
                padding: 0 16px;
            }}
            QPushButton:hover {{ background: {ACCENT_2}; }}
            QPushButton:pressed {{ background: {ACCENT_DEEP}; }}
            """
        )
        self._key_save_btn.clicked.connect(self._on_key_saved)
        key_line.addWidget(self._key_save_btn)
        self._key_saved = _label("Saved ✓", ACCENT, FONT_TINY, 600)
        self._key_saved.setVisible(False)
        key_line.addWidget(self._key_saved)
        card.body_layout.addLayout(key_line)

        self._key_hint = _label(
            "Ollama is local — no API key needed.",
            TEXT_FAINT, FONT_TINY, 400, wrap=True,
        )
        card.body_layout.addWidget(self._key_hint)
        self._sync_provider_fields()
        layout.addWidget(card)

    def _build_api_keys(self, layout):
        card = _SectionCard("API KEYS")

        def _key_edit(placeholder, password=False):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            if password:
                edit.setEchoMode(QLineEdit.Password)
            edit.setFixedHeight(36)
            edit.setStyleSheet(
                f"""
                QLineEdit {{
                    background: {BG_3};
                    border: 1px solid {BORDER};
                    border-radius: 10px;
                    color: {TEXT};
                    font-family: {FONT_FAMILY};
                    font-size: {FONT_SMALL}px;
                    padding: 0 12px;
                }}
                QLineEdit:focus {{ border: 1px solid {BORDER_STRONG}; }}
                """
            )
            return edit

        def _save_button():
            btn = QPushButton("Save")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(36)
            btn.setStyleSheet(
                f"""
                QPushButton {{
                    background: {BG_3};
                    border: 1px solid {BORDER_STRONG};
                    border-radius: 10px;
                    color: {TEXT};
                    font-family: {FONT_FAMILY};
                    font-size: {FONT_SMALL}px;
                    font-weight: 600;
                    padding: 0 16px;
                }}
                QPushButton:hover {{ background: {BG_4}; border: 1px solid {ACCENT}; }}
                QPushButton:pressed {{ background: {BG_4}; }}
                """
            )
            return btn

        def _saved_label():
            lbl = _label("Saved ✓", ACCENT, FONT_TINY, 600)
            lbl.setVisible(False)
            return lbl

        # YouTube Data API v3 key
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._youtube_edit = _key_edit("YouTube Data API v3 key", password=True)
        row1.addWidget(self._youtube_edit, 1)
        self._youtube_save_btn = _save_button()
        self._youtube_save_btn.clicked.connect(self._on_youtube_key_saved)
        row1.addWidget(self._youtube_save_btn)
        self._youtube_saved = _saved_label()
        row1.addWidget(self._youtube_saved)
        card.body_layout.addLayout(row1)
        card.body_layout.addWidget(
            _label("Used by YouTube recommendations/playback (googleapis.com).",
                   TEXT_FAINT, FONT_TINY, 400, wrap=True)
        )

        # Tavily research key
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self._tavily_edit = _key_edit("Tavily search API key", password=True)
        row2.addWidget(self._tavily_edit, 1)
        self._tavily_save_btn = _save_button()
        self._tavily_save_btn.clicked.connect(self._on_tavily_key_saved)
        row2.addWidget(self._tavily_save_btn)
        self._tavily_saved = _saved_label()
        row2.addWidget(self._tavily_saved)
        card.body_layout.addLayout(row2)
        card.body_layout.addWidget(
            _label("Used by the in-depth research pipeline (tavily.com).",
                   TEXT_FAINT, FONT_TINY, 400, wrap=True)
        )

        layout.addWidget(card)

    def _sync_provider_fields(self):
        provider = self._settings.get("provider", "Ollama")
        local = provider == "Ollama"
        self._provider_subtitle.setText(PROVIDER_SUBTITLES.get(provider, ""))
        self._key_edit.setEnabled(not local)
        self._key_save_btn.setEnabled(not local)
        self._key_hint.setVisible(True)
        self._key_hint.setText(
            "Ollama is local — no API key needed."
            if local else
            "Saved to the project .env — takes effect after a restart."
        )
        model = self._settings.get("models", {}).get(provider, PROVIDER_DEFAULT_MODELS.get(provider, ""))
        key = self._settings.get("api_keys", {}).get(provider, "")
        if not key:
            key = read_env().get(PROVIDER_ENV_KEY.get(provider, ""), "")
        block = self._model_edit.blockSignals(True)
        self._model_edit.setText(model)
        self._model_edit.blockSignals(block)
        block = self._key_edit.blockSignals(True)
        self._key_edit.setText(key)
        self._key_edit.blockSignals(block)

    def _build_preferences(self, layout):
        card = _SectionCard("PREFERENCES")
        self._auto_speak = _Toggle(self._settings["auto_speak"])
        self._auto_speak.toggled.connect(self._on_toggle)
        self._auto_speak.setProperty("key", "auto_speak")
        card.body_layout.addWidget(_LabeledRow("Speak responses aloud", self._auto_speak))

        self._timestamps = _Toggle(self._settings["show_timestamps"])
        self._timestamps.toggled.connect(self._on_toggle)
        self._timestamps.setProperty("key", "show_timestamps")
        card.body_layout.addWidget(_LabeledRow("Show message timestamps", self._timestamps))

        self._ontop = _Toggle(self._settings["always_on_top"])
        self._ontop.toggled.connect(self._on_toggle)
        self._ontop.setProperty("key", "always_on_top")
        card.body_layout.addWidget(_LabeledRow("Keep window on top", self._ontop))

        self._hotkey_edit = QLineEdit()
        self._hotkey_edit.setPlaceholderText("e.g. ctrl+alt+n")
        self._hotkey_edit.setText(self._settings["hotkey"])
        self._hotkey_edit.setFixedHeight(32)
        self._hotkey_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background: {BG_3};
                border: 1px solid {BORDER};
                border-radius: 8px;
                color: {TEXT};
                font-family: {FONT_FAMILY};
                font-size: {FONT_SMALL}px;
                padding: 0 10px;
            }}
            QLineEdit:focus {{ border: 1px solid {BORDER_STRONG}; }}
            """
        )
        self._hotkey_edit.textChanged.connect(self._on_hotkey_changed)
        card.body_layout.addWidget(_LabeledRow("Voice hotkey", self._hotkey_edit))
        card.body_layout.addWidget(
            _label("Global shortcut to open JARVIS and talk (press again to stop).",
                   TEXT_FAINT, FONT_TINY, 400, wrap=True)
        )
        layout.addWidget(card)

    def _build_jarvis_runtime(self, layout):
        """Controls specific to the intelligence core and autonomous UI."""
        card = _SectionCard("JARVIS RUNTIME")
        rows = [
            ("core_animation", "Animate intelligence core"),
            ("widget_hover_effects", "Widget hover energy effects"),
            ("auto_open_widgets", "Open useful widgets automatically"),
            ("show_live_transcript", "Show live voice transcript"),
            ("reduced_motion", "Reduce non-essential motion"),
            ("voice_preload", "Preload speech models at launch"),
            ("targeted_memory", "Use targeted task memory"),
            ("show_memory_sources", "Show memory source details"),
            ("layout_snap", "Keep widgets inside safe layout zones"),
        ]
        safety_rows = [
            ("confirm_destructive", "Confirm destructive file actions"),
            ("confirm_external_actions", "Confirm messages and external actions"),
            ("confirm_commands", "Confirm command execution"),
        ]
        self._jarvis_toggles = {}
        for key, label in rows:
            control = _Toggle(self._settings[key])
            control.setProperty("key", key)
            control.toggled.connect(self._on_toggle)
            self._jarvis_toggles[key] = control
            card.body_layout.addWidget(_LabeledRow(label, control))
        card.body_layout.addWidget(_label("SAFETY", TEXT_FAINT, FONT_TINY, 600))
        for key, label in safety_rows:
            control = _Toggle(self._settings[key])
            control.setProperty("key", key)
            control.toggled.connect(self._on_toggle)
            self._jarvis_toggles[key] = control
            card.body_layout.addWidget(_LabeledRow(label, control))
        card.body_layout.addWidget(
            _label(
                "Provider, model, API keys, voice, appearance, hotkey, memory, autonomy, "
                "widget behavior, and safety are controlled from this one matrix.",
                TEXT_FAINT, FONT_TINY, 400, wrap=True,
            )
        )
        layout.addWidget(card)

    # ------------------------------------------------------------------ #
    # persistence + signal plumbing
    # ------------------------------------------------------------------ #
    def load(self, settings: dict):
        self._loading = True
        try:
            merged = dict(self.DEFAULTS)
            merged["models"] = dict(self.DEFAULTS["models"])
            merged["api_keys"] = dict(self.DEFAULTS["api_keys"])
            incoming = dict(settings)

            # Migrate the old single "model" / "api_key" schema (pre
            # multi-provider) into the new per-provider dicts, best-effort.
            legacy_provider = incoming.get("provider") or "Ollama"
            legacy_model = incoming.get("model")
            if isinstance(legacy_model, str) and legacy_model:
                short = legacy_model.split("—")[-1].strip() if "—" in legacy_model else legacy_model
                if legacy_provider in merged["models"]:
                    merged["models"][legacy_provider] = short or merged["models"][legacy_provider]
            legacy_key = incoming.get("api_key")
            if isinstance(legacy_key, str) and legacy_key and legacy_provider in merged["api_keys"]:
                merged["api_keys"][legacy_provider] = legacy_key

            for k, v in incoming.items():
                if k not in self.DEFAULTS:
                    continue
                if k == "models" and isinstance(v, dict):
                    merged["models"].update(v)
                elif k == "api_keys" and isinstance(v, dict):
                    merged["api_keys"].update(v)
                elif k not in ("models", "api_keys"):
                    merged[k] = v

            self._settings = merged
            self._name_edit.setText(merged["assistant_name"])
            self._engine_combo.setCurrentText(merged["voice_engine"])
            merged["voice"] = self._populate_voice_options(merged["voice_engine"], merged["voice"])
            self._settings["voice"] = merged["voice"]
            self._speed_slider.setValue(int(merged["speed"] * 100))
            self._provider_combo.setCurrentText(merged["provider"])
            self._hotkey_edit.setText(merged["hotkey"])
            self._auto_speak.setChecked(merged["auto_speak"])
            self._timestamps.setChecked(merged["show_timestamps"])
            self._ontop.setChecked(merged["always_on_top"])
            for key, control in self._jarvis_toggles.items():
                control.setChecked(bool(merged[key]))
            env = read_env()
            self._youtube_edit.setText(env.get("YOUTUBE_API_KEY", ""))
            self._tavily_edit.setText(env.get("TAVILY_API_KEY", ""))
            for idx, sw in enumerate(self._swatches):
                sw.set_selected(idx == merged["accent"])
            for card, key in zip(self._theme_cards, ["space_black", "light_brown", "space_mint"]):
                card.set_selected(key == merged["theme"])
            self._sync_provider_fields()
        finally:
            self._loading = False

    def settings(self) -> dict:
        return dict(self._settings)

    def _emit(self):
        if self._loading:
            return
        self.changed.emit(dict(self._settings))

    def _on_name_changed(self, text):
        self._settings["assistant_name"] = text
        self._emit()

    def _on_theme_chosen(self, key: str):
        self._settings["theme"] = key
        for card, k in zip(self._theme_cards, ["space_black", "light_brown", "space_mint"]):
            card.set_selected(k == key)
        self._emit()

    def _on_engine_changed(self, text):
        self._settings["voice_engine"] = text
        self._settings["voice"] = self._populate_voice_options(text, self._settings.get("voice", ""))
        self._emit()

    def _populate_voice_options(self, engine: str, selected: str) -> str:
        options = KOKORO_VOICE_OPTIONS if engine == "kokoro" else PIPER_VOICE_OPTIONS
        valid = {value for value, _label_text in options}
        if selected not in valid:
            selected = "Puck — am_puck" if engine == "kokoro" else "Ryan — en_US-ryan-high"
        blocked = self._voice_combo.blockSignals(True)
        self._voice_combo.clear()
        for value, label_text in options:
            self._voice_combo.addItem(label_text, value)
        index = self._voice_combo.findData(selected)
        self._voice_combo.setCurrentIndex(max(0, index))
        self._voice_combo.blockSignals(blocked)
        return selected

    def _on_voice_changed(self, text):
        data = self._voice_combo.currentData()
        self._settings["voice"] = data or text
        self._emit()

    def _on_provider_changed(self, text):
        self._settings["provider"] = text
        self._sync_provider_fields()
        self._emit()

    def _on_hotkey_changed(self, text):
        self._settings["hotkey"] = (text or "").strip().lower()
        self._emit()

    def _on_speed_changed(self, value):
        self._settings["speed"] = value / 100.0
        self._speed_label.setText(f"{value / 100.0:.1f}×")
        self._emit()

    def _on_accent_chosen(self, idx):
        self._settings["accent"] = idx
        for i, sw in enumerate(self._swatches):
            sw.set_selected(i == idx)
        self._emit()

    def _on_model_text_changed(self, text):
        provider = self._settings.get("provider", "Ollama")
        self._settings.setdefault("models", {})[provider] = text
        self._emit()

    def _on_key_changed(self, text):
        provider = self._settings.get("provider", "Ollama")
        if provider == "Ollama":
            return
        self._settings.setdefault("api_keys", {})[provider] = text
        self._emit()

    def _on_key_saved(self):
        provider = self._settings.get("provider", "Ollama")
        env_var = PROVIDER_ENV_KEY.get(provider)
        if not env_var:
            return
        value = self._key_edit.text().strip()
        update_env_file({env_var: value})
        self._settings.setdefault("api_keys", {})[provider] = value
        self._emit()
        self._flash_saved(self._key_saved)

    def _on_youtube_key_saved(self):
        value = self._youtube_edit.text().strip()
        update_env_file({"YOUTUBE_API_KEY": value})
        self._settings.setdefault("api_keys", {})["YouTube"] = value
        self._emit()
        self._flash_saved(self._youtube_saved)

    def _on_tavily_key_saved(self):
        value = self._tavily_edit.text().strip()
        update_env_file({"TAVILY_API_KEY": value})
        self._settings.setdefault("api_keys", {})["Tavily"] = value
        self._emit()
        self._flash_saved(self._tavily_saved)

    def _flash_saved(self, label: QLabel):
        label.setVisible(True)
        QTimer.singleShot(1800, lambda: label.setVisible(False))

    def _on_toggle(self, checked):
        key = self.sender().property("key")
        if key:
            self._settings[key] = checked
            self._emit()

    # ------------------------------------------------------------------ #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)
        painter.fillPath(path, QColor(BG_2))
        highlight = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        highlight.setColorAt(0.0, QColor(255, 255, 255, 18))
        highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, highlight)
        painter.setPen(QPen(QColor(BORDER), 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)


class _Swatch(QWidget):
    """A clickable accent-color swatch with a selection ring."""

    clicked = Signal()

    def __init__(self, c1: str, c2: str, selected=False, parent=None):
        super().__init__(parent)
        self.c1 = c1
        self.c2 = c2
        self._selected = selected
        self._hovered = False
        self.setFixedSize(34, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, selected: bool):
        self._selected = selected
        self.update()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
        if self._selected or self._hovered:
            ring = QColor(ACCENT)
            ring.setAlpha(255 if self._selected else 120)
            painter.setBrush(Qt.NoBrush)
            pen = QPen(ring, 2.0)
            painter.setPen(pen)
            painter.drawEllipse(rect.adjusted(-2.5, -2.5, 2.5, 2.5))
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(self.c1))
        gradient.setColorAt(1.0, QColor(self.c2))
        painter.setBrush(gradient)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect)
