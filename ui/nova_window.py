"""
NovaWindow — the redesigned Nova desktop app shell.

    NavRail (256px) ──────────────────────────┐
    │ orb · name · New Thread · page tabs     │  TopBar (64px)
    │ status card · Settings                  │  links · upgrade · bell · controls
    └─────────────────────────────────────────┴────────────
                                               │  QStackedWidget:
                                               │  Chat / History / Templates /
                                               │  Library / Settings pages

UI ONLY. The window talks to the assistant through signals:

    textSubmitted(str)      — user sent a message (run your agent)
    voicePressed()          — user pressed the mic (start voice session)
    settingsChanged(dict)   — settings edited (apply live + persist)

And receives state through methods:

    append_assistant(text)  — show/stream an assistant reply
    set_voice_state(state)  — "idle"|"listening"|"thinking"|"speaking"
    begin_reply()           — show the typing indicator
    open_conversation(id)   — jump to a conversation (from History)

Conversations and settings persist to data/ as JSON. Wire it up in
main.py exactly like before:

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

from PySide6.QtCore import Qt, QRectF, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient, QPainterPath, QCloseEvent
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget, QMenu

from . import icons, theme
from .animations import animate_property
from .widgets import (
    NavRail, TopBar, ChatPage, HistoryPage, LibraryPage, TemplatesPage,
    SettingsPage, ConnectionsPage,
)

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CONVERSATIONS_FILE = _DATA_DIR / "nova_conversations.json"
_SETTINGS_FILE = _DATA_DIR / "nova_settings.json"

PAGES = ("chat", "history", "templates", "library", "connections", "settings")


class NovaWindow(QWidget):
    """The redesigned Nova desktop application window."""

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
        self.setMinimumSize(theme.WINDOW_MIN_W, theme.WINDOW_MIN_H)
        self.resize(theme.WINDOW_W, theme.WINDOW_H)
        self.setWindowTitle(theme.ASSISTANT_NAME)

        self._voice_state = "idle"
        self._maximized = False
        self._reply_live = False
        self._typing_active = False
        self._conversations = {}
        self._active_id = None

        self._load_state()
        self._assistant_name = (
            (self._settings.get("assistant_name") or "").strip() or theme.ASSISTANT_NAME
        )

        # ---- shell (the actual window body) ----
        self._shell = QWidget(self)
        self._shell.setAttribute(Qt.WA_TranslucentBackground)
        self._shell.setObjectName("shell")

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SHELL_MARGIN, theme.SHELL_MARGIN,
                                theme.SHELL_MARGIN, theme.SHELL_MARGIN)
        root.addWidget(self._shell)

        shell_layout = QVBoxLayout(self._shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        # ---- content row: nav rail + main column ----
        content = QWidget(self._shell)
        content.setAttribute(Qt.WA_TranslucentBackground)
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.nav = NavRail()
        self.nav.pageSelected.connect(self.show_page)
        self.nav.newThreadRequested.connect(self.new_conversation)
        self.nav.settingsRequested.connect(self.open_settings)
        content_layout.addWidget(self.nav)

        main_col = QWidget(content)
        main_col.setAttribute(Qt.WA_TranslucentBackground)
        main_col_layout = QVBoxLayout(main_col)
        main_col_layout.setContentsMargins(0, 0, 0, 0)
        main_col_layout.setSpacing(0)

        self.top_bar = TopBar()
        self.top_bar.settingsRequested.connect(self.open_settings)
        self.top_bar.minimizeRequested.connect(self.showMinimized)
        self.top_bar.maximizeRequested.connect(self.toggle_maximized)
        self.top_bar.closeRequested.connect(self.hide)
        main_col_layout.addWidget(self.top_bar)

        self._stack = QStackedWidget(main_col)
        self._stack.setStyleSheet(
            "QStackedWidget { background: transparent; border: none; }"
        )
        self.chat_page = ChatPage()
        self.chat_page.textSubmitted.connect(self._on_submit)
        self.chat_page.voicePressed.connect(self.voicePressed.emit)
        self.chat_page.attachRequested.connect(self._on_attach_requested)
        self.chat_page.settingsRequested.connect(self.open_settings)
        self.history_page = HistoryPage()
        self.history_page.openRequested.connect(self.open_conversation)
        self.history_page.deleteRequested.connect(self.delete_conversation)
        self.history_page.pinToggled.connect(self.toggle_pin)
        self.templates_page = TemplatesPage()
        self.library_page = LibraryPage()
        self.connections_page = ConnectionsPage()
        self.settings_page = SettingsPage()
        self.settings_page.changed.connect(self._on_settings_changed)

        self._pages = {
            "chat": self.chat_page,
            "history": self.history_page,
            "templates": self.templates_page,
            "library": self.library_page,
            "connections": self.connections_page,
            "settings": self.settings_page,
        }
        for name in PAGES:
            self._stack.addWidget(self._pages[name])
        self._current_page = "chat"
        main_col_layout.addWidget(self._stack, 1)

        content_layout.addWidget(main_col, 1)
        shell_layout.addWidget(content, 1)

        # ---- persistence debounce ----
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_conversations)

        # initial conversation
        if not self._active_id:
            self.new_conversation(first=True)
        else:
            self.open_conversation(self._active_id, switch=False)

        self._apply_settings(self._settings)
        self._refresh_history()
        self.nav.set_status("idle")

    # ================================================================== #
    # page switching
    # ================================================================== #
    def show_page(self, page: str):
        if page not in self._pages:
            return
        self._current_page = page
        self._stack.setCurrentWidget(self._pages[page])
        self.nav.set_active(page)
        if page == "chat":
            QTimer.singleShot(60, self.chat_page.focus_input)
        elif page == "history":
            self._refresh_history()
        elif page == "settings":
            self.settings_page.load(self._settings)

    # ================================================================== #
    # persistence
    # ================================================================== #
    def _load_state(self):
        self._settings = dict(SettingsPage.DEFAULTS)
        try:
            if _SETTINGS_FILE.exists():
                data = json.loads(
                    _SETTINGS_FILE.read_text(encoding="utf-8-sig"))
                self._settings.update({k: v for k, v in data.items()
                                       if k in SettingsPage.DEFAULTS})
        except Exception:
            logger.exception("Failed to load settings")
        try:
            if _CONVERSATIONS_FILE.exists():
                data = json.loads(
                    _CONVERSATIONS_FILE.read_text(encoding="utf-8-sig"))
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
        name = (settings.get("assistant_name") or "").strip() or theme.ASSISTANT_NAME
        self._assistant_name = name
        self.nav.set_name(name)
        self.chat_page.set_assistant_name(name)
        self.chat_page._welcome.set_assistant_name(name)
        self.setWindowTitle(name)

        provider = settings.get("provider", "Ollama")
        models = settings.get("models") or {}
        model = (models.get(provider) or settings.get("model") or "").strip()
        self.chat_page.set_model(provider, model)
        self.nav.set_model(provider, model)

        new_theme = theme._resolve_theme(settings.get("theme") or "dark")
        if new_theme != theme.THEME_NAME:
            theme.apply_theme(new_theme)
            self._restyle_all()
        if settings.get("always_on_top"):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.show()

    def _restyle_all(self):
        for name in PAGES:
            page = self._pages[name]
            if hasattr(page, "apply_theme"):
                try:
                    page.apply_theme()
                except Exception:
                    logger.exception("apply_theme failed on %s", name)
        self.nav.apply_theme()
        self.top_bar.apply_theme()
        self.update()

    # ================================================================== #
    # history helpers
    # ================================================================== #
    def _sidebar_items(self):
        items = []
        for cid, conv in self._conversations.items():
            if cid == "_active":
                continue
            messages = conv.get("messages") or []
            preview = ""
            if messages:
                preview = (messages[-1].get("text") or "")[:160]
            items.append({
                "id": cid,
                "title": conv.get("title", "Conversation"),
                "preview": preview,
                "updated": conv.get("updated", 0),
                "pinned": bool(conv.get("pinned", False)),
            })
        items.sort(key=lambda c: (not c["pinned"], -c["updated"]))
        return items

    def _refresh_history(self):
        self.history_page.set_items(self._sidebar_items())

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
        self.chat_page.clear()
        self.chat_page.set_title("New conversation")
        self.chat_page.show_welcome(True)
        if not first:
            self._schedule_save()
        self._on_voice_state("idle")
        self.show_page("chat")
        self._refresh_history()
        QTimer.singleShot(0, self.chat_page.focus_input)

    def open_conversation(self, cid: str, switch: bool = True):
        if cid not in self._conversations:
            return
        self._active_id = cid
        conv = self._conversations[cid]
        self.chat_page.clear()
        self.chat_page.set_title(conv.get("title", "Conversation"))
        messages = conv.get("messages", [])
        if messages:
            self.chat_page.show_welcome(False)
            for msg in messages:
                if msg["role"] == "user":
                    self.chat_page.append_user(msg["text"], ts=msg.get("ts"))
                else:
                    self.chat_page.append_assistant(msg["text"], ts=msg.get("ts"))
        else:
            self.chat_page.show_welcome(True)
        self._reply_live = False
        if switch:
            self.show_page("chat")
        self._refresh_history()

    def delete_conversation(self, cid: str):
        self._conversations.pop(cid, None)
        if self._active_id == cid:
            self.new_conversation()
        else:
            self._refresh_history()
        self._schedule_save()

    def toggle_pin(self, cid: str):
        conv = self._conversations.get(cid)
        if conv is None:
            return
        conv["pinned"] = not conv.get("pinned", False)
        self._refresh_history()
        self._schedule_save()

    # ================================================================== #
    # chat content
    # ================================================================== #
    def append_user(self, text: str):
        self._finish_typing()
        conv = self._conversations.setdefault(self._active_id, {
            "title": "New conversation", "messages": [], "updated": time.time(),
        })
        conv["messages"].append({"role": "user", "text": text, "ts": time.time()})
        if conv["title"] == "New conversation":
            conv["title"] = text[:42] + ("…" if len(text) > 42 else "")
            self.chat_page.set_title(conv["title"])
        conv["updated"] = time.time()
        self.chat_page.show_welcome(False)
        self.chat_page.append_user(text)
        self._refresh_history()
        self._schedule_save()

    def begin_reply(self):
        """Show the typing indicator (called when the agent starts thinking)."""
        if self._typing_active:
            return
        self.chat_page.begin_reply()
        self._typing_active = True

    def _finish_typing(self):
        if self._typing_active:
            self.chat_page._finish_typing()
            self._typing_active = False

    def append_assistant(self, text: str):
        """Show/stream an assistant reply. Progressive chunks accumulate."""
        text = (text or "").strip()
        if not text:
            return
        conv = self._conversations.setdefault(self._active_id, {
            "title": "New conversation", "messages": [], "updated": time.time(),
        })
        self.chat_page.show_welcome(False)

        if self._typing_active:
            self._finish_typing()
            conv["messages"].append({"role": "assistant", "text": text,
                                     "ts": time.time()})
            self.chat_page.append_assistant(text)
            self._reply_live = True
        elif self._reply_live and conv["messages"]:
            last = conv["messages"][-1]
            if last["role"] == "assistant":
                prev = last["text"]
                if text.startswith(prev):
                    last["text"] = text
                elif text != prev:
                    last["text"] = prev + "\n\n" + text
                self.chat_page.append_assistant(last["text"])
            else:
                conv["messages"].append({"role": "assistant", "text": text,
                                         "ts": time.time()})
                self.chat_page.append_assistant(text)
        else:
            conv["messages"].append({"role": "assistant", "text": text,
                                     "ts": time.time()})
            self.chat_page.append_assistant(text)
            self._reply_live = True

        conv["updated"] = time.time()
        self._refresh_history()
        self._schedule_save()

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
        self.nav.set_status(state)
        self.chat_page.set_voice_state(state)
        if state == "listening":
            self.show_and_raise()
            self.chat_page.focus_input()
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
        menu = QMenu(self)
        menu.setStyleSheet(theme.menu_qss())
        actions = [
            ("file", "file", "Attach files…"),
            ("image", "image", "Attach images…"),
            ("screenshot", "monitor", "Screen capture"),
            ("context", "sparkle", "Context & workspace"),
        ]
        for kind, icon_name, text in actions:
            act = menu.addAction(icons.icon(icon_name, theme.TEXT_SOFT, 15), text)
            act.triggered.connect(lambda _=False, k=kind: self._on_attach_action(k))
        menu.addSeparator()
        self.chat_page.input_bar.show_attach_menu(menu)

    def _on_attach_action(self, kind: str):
        from PySide6.QtWidgets import QFileDialog
        dock = self.chat_page.input_bar
        if kind == "file":
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Attach files", "",
                "All files (*.*);;Documents (*.pdf *.docx *.txt *.md)",
            )
            for p in paths[:5]:
                dock.add_attachment("file", Path(p).name)
        elif kind == "image":
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Attach images", "",
                "Images (*.png *.jpg *.jpeg *.webp *.gif)",
            )
            for p in paths[:5]:
                dock.add_attachment("image", Path(p).name)
        elif kind == "screenshot":
            self._capture_screenshot()
        elif kind == "context":
            dock.add_attachment("context", "Context & workspace")

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
            self.chat_page.input_bar.add_attachment("screenshot", "Screen capture")
        except Exception:
            logger.exception("Screenshot capture failed")

    # ================================================================== #
    # settings page
    # ================================================================== #
    def open_settings(self):
        self.show_page("settings")

    def close_settings(self):
        if self._current_page == "settings":
            self.show_page("chat")

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
            QTimer.singleShot(0, self.chat_page.focus_input)

    def toggle_maximized(self):
        if self._maximized:
            self.showNormal()
            self._maximized = False
        else:
            self.showMaximized()
            self._maximized = True

    def showEvent(self, event):
        super().showEvent(event)
        self.setWindowOpacity(0.0)
        animate_property(self, b"windowOpacity", 1.0, duration=180)
        QTimer.singleShot(120, self.chat_page.focus_input)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._shell.setGeometry(self.rect())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self._current_page == "settings":
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
        path.addRoundedRect(rect, theme.SHELL_RADIUS, theme.SHELL_RADIUS)

        base = QLinearGradient(rect.topLeft(), rect.bottomRight())
        base.setColorAt(0.0, QColor(theme.BG_1))
        base.setColorAt(1.0, QColor(theme.BG_0))
        painter.fillPath(path, base)

        # ambient accent wash — very subtle
        wash = QLinearGradient(rect.left(), rect.top(),
                               rect.width() * 0.55, rect.height())
        wash_color = QColor(theme.ACCENT)
        wash_color.setAlpha(16 if theme.THEME_NAME == "dark" else 8)
        wash.setColorAt(0.0, wash_color)
        wash.setColorAt(0.5, QColor(0, 0, 0, 0))
        painter.fillPath(path, wash)

        pen = QPen(QColor(theme.BORDER), 1.0)
        painter.setPen(pen)
        painter.drawPath(path)
