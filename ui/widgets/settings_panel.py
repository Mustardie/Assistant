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

from PySide6.QtCore import Qt, QRectF, Signal, QPropertyAnimation, QEasingCurve, Property
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient, QPainterPath, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QLineEdit,
    QComboBox, QSlider, QFrame,
)

from .. import icons
from ..theme import (
    BG_2, BG_3, BG_4, BORDER, BORDER_STRONG, TEXT, TEXT_SOFT, TEXT_FAINT,
    ACCENT, ACCENT_2, FONT_FAMILY, FONT_BODY, FONT_SMALL, FONT_TINY, FONT_H3,
    THEME_NAMES, ASSISTANT_NAME as ASSISTANT_NAME_DEFAULT,
)
from .icon_button import IconButton

ACCENTS = [
    ("#6E8BFF", "#9B6DFF"),
    ("#34C6F2", "#5B8DFF"),
    ("#4CD6A5", "#2FAF8B"),
    ("#FF8A65", "#FF5C8A"),
    ("#E8D44D", "#E8A24D"),
]

MODEL_OPTIONS = [
    ("Ollama — qwen2.5vl:7b (local multimodal)",
     "Local · vision + reasoning · no cloud"),
    ("Ollama — gemma3:12b (local)",
     "Local · fast general chat"),
    ("OpenRouter — openrouter/free",
     "Cloud reasoning · needs an API key"),
    ("Gemini — gemini-2.5-flash",
     "Google cloud · needs an API key"),
]

PROVIDERS = ["Ollama", "OpenRouter", "Gemini"]


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
        if self._anim:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"progress", self)
        self._anim.setDuration(180)
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

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
    """Label + control on one row (used for API key, toggles, sliders)."""

    def __init__(self, label: str, control: QWidget, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(10)
        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"color: {TEXT}; font-family: {FONT_FAMILY}; font-size: {FONT_SMALL}px;"
            "font-weight: 500; background: transparent;"
        )
        layout.addWidget(lbl, 1)
        layout.addWidget(control)


def _label(text, color=TEXT_SOFT, size=FONT_SMALL, weight=500):
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {color}; font-family: {FONT_FAMILY}; font-size: {size}px;"
        f"font-weight: {weight}; background: transparent;"
    )
    return lbl


