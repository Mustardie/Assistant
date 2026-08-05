"""
NovaWindow — the premium standalone Nova desktop app.

A frameless, rounded, dark-glass window built from the reusable widgets:

    TitleBar → Sidebar → Chat (messages + input dock) → Settings slide-over

UI ONLY. The window talks to the assistant through signals:

    textSubmitted(str)      — user sent a message (run your agent)
    voicePressed()          — user pressed the mic (start voice session)

And receives state through methods:

    append_assistant(text)  — show/stream an assistant reply
    set_voice_state(state)  — "idle"|"listening"|"thinking"|"speaking"
    begin_reply()           — show the typing indicator

Conversations and settings persist to data/ as JSON, with no backend
knowledge whatsoever. Wire it up in main.py exactly like the old overlay:

    from ui.nova_window import NovaWindow
    window = NovaWindow()
    window.textSubmitted.connect(agent.run_threaded)
    bridge.responseReady.connect(window.append_assistant)
    window.voicePressed.connect(start_voice)
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QRectF, QSize, QTimer, QPropertyAnimation, \
    QParallelAnimationGroup, QEasingCurve, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient, QPainterPath, QFont, QCloseEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel, QFileDialog,
    QGraphicsOpacityEffect, QFrame,
)

from . import icons
from .theme import (
    BG_0, BG_1, BG_2, BG_3, BORDER, TEXT, TEXT_SOFT, TEXT_FAINT, ACCENT,
    ACCENT_2, FONT_FAMILY, FONT_BODY, FONT_SMALL, FONT_H3,
    SHELL_MARGIN, SHELL_RADIUS, TITLEBAR_H, SIDEBAR_W, SIDEBAR_W_COLLAPSED,
    SIDEBAR_RADIUS, WINDOW_W, WINDOW_H, WINDOW_MIN_W, WINDOW_MIN_H,
    INPUT_H, STATUS_LABELS, FONT_TINY, ASSISTANT_NAME,
)
from .animations import animate_property
from .widgets import (
    TitleBar, Sidebar, ChatInputBar, AttachmentMenu, SettingsPanel,
    UserBubble, AssistantRow, TypingIndicator, NovaAvatar,
)
from .widgets.message_bubble import fade_in

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CONVERSATIONS_FILE = _DATA_DIR / "nova_conversations.json"
_SETTINGS_FILE = _DATA_DIR / "nova_settings.json"

WELCOME_SUGGESTIONS = [
    "Summarize my Gmail",
    "What's on my screen?",
    "Find my recent notes",
    "Plan a weekend trip",
]


def _relative_time(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return time.strftime("%b %d", time.localtime(ts))


class _Welcome(QWidget):
    """The empty-state hero: orb, greeting, suggestion chips."""

    suggestionPicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addStretch(3)

        self.greeting = QLabel("Good evening")
        self.greeting.setAlignment(Qt.AlignCenter)
        self.greeting.setStyleSheet(
            f"color: {TEXT}; font-family: {FONT_FAMILY}; font-size: 26px;"
            "font-weight: 700; background: transparent; letter-spacing: 0.2px;"
        )
        layout.addWidget(self.greeting)

        self.sub = QLabel(f"{ASSISTANT_NAME} is here. Ask anything — or start with one of these.")
        self.sub.setAlignment(Qt.AlignCenter)
        self.sub.setStyleSheet(
            f"color: {TEXT_FAINT}; font-family: {FONT_FAMILY}; font-size: {FONT_BODY}px;"
            "font-weight: 400; background: transparent;"
        )
        layout.addWidget(self.sub)

        chips = QHBoxLayout()
        chips.setSpacing(10)
        chips.addStretch(1)
        self._chip_widgets = []
        for text in WELCOME_SUGGESTIONS:
            chip = _SuggestionChip(text)
            chip.picked.connect(self.suggestionPicked.emit)
            chips.addWidget(chip)
            self._chip_widgets.append(chip)
        chips.addStretch(1)
        layout.addLayout(chips)

        layout.addStretch(5)

    def set_greeting(self, name: str = "there"):
        hour = time.localtime().tm_hour
        if hour < 5:
            greeting = "Up late?"
        elif hour < 12:
            greeting = "Good morning"
        elif hour < 18:
            greeting = "Good afternoon"
        else:
            greeting = "Good evening"
        self.greeting.setText(greeting)


class _SuggestionChip(QWidget):
    """A pill-shaped suggestion that submits on click."""

    picked = Signal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text = text
        self._hovered = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(38)

    def sizeHint(self):
        return QSize(len(self.text) * 7 + 36, 38)

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
            self.picked.emit(self.text)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        if self._hovered:
            painter.fillPath(path, QColor(BG_3))
        else:
            painter.fillPath(path, QColor(BG_2))
        painter.setPen(QPen(QColor(BORDER), 1.0))
        painter.drawPath(path)
        painter.setPen(QColor(TEXT_SOFT))
        font = painter.font()
        font.setFamily(FONT_FAMILY)
        font.setPointSizeF(8.6)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self.text)


class _TypingRow(QWidget):
    """Avatar + typing indicator, shown while Nova thinks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.avatar = NovaAvatar(size=26)
        layout.addWidget(self.avatar, 0, Qt.AlignTop)
        self.indicator = TypingIndicator()
        layout.addWidget(self.indicator, 0, Qt.AlignTop)
        layout.addStretch(1)