def _combo(items) -> QComboBox:
    box = QComboBox()
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
        "model": "Ollama — qwen2.5vl:7b (local multimodal)",
        "voice_engine": "piper",
        "voice": "Ryan — en_US-ryan-high",
        "speed": 1.0,
        "accent": 0,
        "compact": False,
        "provider": "Ollama",
        "api_key": "",
        "theme": "space_black",
        "hotkey": "ctrl+space",
        "auto_speak": True,
        "show_timestamps": True,
        "always_on_top": False,
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
        title = QLabel("Settings")
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
        body_layout.addStretch(1)

        self._build_assistant(body_layout)
        self._build_model(body_layout)
        self._build_voice(body_layout)
        self._build_appearance(body_layout)
        self._build_api(body_layout)
        self._build_preferences(body_layout)

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
                TEXT_FAINT, FONT_SMALL, 400,
            )
        )
        layout.addWidget(card)

    def _build_model(self, layout):
        card = _SectionCard("MODEL")
        self._model_cards = []
        for title, subtitle in MODEL_OPTIONS:
            opt = _OptionCard(title, subtitle, selected=(title == self._settings["model"]))
            opt.clicked.connect(lambda t=title, o=opt: self._on_model_chosen(t, o))
            self._model_cards.append(opt)
            card.body_layout.addWidget(opt)
        layout.addWidget(card)

    def _build_voice(self, layout):
        card = _SectionCard("VOICE")
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        row1.addWidget(_label("Engine"))
        self._engine_combo = _combo(["piper", "kokoro"])
        self._engine_combo.setCurrentText(self._settings["voice_engine"])
        self._engine_combo.currentTextChanged.connect(self._on_engine_changed)
        row1.addWidget(self._engine_combo)
        row1.addWidget(_label("Voice"))
        self._voice_combo = _combo([
            "Ryan — en_US-ryan-high",
            "Amy — en_US-amy-medium",
            "Kristin — en_US-kristin-medium",
            "LibriTTS — en_US-libritts-high",
        ])
        self._voice_combo.setCurrentText(self._settings["voice"])
        self._voice_combo.currentTextChanged.connect(self._on_voice_changed)
        row1.addWidget(self._voice_combo, 1)
        card.body_layout.addLayout(row1)

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
            _label("Theme applies when the app restarts.", TEXT_FAINT, FONT_SMALL, 400)
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

    def _build_api(self, layout):
        card = _SectionCard("API")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_label("Provider"))
        self._provider_combo = _combo(PROVIDERS)
        self._provider_combo.setCurrentText(self._settings["provider"])
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        row.addWidget(self._provider_combo, 1)
        card.body_layout.addLayout(row)

        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText("API key (stored locally)")
        self._key_edit.setEchoMode(QLineEdit.Password)
        self._key_edit.setFixedHeight(36)
        self._key_edit.setStyleSheet(
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
        self._key_edit.textChanged.connect(self._on_key_changed)
        card.body_layout.addWidget(_LabeledRow("API key", self._key_edit))

        self._key_hint = _label(
            "Ollama is local — no API key needed.",
            TEXT_FAINT, FONT_TINY, 400,
        )
        card.body_layout.addWidget(self._key_hint)
        self._sync_key_field()
        layout.addWidget(card)

    def _sync_key_field(self):
        provider = self._settings.get("provider", "Ollama")
        local = provider == "Ollama"
        self._key_edit.setEnabled(not local)
        self._key_hint.setText(
            "Ollama is local — no API key needed."
            if local else
            "Key is stored locally on this PC and used by the backend."
        )

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
            _label("Global shortcut to open Nova and talk (press again to stop).",
                   TEXT_FAINT, FONT_TINY, 400)
        )
        layout.addWidget(card)

    # ------------------------------------------------------------------ #
    # persistence + signal plumbing
    # ------------------------------------------------------------------ #
    def load(self, settings: dict):
        self._loading = True
        try:
            merged = dict(self.DEFAULTS)
            merged.update({k: v for k, v in settings.items() if k in self.DEFAULTS})
            self._settings = merged
            self._name_edit.setText(merged["assistant_name"])
            self._engine_combo.setCurrentText(merged["voice_engine"])
            self._voice_combo.setCurrentText(merged["voice"])
            self._speed_slider.setValue(int(merged["speed"] * 100))
            self._provider_combo.setCurrentText(merged["provider"])
            self._hotkey_edit.setText(merged["hotkey"])
            self._auto_speak.setChecked(merged["auto_speak"])
            self._timestamps.setChecked(merged["show_timestamps"])
            self._ontop.setChecked(merged["always_on_top"])
            for idx, sw in enumerate(self._swatches):
                sw.set_selected(idx == merged["accent"])
            for card in self._model_cards:
                card.set_selected(card.title == merged["model"])
            for card, key in zip(self._theme_cards, ["space_black", "light_brown", "space_mint"]):
                card.set_selected(key == merged["theme"])
            self._sync_key_field()
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

    def _on_model_chosen(self, title, opt):
        self._settings["model"] = title
        for card in self._model_cards:
            card.set_selected(card is opt)
        self._emit()

    def _on_theme_chosen(self, key: str):
        self._settings["theme"] = key
        for card, k in zip(self._theme_cards, ["space_black", "light_brown", "space_mint"]):
            card.set_selected(k == key)
        self._emit()

    def _on_engine_changed(self, text):
        self._settings["voice_engine"] = text
        self._emit()

    def _on_voice_changed(self, text):
        self._settings["voice"] = text
        self._emit()

    def _on_provider_changed(self, text):
        self._settings["provider"] = text
        self._sync_key_field()
        current = self._settings.get("model", "")
        # keep the model choice consistent with the selected provider
        if text.lower() not in current.lower():
            for card in self._model_cards:
                if text.lower() in card.title.lower():
                    self._settings["model"] = card.title
                    card.set_selected(True)
                else:
                    card.set_selected(False)
        else:
            for card in self._model_cards:
                card.set_selected(card.title == current)
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

    def _on_key_changed(self, text):
        self._settings["api_key"] = text
        self._emit()

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