class NovaWindow(QWidget):
    """The premium Nova desktop application window."""

    textSubmitted = Signal(str)
    voicePressed = Signal()
    settingsChanged = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)

        # ---- window chrome ----
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Window
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(WINDOW_MIN_W, WINDOW_MIN_H)
        self.resize(WINDOW_W, WINDOW_H)
        self.setWindowTitle(ASSISTANT_NAME)

        self._voice_state = "idle"
        self._maximized = False
        self._sidebar_collapsed = False
        self._settings_open = False
        self._reply_live = False
        self._pending_typing = False
        self._conversations = {}
        self._active_id = None
        self._attachments = []

        self._load_state()
        self._assistant_name = (
            (self._settings.get("assistant_name") or "").strip() or ASSISTANT_NAME
        )

        # ---- shell (the actual window body) ----
        self._shell = QWidget(self)
        self._shell.setAttribute(Qt.WA_TranslucentBackground)
        self._shell.setObjectName("shell")

        root = QVBoxLayout(self)
        root.setContentsMargins(SHELL_MARGIN, SHELL_MARGIN, SHELL_MARGIN, SHELL_MARGIN)
        root.addWidget(self._shell)

        shell_layout = QVBoxLayout(self._shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        self.title_bar = TitleBar()
        self.title_bar.sidebarToggle.connect(self.toggle_sidebar)
        self.title_bar.minimizeRequested.connect(self.showMinimized)
        self.title_bar.maximizeRequested.connect(self.toggle_maximized)
        self.title_bar.closeRequested.connect(self.hide)
        shell_layout.addWidget(self.title_bar)

        # ---- content row: sidebar + chat ----
        content = QWidget(self._shell)
        content.setAttribute(Qt.WA_TranslucentBackground)
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(10)

        self.sidebar = Sidebar()
        self.sidebar.setFixedWidth(SIDEBAR_W)
        self.sidebar.newChatRequested.connect(self.new_conversation)
        self.sidebar.conversationSelected.connect(self.open_conversation)
        self.sidebar.conversationDeleteRequested.connect(self.delete_conversation)
        self.sidebar.settingsRequested.connect(self.open_settings)
        self.sidebar.collapseRequested.connect(self.toggle_sidebar)
        content_layout.addWidget(self.sidebar)

        # ---- chat column ----
        chat = QWidget(content)
        chat.setAttribute(Qt.WA_TranslucentBackground)
        chat_layout = QVBoxLayout(chat)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        self._chat_header = QWidget(chat)
        self._chat_header.setFixedHeight(44)
        self._chat_header.setAttribute(Qt.WA_TranslucentBackground)
        header_layout = QHBoxLayout(self._chat_header)
        header_layout.setContentsMargins(18, 0, 14, 0)
        header_layout.setSpacing(10)

        self._conv_title = QLabel("")
        self._conv_title.setStyleSheet(
            f"color: {TEXT}; font-family: {FONT_FAMILY}; font-size: {FONT_H3}px;"
            "font-weight: 700; background: transparent;"
        )
        header_layout.addWidget(self._conv_title)

        self._model_chip = QLabel("")
        self._model_chip.setStyleSheet(
            f"color: {TEXT_SOFT}; font-family: {FONT_FAMILY}; font-size: {FONT_SMALL}px;"
            "font-weight: 500; background: transparent;"
        )
        header_layout.addWidget(self._model_chip)
        header_layout.addStretch(1)

        self._new_chat_small = self._make_small_button(
            "plus", "New conversation", self.new_conversation
        )
        header_layout.addWidget(self._new_chat_small)
        chat_layout.addWidget(self._chat_header)
        # messages area
        self._messages_host = QWidget(chat)
        self._messages_host.setAttribute(Qt.WA_TranslucentBackground)
        self._messages_layout = QVBoxLayout(self._messages_host)
        self._messages_layout.setContentsMargins(10, 6, 10, 6)
        self._messages_layout.setSpacing(18)
        self._messages_layout.addStretch(1)

        self._messages_scroll = QScrollArea(chat)
        self._messages_scroll.setWidgetResizable(True)
        self._messages_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._messages_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._messages_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._messages_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 8px; border: none; margin: 2px; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,0.10); border-radius: 4px; min-height: 28px; }"
            "QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.20); }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        )
        self._messages_scroll.setWidget(self._messages_host)
        chat_layout.addWidget(self._messages_scroll, 1)

        # input dock
        self.input_bar = ChatInputBar(chat)
        self.input_bar.submitted.connect(self._on_submit)
        self.input_bar.attachRequested.connect(self._on_attach_requested)
        self.input_bar.voicePressed.connect(self.voicePressed.emit)
        chat_layout.addWidget(self.input_bar)

        content_layout.addWidget(chat, 1)

        shell_layout.addWidget(content, 1)

        # ---- settings slide-over ----
        self.settings_panel = SettingsPanel(self._shell)
        self.settings_panel.closeRequested.connect(self.close_settings)
        self.settings_panel.changed.connect(self._on_settings_changed)
        self.settings_panel.hide()

        # ---- attachment menu ----
        self.attach_menu = AttachmentMenu()
        self.attach_menu.actionSelected.connect(self._on_attach_action)

        # ---- welcome ----
        self._welcome = _Welcome(self._messages_host)
        self._messages_layout.insertWidget(0, self._welcome)
        self._welcome.setVisible(False)
        self._welcome.suggestionPicked.connect(self._send_suggestion)

        self._typing_row = None

        # ---- persistence debounce ----
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_conversations)

        # initial conversation
        if not self._active_id:
            self.new_conversation(first=True)
        else:
            self._render_current()

        self._apply_settings(self._settings)
        self.title_bar.set_status(STATUS_LABELS["idle"])
        self.sidebar.set_status("idle")
        self.sidebar.set_conversations(self._sidebar_items())

    def _make_small_button(self, icon_name, tooltip, slot):
        from .widgets import IconButton
        btn = IconButton(icon_name, size=30, tooltip=tooltip)
        btn.clicked.connect(slot)
        return btn

    # ================================================================== #
    # persistence
    # ================================================================== #
    def _load_state(self):
        self._settings = dict(SettingsPanel.DEFAULTS)
        try:
            if _SETTINGS_FILE.exists():
                data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
                self._settings.update({k: v for k, v in data.items()
                                       if k in SettingsPanel.DEFAULTS})
        except Exception:
            logger.exception("Failed to load settings")
        try:
            if _CONVERSATIONS_FILE.exists():
                data = json.loads(_CONVERSATIONS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._conversations = data
                    self._active_id = data.get("_active")
        except Exception:
            logger.exception("Failed to load conversations")

    def _save_conversations(self):
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = dict(self._conversations)
            payload["_active"] = self._active_id
            _CONVERSATIONS_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception:
            logger.exception("Failed to save conversations")

    def _schedule_save(self):
        self._save_timer.start(600)

    def _save_settings(self):
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _SETTINGS_FILE.write_text(
                json.dumps(self._settings, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to save settings")

    def _on_settings_changed(self, settings: dict):
        self._settings = dict(settings)
        self._save_settings()
        self._apply_settings(settings)
        self.settingsChanged.emit(dict(settings))

    def _apply_settings(self, settings: dict):
        self.settings_panel.load(settings)
        name = (settings.get("assistant_name") or "").strip() or ASSISTANT_NAME
        self._assistant_name = name
        self.title_bar.set_name(name)
        self.sidebar.set_name(name)
        self.input_bar.set_name(name)
        self.setWindowTitle(name)
        self._welcome.sub.setText(
            f"{name} is here. Ask anything — or start with one of these."
        )
        self._rename_assistant_rows(name)

        provider = settings.get("provider", "Ollama")
        models = settings.get("models") or {}
        model = (models.get(provider) or settings.get("model") or "").strip()
        short = model.split("—")[-1].split("(")[0].strip() if "—" in model else model
        self._model_chip.setText(f"Model · {short or provider}")
        if settings.get("always_on_top"):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.show()

    def _rename_assistant_rows(self, name: str):
        count = self._messages_layout.count()
        for i in range(count):
            item = self._messages_layout.itemAt(i)
            w = item.widget()
            if isinstance(w, AssistantRow):
                w.set_name(name)

    def _sidebar_items(self):
        items = []
        for cid, conv in self._conversations.items():
            if cid == "_active":
                continue
            items.append({
                "id": cid,
                "title": conv.get("title", "Conversation"),
                "updated": conv.get("updated", 0),
            })
        items.sort(key=lambda c: c["updated"], reverse=True)
        return items

    # ================================================================== #
    # conversations
    # ================================================================== #
    def new_conversation(self, first=False):
        self._finish_typing()
        cid = uuid.uuid4().hex[:12]
        self._conversations[cid] = {
            "title": "New conversation",
            "messages": [],
            "updated": time.time(),
        }
        self._active_id = cid
        self._conv_title.setText("New conversation")
        self._clear_messages()
        self._show_welcome(True)
        self.sidebar.set_conversations(self._sidebar_items())
        self.sidebar.set_active(cid)
        if not first:
            self._schedule_save()
        self._on_voice_state("idle")
        QTimer.singleShot(0, self.input_bar.focus_input)

    def open_conversation(self, cid: str):
        if cid not in self._conversations:
            return
        self._active_id = cid
        self._render_current()
        self.sidebar.set_active(cid)
        self.sidebar.set_conversations(self._sidebar_items())
        self._schedule_save()

    def delete_conversation(self, cid: str):
        self._conversations.pop(cid, None)
        if self._active_id == cid:
            self.new_conversation()
        else:
            self.sidebar.set_conversations(self._sidebar_items())
        self._schedule_save()

    def _render_current(self):
        conv = self._conversations.get(self._active_id)
        if not conv:
            return
        self._clear_messages()
        self._conv_title.setText(conv.get("title", "Conversation"))
        messages = conv.get("messages", [])
        if not messages:
            self._show_welcome(True)
            return
        self._show_welcome(False)
        for msg in messages:
            self._render_message(msg["role"], msg["text"], ts=msg.get("ts"))

    def _clear_messages(self):
        """Remove every message row while keeping the welcome (index 0)
        and the trailing stretch pinned in the layout."""
        for i in range(self._messages_layout.count() - 2, 0, -1):
            item = self._messages_layout.takeAt(i)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    # ------------------------------------------------------------------ #
    # welcome
    # ------------------------------------------------------------------ #
    def _show_welcome(self, visible: bool):
        if self._messages_layout.indexOf(self._welcome) < 0:
            self._messages_layout.insertWidget(0, self._welcome)
        self._welcome.setVisible(visible)
        if visible:
            self._welcome.set_greeting()

    # ================================================================== #
    # chat content
    # ================================================================== #
    def _render_message(self, role: str, text: str, ts=None):
        ts = ts or time.time()
        if role == "user":
            row = QWidget()
            row.setAttribute(Qt.WA_TranslucentBackground)
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            bubble = UserBubble()
            bubble.set_text(text)
            lay.addStretch(1)
            lay.addWidget(bubble, 0, Qt.AlignTop)
            self._messages_layout.insertWidget(self._messages_layout.count() - 1, row)
        else:
            row = AssistantRow()
            row.set_text(text)
            row.set_name(self._assistant_name)
            row.set_time(ts)
            self._messages_layout.insertWidget(self._messages_layout.count() - 1, row)
        fade_in(row)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        bar = self._messages_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def append_user(self, text: str):
        self._finish_typing()
        conv = self._conversations.setdefault(self._active_id, {
            "title": "New conversation", "messages": [], "updated": time.time(),
        })
        conv["messages"].append({"role": "user", "text": text, "ts": time.time()})
        if conv["title"] == "New conversation":
            conv["title"] = text[:42] + ("…" if len(text) > 42 else "")
            self._conv_title.setText(conv["title"])
        conv["updated"] = time.time()
        self._show_welcome(False)
        self._render_message("user", text)
        self.sidebar.set_conversations(self._sidebar_items())
        self.sidebar.set_active(self._active_id)
        self._schedule_save()

    def begin_reply(self):
        """Show the typing indicator (called when the agent starts thinking)."""
        if self._typing_row is not None:
            return
        self._typing_row = _TypingRow(self._messages_host)
        self._messages_layout.insertWidget(self._messages_layout.count() - 1,
                                           self._typing_row)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _finish_typing(self):
        if self._typing_row is not None:
            self._typing_row.deleteLater()
            self._typing_row = None

    def append_assistant(self, text: str):
        """Show/stream an assistant reply. Progressive chunks accumulate."""
        text = (text or "").strip()
        if not text:
            return
        conv = self._conversations.setdefault(self._active_id, {
            "title": "New conversation", "messages": [], "updated": time.time(),
        })
        self._show_welcome(False)

        if self._typing_row is not None:
            self._finish_typing()
            conv["messages"].append({"role": "assistant", "text": text,
                                     "ts": time.time()})
            self._render_message("assistant", text)
            self._reply_live = True
        elif self._reply_live and conv["messages"]:
            last = conv["messages"][-1]
            if last["role"] == "assistant":
                prev = last["text"]
                if text.startswith(prev):
                    last["text"] = text
                    self._update_last_assistant(text)
                elif text != prev:
                    last["text"] = prev + "\n\n" + text
                    self._update_last_assistant(last["text"])
                self._scroll_to_bottom()
            else:
                conv["messages"].append({"role": "assistant", "text": text,
                                         "ts": time.time()})
                self._render_message("assistant", text)
        else:
            conv["messages"].append({"role": "assistant", "text": text,
                                     "ts": time.time()})
            self._render_message("assistant", text)
            self._reply_live = True

        conv["updated"] = time.time()
        self.sidebar.set_conversations(self._sidebar_items())
        self._schedule_save()

    def _update_last_assistant(self, text: str):
        count = self._messages_layout.count()
        for i in range(count - 1):
            item = self._messages_layout.itemAt(i)
            w = item.widget()
            if isinstance(w, AssistantRow):
                if i == count - 2:
                    w.set_text(text)
                    w.updateGeometry()
                    break

    def _send_suggestion(self, text: str):
        self.input_bar.editor.setPlainText(text)
        self._on_submit(text)

    def _on_submit(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        self.append_user(text)
        self.begin_reply()
        self._reply_live = False
        self.textSubmitted.emit(text)

    # ================================================================== #
    # voice state
    # ================================================================== #
    def _on_voice_state(self, state: str):
        self._voice_state = state
        self.title_bar.set_status(STATUS_LABELS.get(state, STATUS_LABELS["idle"]),
                                  accent=state != "idle")
        self.sidebar.set_status(state)
        self.input_bar.set_voice_state(state)
        if state == "listening":
            self.show_and_raise()
            self.input_bar.focus_input()
        elif state == "thinking":
            self.begin_reply()
        elif state in ("speaking", "idle"):
            self._finish_typing()

    def set_voice_state(self, state: str):
        self._on_voice_state(state)

    # ================================================================== #
    # attachments
    # ================================================================== #
    def _on_attach_requested(self):
        self.attach_menu.popup(
            self.input_bar.mapToGlobal(
                self.input_bar.attach_btn.rect().bottomLeft()
            )
        )

    def _on_attach_action(self, kind: str):
        if kind == "file":
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Attach files", "",
                "All files (*.*);;Documents (*.pdf *.docx *.txt *.md)",
            )
            for p in paths[:5]:
                self.input_bar.add_attachment("file", Path(p).name)
        elif kind == "image":
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Attach images", "",
                "Images (*.png *.jpg *.jpeg *.webp *.gif)",
            )
            for p in paths[:5]:
                self.input_bar.add_attachment("image", Path(p).name)
        elif kind == "screenshot":
            self._capture_screenshot()
        elif kind == "context":
            self.input_bar.add_attachment("context", "Context & workspace")

    def _capture_screenshot(self):
        try:
            import mss
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])
            import PIL.Image
            img = PIL.Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            img.thumbnail((1280, 1280))
            tmp = _DATA_DIR / "nova_screenshot.png"
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            img.save(tmp)
            self.input_bar.add_attachment("screenshot", "Screen capture")
        except Exception:
            logger.exception("Screenshot capture failed")

    # ================================================================== #
    # sidebar / settings
    # ================================================================== #
    def toggle_sidebar(self):
        self._sidebar_collapsed = not self._sidebar_collapsed
        target = SIDEBAR_W_COLLAPSED if self._sidebar_collapsed else SIDEBAR_W
        group = QParallelAnimationGroup(self.sidebar)
        for prop in (b"minimumWidth", b"maximumWidth"):
            anim = QPropertyAnimation(self.sidebar, prop, group)
            anim.setDuration(240)
            anim.setStartValue(self.sidebar.width())
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            group.addAnimation(anim)
        group.start(QParallelAnimationGroup.DeletionPolicy.DeleteWhenStopped)
        self.sidebar.set_collapsed(self._sidebar_collapsed)
        self.sidebar.setFixedWidth(target)

    def open_settings(self):
        self._settings_open = True
        self.settings_panel.load(self._settings)
        self.settings_panel.show()
        self.settings_panel.raise_()
        self._layout_settings_panel()

    def close_settings(self):
        self._settings_open = False
        self.settings_panel.hide()

    def _layout_settings_panel(self):
        w = 380
        inset = 10
        self.settings_panel.setGeometry(
            self._shell.width() - w - inset, inset, w, self._shell.height() - inset * 2
        )
        self.settings_panel.raise_()

    # ================================================================== #
    # window chrome
    # ================================================================== #
    def show_and_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()
        if not self._maximized:
            self.showNormal()

    def toggle(self):
        """Ctrl+Space: show/raise if hidden, hide if visible."""
        if self.isVisible():
            self.hide()
        else:
            self.show_and_raise()
            QTimer.singleShot(0, self.input_bar.focus_input)

    def toggle_maximized(self):
        if self._maximized:
            self.showNormal()
            self._maximized = False
        else:
            self.showMaximized()
            self._maximized = True
        self.title_bar.set_maximized_state(self._maximized)

    def showEvent(self, event):
        super().showEvent(event)
        self.setWindowOpacity(0.0)
        animate_property(self, b"windowOpacity", 1.0, duration=180)
        self._layout_settings_panel()
        if not self._settings_open:
            self.settings_panel.hide()
        QTimer.singleShot(120, self.input_bar.focus_input)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._shell.setGeometry(self.rect())
        self._layout_settings_panel()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self._settings_open:
                self.close_settings()
            else:
                self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent):
        self._save_conversations()
        event.ignore()
        self.hide()

    # ================================================================== #
    # painting — window body
    # ================================================================== #
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(rect, SHELL_RADIUS, SHELL_RADIUS)

        base = QLinearGradient(rect.topLeft(), rect.bottomRight())
        base.setColorAt(0.0, QColor(BG_1))
        base.setColorAt(1.0, QColor(BG_0))
        painter.fillPath(path, base)

        # ambient accent wash — very subtle
        wash = QLinearGradient(rect.left(), rect.top(),
                               rect.width() * 0.55, rect.height())
        wash_color = QColor(ACCENT)
        wash_color.setAlpha(16)
        wash.setColorAt(0.0, wash_color)
        wash.setColorAt(0.5, QColor(0, 0, 0, 0))
        painter.fillPath(path, wash)

        pen = QPen(QColor(BORDER), 1.0)
        painter.setPen(pen)
        painter.drawPath(path)
