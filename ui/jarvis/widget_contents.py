"""Content views for implemented JARVIS widgets.

The workspace shell owns movement, resizing and persistence.  These classes
only render a widget's data and emit semantic actions back to JARVIS.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ui.jarvis.models import WidgetSpec, WidgetState
from ui.jarvis.styles import CYAN, ERROR, SUCCESS, TEXT, TEXT_FAINT, TEXT_SOFT, WARNING, button_style


def _label(text: str = "", *, soft: bool = False, wrap: bool = True) -> QLabel:
    item = QLabel(text)
    item.setWordWrap(wrap)
    item.setStyleSheet(f"color: {TEXT_SOFT if soft else TEXT}; background: transparent;")
    return item


class WidgetContent(QWidget):
    actionRequested = Signal(str, object)

    def __init__(self, spec: WidgetSpec, state: WidgetState, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.state = state
        self.setStyleSheet("background: transparent;")

    def request(self, action: str, payload: dict[str, Any] | None = None) -> None:
        self.actionRequested.emit(action, dict(payload or {}))

    def apply_state(self, state: WidgetState) -> None:
        self.state = state


class ChatWidget(WidgetContent):
    messageSubmitted = Signal(str)
    interrupted = Signal()

    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(9)
        self.history = QTextBrowser()
        self.history.setOpenExternalLinks(True)
        self.history.setStyleSheet(
            f"QTextBrowser {{ border: 0; background: rgba(2, 10, 16, 120); color: {TEXT}; padding: 9px; }}"
        )
        layout.addWidget(self.history, 1)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type here only when voice is inconvenient…")
        self.input.setStyleSheet(
            f"QLineEdit {{ color:{TEXT}; background:rgba(255,255,255,10); border:1px solid rgba(103,228,238,50); border-radius:8px; padding:9px; }}"
        )
        self.send = QPushButton("SEND")
        self.send.setStyleSheet(button_style(accent=True))
        self.stop = QPushButton("STOP")
        self.stop.setStyleSheet(button_style(danger=True))
        row.addWidget(self.input, 1)
        row.addWidget(self.send)
        row.addWidget(self.stop)
        layout.addLayout(row)
        self.send.clicked.connect(self._submit)
        self.input.returnPressed.connect(self._submit)
        self.stop.clicked.connect(self._interrupt)
        for message in state.data.get("messages", []):
            self.add_message(message.get("role", "assistant"), message.get("text", ""))

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.add_message("user", text)
        self.messageSubmitted.emit(text)

    def _interrupt(self) -> None:
        self.interrupted.emit()
        self.request("interrupt")

    def add_message(self, role: str, text: str) -> None:
        names = {"user": "YOU", "assistant": "JARVIS", "tool": "TOOL", "system": "SYSTEM"}
        colors = {"user": CYAN, "assistant": TEXT, "tool": WARNING, "system": TEXT_FAINT}
        safe = html.escape(str(text)).replace("\n", "<br>")
        self.history.append(
            f'<p style="margin:5px 0"><b style="color:{colors.get(role, TEXT_SOFT)}">'
            f'{names.get(role, role.upper())}</b><br>{safe}</p>'
        )


class TaskProgressWidget(WidgetContent):
    cancelled = Signal()

    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        self.goal = _label()
        self.status = _label(soft=True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setStyleSheet(
            f"QProgressBar {{ color:{TEXT_SOFT}; background:rgba(255,255,255,10); border:0; height:9px; }}"
            f"QProgressBar::chunk {{ background:{CYAN}; }}"
        )
        self.steps = QListWidget()
        self.steps.setStyleSheet(f"QListWidget {{ border:0; background:transparent; color:{TEXT_SOFT}; }}")
        self.cancel = QPushButton("STOP TASK")
        self.cancel.setStyleSheet(button_style(danger=True))
        self.cancel.clicked.connect(self._cancel)
        layout.addWidget(self.goal)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        layout.addWidget(self.steps, 1)
        layout.addWidget(self.cancel, 0, Qt.AlignRight)
        self.apply_state(state)

    def _cancel(self):
        self.cancelled.emit()

    def apply_state(self, state):
        super().apply_state(state)
        data = state.data
        self.goal.setText(data.get("goal") or "No active mission")
        status = str(data.get("status") or "standing by").replace("_", " ").upper()
        retry = data.get("retry_status")
        self.status.setText(f"{status}{' · ' + str(retry) if retry else ''}")
        steps = data.get("steps") or []
        complete = len([s for s in steps if isinstance(s, dict) and s.get("status") == "complete"])
        failed = len([s for s in steps if isinstance(s, dict) and s.get("status") == "failed"])
        self.progress.setValue(int(data.get("progress", (complete / len(steps) * 100) if steps else 0)))
        self.steps.clear()
        for item in steps:
            if isinstance(item, str):
                self.steps.addItem(f"○  {item}")
            else:
                mark = {"complete": "DONE", "failed": "FAIL", "active": "NOW"}.get(item.get("status"), "NEXT")
                self.steps.addItem(f"[{mark}]  {item.get('label') or item.get('step') or 'Step'}")
        for call in data.get("tool_calls") or []:
            self.steps.addItem(f"[TOOL]  {call.get('tool', 'tool')} · {call.get('status', 'unknown')}")
        if not steps and not data.get("tool_calls"):
            self.steps.addItem("Waiting for a task plan")
        self.cancel.setEnabled(status not in {"COMPLETE", "IDLE", "STANDING BY"})


class ConfirmationWidget(WidgetContent):
    resolved = Signal(str, bool)

    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 14)
        self.action = _label()
        self.risk = _label(soft=True)
        self.target = _label(soft=True)
        row = QHBoxLayout()
        self.deny = QPushButton("DENY")
        self.approve = QPushButton("APPROVE")
        self.deny.setStyleSheet(button_style(danger=True))
        self.approve.setStyleSheet(button_style(accent=True))
        row.addStretch(1)
        row.addWidget(self.deny)
        row.addWidget(self.approve)
        layout.addWidget(_label("SENSITIVE ACTION", soft=True))
        layout.addWidget(self.action)
        layout.addWidget(self.risk)
        layout.addWidget(self.target)
        layout.addStretch(1)
        layout.addLayout(row)
        self.deny.clicked.connect(lambda: self._resolve(False))
        self.approve.clicked.connect(lambda: self._resolve(True))
        self.apply_state(state)

    def apply_state(self, state):
        super().apply_state(state)
        data = state.data
        self.action.setText(data.get("action") or "Action was not specified")
        self.risk.setText(f"RISK · {data.get('risk') or 'Changes external state'}")
        self.target.setText(f"TARGET · {data.get('target') or 'Not specified'}")
        resolved = bool(data.get("resolved"))
        self.approve.setEnabled(not resolved)
        self.deny.setEnabled(not resolved)

    def _resolve(self, approved: bool):
        confirmation_id = str(self.state.data.get("confirmation_id") or self.state.widget_id)
        self.approve.setEnabled(False)
        self.deny.setEnabled(False)
        self.resolved.emit(confirmation_id, approved)


class SystemStatusWidget(WidgetContent):
    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        self.form = QFormLayout(self)
        self.form.setContentsMargins(14, 10, 14, 14)
        self.rows = {}
        for name in ("MODEL", "MIC", "SPEECH TO TEXT", "VOICE", "CONNECTORS", "ACTIVE TASK"):
            value = _label(soft=True, wrap=False)
            self.rows[name] = value
            heading = _label(name, soft=True, wrap=False)
            self.form.addRow(heading, value)
        self.warning = _label(soft=True)
        self.form.addRow(self.warning)
        self.refresh = QPushButton("REFRESH SYSTEMS")
        self.refresh.setStyleSheet(button_style(accent=True))
        self.refresh.clicked.connect(lambda: self.request("refresh"))
        self.form.addRow(self.refresh)
        self.apply_state(state)

    def apply_state(self, state):
        super().apply_state(state)
        data = state.data
        defaults = {
            "MODEL": "Not connected",
            "MIC": "Checking…",
            "SPEECH TO TEXT": "Not connected",
            "VOICE": "Not connected",
            "CONNECTORS": "No live status",
            "ACTIVE TASK": "Idle",
        }
        keys = {"MODEL": "model", "MIC": "mic", "SPEECH TO TEXT": "stt", "VOICE": "tts", "CONNECTORS": "connectors", "ACTIVE TASK": "task"}
        for label, key in keys.items():
            self.rows[label].setText(str(data.get(key, defaults[label])))
        self.warning.setText(str(data.get("warning") or "Unavailable services are never reported as connected."))


class WeatherWidget(WidgetContent):
    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 14)
        location_row = QHBoxLayout()
        self.location_input = QLineEdit()
        self.location_input.setPlaceholderText("City or region")
        self.refresh = QPushButton("REFRESH")
        self.refresh.setStyleSheet(button_style(accent=True))
        self.refresh.clicked.connect(self._refresh)
        self.location_input.returnPressed.connect(self._refresh)
        location_row.addWidget(self.location_input, 1)
        location_row.addWidget(self.refresh)
        self.location = _label()
        self.temperature = QLabel("—")
        self.temperature.setStyleSheet(f"font-size:32px; font-weight:300; color:{CYAN}; background:transparent;")
        self.summary = _label(soft=True)
        self.forecast = _label(soft=True)
        self.notice = _label(soft=True)
        layout.addLayout(location_row)
        layout.addWidget(self.location)
        layout.addWidget(self.temperature)
        layout.addWidget(self.summary)
        layout.addWidget(self.forecast)
        layout.addStretch(1)
        layout.addWidget(self.notice)
        self.apply_state(state)

    def apply_state(self, state):
        super().apply_state(state)
        data = state.data
        connected = bool(data.get("connected"))
        if data.get("location") and not self.location_input.text():
            self.location_input.setText(str(data.get("location")))
        self.location.setText(data.get("location") or "Location not selected")
        self.temperature.setText(f"{data.get('temperature', '—')}{data.get('unit', '°')}" if connected else "—")
        self.summary.setText(data.get("summary") or ("Live conditions unavailable" if not connected else "No condition summary"))
        forecast = data.get("forecast") or []
        self.forecast.setText("  ·  ".join(map(str, forecast)) if forecast else "No forecast data")
        if state.loading:
            self.notice.setText("CONTACTING LIVE WEATHER SERVICE…")
        elif state.error:
            self.notice.setText(f"WEATHER ERROR · {state.error}")
        elif connected:
            self.notice.setText(str(data.get("warning") or "Live provider connected"))
        else:
            self.notice.setText("ENTER A LOCATION · live keyless weather is ready on demand")

    def _refresh(self):
        self.request("refresh", {"location": self.location_input.text().strip()})


class VideoPlayerWidget(WidgetContent):
    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 10)
        self.player = None
        self.video = None
        self.message = _label("No local video selected", soft=True)
        self.message.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.message, 1)
        controls = QHBoxLayout()
        self.open = QPushButton("OPEN")
        self.play = QPushButton("PLAY")
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(70)
        self.open.setStyleSheet(button_style())
        self.play.setStyleSheet(button_style(accent=True))
        controls.addWidget(self.open)
        controls.addWidget(self.play)
        controls.addWidget(self.seek, 1)
        controls.addWidget(_label("VOL", soft=True, wrap=False))
        controls.addWidget(self.volume)
        layout.addLayout(controls)
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget

            self.video = QVideoWidget()
            layout.insertWidget(0, self.video, 1)
            self.message.hide()
            self.audio = QAudioOutput(self)
            self.audio.setVolume(0.7)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.positionChanged.connect(self.seek.setValue)
            self.player.durationChanged.connect(lambda value: self.seek.setRange(0, value))
            self.seek.sliderMoved.connect(self.player.setPosition)
            self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
            self.play.clicked.connect(self._toggle)
        except Exception as exc:
            self.message.setText(f"Media backend unavailable\n{exc}")
            self.play.setEnabled(False)
        self.apply_state(state)
        self.open.clicked.connect(lambda: self.request("open"))

    def apply_state(self, state):
        super().apply_state(state)
        path = state.data.get("path")
        if self.player and path and Path(path).is_file():
            self.player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
            self.message.hide()
        elif path:
            self.message.setText(f"Video file not found\n{path}")
            self.message.show()
            self.play.setEnabled(False)
        else:
            self.message.setText("No local video selected\nProvide a path or use an available file picker backend.")
            self.message.show()
            self.play.setEnabled(False)

    def _toggle(self):
        if not self.player:
            return
        from PySide6.QtMultimedia import QMediaPlayer

        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.play.setText("PLAY")
            self.request("pause")
        else:
            self.player.play()
            self.play.setText("PAUSE")
            self.request("play", {"path": self.state.data.get("path")})


class FileSearchWidget(WidgetContent):
    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search local files…")
        self.search = QPushButton("SEARCH")
        self.search.setStyleSheet(button_style(accent=True))
        row.addWidget(self.query, 1)
        row.addWidget(self.search)
        self.results = QListWidget()
        self.results.setStyleSheet(f"QListWidget {{ color:{TEXT_SOFT}; background:rgba(0,0,0,30); border:0; }}")
        self.notice = _label(soft=True)
        layout.addLayout(row)
        layout.addWidget(self.results, 1)
        layout.addWidget(self.notice)
        result_actions = QHBoxLayout()
        result_actions.addStretch(1)
        self.open_result = QPushButton("OPEN")
        self.reveal_result = QPushButton("SHOW IN FOLDER")
        self.open_result.setStyleSheet(button_style(accent=True))
        self.reveal_result.setStyleSheet(button_style())
        self.open_result.clicked.connect(lambda: self._selected_action("open"))
        self.reveal_result.clicked.connect(lambda: self._selected_action("reveal"))
        result_actions.addWidget(self.reveal_result)
        result_actions.addWidget(self.open_result)
        layout.addLayout(result_actions)
        self.search.clicked.connect(self._search)
        self.query.returnPressed.connect(self._search)
        self.results.itemDoubleClicked.connect(lambda item: self.request("open", {"path": item.data(Qt.UserRole)}))
        self.apply_state(state)

    def _search(self):
        self.request("search", {"query": self.query.text().strip()})

    def _selected_action(self, action: str):
        item = self.results.currentItem()
        if item:
            self.request(action, {"path": item.data(Qt.UserRole)})

    def apply_state(self, state):
        super().apply_state(state)
        data = state.data
        if data.get("query") and not self.query.text():
            self.query.setText(str(data["query"]))
        self.results.clear()
        for result in data.get("results") or []:
            if isinstance(result, str):
                path = result
                title = result
                tooltip = result
            else:
                path = result.get("path", "")
                summary = result.get("summary") or (result.get("profile") or {}).get("summary", {}).get("text") or "Purpose unknown"
                confidence = result.get("confidence")
                risk = result.get("risk") or (result.get("profile") or {}).get("risk")
                badges = []
                if risk:
                    badges.append(str(risk).upper())
                if isinstance(confidence, (int, float)):
                    badges.append(f"{confidence:.0%}")
                badge_text = f"  [{' · '.join(badges)}]" if badges else ""
                title = f"{Path(path).name or path}{badge_text}\n{summary}"
                evidence = result.get("evidence") or (result.get("profile") or {}).get("evidence") or []
                tooltip = f"{path}\n\n{summary}\n\nEvidence:\n" + "\n".join(str(item) for item in evidence[:8])
            self.results.addItem(title)
            item = self.results.item(self.results.count() - 1)
            item.setData(Qt.UserRole, path)
            item.setToolTip(tooltip)
        if state.loading:
            self.notice.setText("Searching…")
        elif state.error:
            self.notice.setText(f"Search failed · {state.error}")
        elif not data.get("results"):
            self.notice.setText("No results yet · local search runs only when its backend handles this request.")
        else:
            self.notice.setText(f"{len(data['results'])} result(s)")


class MemoryRecallWidget(WidgetContent):
    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        self.status = _label(soft=True)
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search memory…")
        self.recall = QPushButton("RECALL")
        self.recall.setStyleSheet(button_style(accent=True))
        self.recall.clicked.connect(lambda: self.request("recall", {"query": self.query.text().strip()}))
        self.query.returnPressed.connect(self.recall.click)
        row.addWidget(self.query, 1)
        row.addWidget(self.recall)
        self.memories = QListWidget()
        self.memories.setStyleSheet(f"QListWidget {{ color:{TEXT_SOFT}; background:transparent; border:0; }}")
        layout.addWidget(self.status)
        layout.addLayout(row)
        layout.addWidget(self.memories, 1)
        self.apply_state(state)

    def apply_state(self, state):
        super().apply_state(state)
        data = state.data
        memories = data.get("memories") or []
        self.status.setText("MEMORY USED FOR THIS TASK" if data.get("used") else "NO MEMORY USED · retrieval is targeted, not automatic")
        self.memories.clear()
        for memory in memories:
            if isinstance(memory, str):
                self.memories.addItem(memory)
            else:
                text = memory.get("summary") or memory.get("text") or "Memory item"
                evidence = memory.get("matched_terms") or memory.get("confidence") or "source available"
                self.memories.addItem(f"{text}  ·  {evidence}")
        if not memories:
            self.memories.addItem("No relevant memories retrieved")


_LIST_WIDGETS = {
    "calendar": {"placeholder": "Add an event or search schedule…", "primary": ("REFRESH", "refresh"), "secondary": ("ADD EVENT", "create"), "keys": ("events",), "empty": "No local or connected calendar events."},
    "reminders": {"placeholder": "New reminder…", "primary": ("ADD", "create"), "secondary": ("COMPLETE", "complete"), "keys": ("items", "reminders"), "empty": "No reminders. Add one above."},
    "web_results": {"placeholder": "Search the web…", "primary": ("SEARCH", "search"), "secondary": ("OPEN", "open"), "keys": ("results",), "empty": "Run a search to collect cited results."},
    "app_launcher": {"placeholder": "Find an installed app…", "primary": ("SEARCH", "search"), "secondary": ("LAUNCH", "launch"), "keys": ("apps", "results"), "empty": "Refresh or search the local application index."},
    "transfers": {"placeholder": None, "primary": ("REFRESH", "refresh"), "secondary": ("CANCEL", "cancel"), "keys": ("transfers", "items"), "empty": "No active transfers."},
    "notifications": {"placeholder": None, "primary": ("REFRESH", "refresh"), "secondary": ("DISMISS", "dismiss"), "keys": ("notifications", "items"), "empty": "No unread JARVIS notifications."},
    "email": {"placeholder": "Search Gmail…", "primary": ("SEARCH", "search"), "secondary": ("OPEN", "open"), "keys": ("emails", "results"), "empty": "Connect Gmail or search the inbox."},
    "connectors": {"placeholder": None, "primary": ("REFRESH", "refresh"), "secondary": ("CONNECT", "connect"), "keys": ("connectors", "items"), "empty": "No connectors discovered."},
    "tool_inspector": {"placeholder": None, "primary": ("REFRESH", "refresh"), "secondary": ("RETRY", "retry"), "keys": ("tool_calls", "items"), "empty": "No tool calls in the current session."},
    "plan_inspector": {"placeholder": None, "primary": ("REFRESH", "refresh"), "secondary": ("CANCEL", "cancel"), "keys": ("steps", "items"), "empty": "No active plan."},
    "automation": {"placeholder": "Filter automations…", "primary": ("REFRESH", "refresh"), "secondary": ("RUN", "run"), "keys": ("automations", "items"), "empty": "No automation watchers are registered."},
    "skills": {"placeholder": "Find a saved skill…", "primary": ("REFRESH", "refresh"), "secondary": ("RUN", "run"), "keys": ("skills", "items"), "empty": "No saved skills were found."},
    "command_palette": {"placeholder": "Ask, open, search, or run…", "primary": ("RUN", "run"), "secondary": ("CLEAR", "clear"), "keys": ("suggestions", "items"), "empty": "Type a natural-language command."},
    "system_monitor": {"placeholder": None, "primary": ("REFRESH", "refresh"), "secondary": None, "keys": ("metrics", "items"), "empty": "Collecting local system metrics…"},
    "code_task": {"placeholder": "Describe a development task…", "primary": ("RUN TESTS", "run_tests"), "secondary": ("OPEN FILE", "open_file"), "keys": ("files", "tests", "items"), "empty": "No active development task."},
    "activity": {"placeholder": "Filter activity…", "primary": ("REFRESH", "refresh"), "secondary": ("CLEAR", "clear"), "keys": ("activity", "items"), "empty": "No JARVIS activity recorded in this session."},
}


def _item_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)
    primary = item.get("title") or item.get("name") or item.get("summary") or item.get("subject") or item.get("path") or item.get("tool") or item.get("label") or item.get("step") or item.get("action") or item.get("connector") or "Item"
    secondary = item.get("status") or item.get("time") or item.get("sender") or item.get("detail") or item.get("description") or item.get("value") or ""
    return f"{primary}  ·  {secondary}" if secondary else str(primary)


class ListActionWidget(WidgetContent):
    """Purpose-configured search/list/action surface for live collections."""

    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        self.config = _LIST_WIDGETS[state.widget_type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        self.context = _label(spec.purpose, soft=True)
        layout.addWidget(self.context)
        placeholder = self.config.get("placeholder")
        self.query = None
        if placeholder:
            self.query = QLineEdit()
            self.query.setPlaceholderText(placeholder)
            self.query.returnPressed.connect(self._primary)
            layout.addWidget(self.query)
        self.items = QListWidget()
        self.items.setStyleSheet(f"QListWidget {{ color:{TEXT_SOFT}; background:rgba(0,0,0,35); border:1px solid rgba(103,228,238,25); border-radius:7px; padding:4px; }} QListWidget::item:selected {{ color:{TEXT}; background:rgba(103,228,238,35); }}")
        self.items.itemDoubleClicked.connect(lambda _item: self._secondary())
        layout.addWidget(self.items, 1)
        self.notice = _label(soft=True)
        layout.addWidget(self.notice)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.primary = QPushButton(self.config["primary"][0])
        self.primary.setStyleSheet(button_style(accent=True))
        self.primary.clicked.connect(self._primary)
        actions.addWidget(self.primary)
        self.secondary = None
        if self.config.get("secondary"):
            self.secondary = QPushButton(self.config["secondary"][0])
            self.secondary.setStyleSheet(button_style())
            self.secondary.clicked.connect(self._secondary)
            actions.addWidget(self.secondary)
        layout.addLayout(actions)
        self.apply_state(state)

    def _payload(self) -> dict:
        selected = self.items.currentItem()
        return {
            "query": self.query.text().strip() if self.query else "",
            "text": self.query.text().strip() if self.query else "",
            "selected": selected.data(Qt.UserRole) if selected else None,
        }

    def _primary(self):
        self.request(self.config["primary"][1], self._payload())

    def _secondary(self):
        if self.config.get("secondary"):
            self.request(self.config["secondary"][1], self._payload())

    def apply_state(self, state):
        super().apply_state(state)
        data = state.data
        values = []
        for key in self.config["keys"]:
            candidate = data.get(key)
            if isinstance(candidate, list):
                values.extend(candidate)
        self.items.clear()
        for value in values:
            self.items.addItem(_item_text(value))
            self.items.item(self.items.count() - 1).setData(Qt.UserRole, value)
        if state.loading:
            self.notice.setText("WORKING · backend request in progress…")
        elif state.error:
            self.notice.setText(f"ERROR · {state.error}")
        elif values:
            self.notice.setText(f"{len(values)} live item(s)")
        else:
            self.notice.setText(self.config["empty"])
        if self.query and data.get("query") and not self.query.text():
            self.query.setText(str(data["query"]))


_TEXT_WIDGETS = {
    "notes": {"placeholder": "Write a note, scratchpad, or task summary…", "primary": ("SAVE", "save"), "secondary": ("COPY", "copy"), "field": "text"},
    "clipboard": {"placeholder": "Clipboard content appears here…", "primary": ("REFRESH", "refresh"), "secondary": ("SUMMARIZE", "summarize"), "field": "text"},
    "messaging": {"placeholder": "Draft a message. Sending always requires confirmation…", "primary": ("DRAFT", "draft"), "secondary": ("REQUEST SEND", "send"), "field": "draft"},
    "terminal": {"placeholder": "Enter a command. Execution always requires confirmation…", "primary": ("REQUEST RUN", "run"), "secondary": ("COPY OUTPUT", "copy"), "field": "command"},
    "media_review": {"placeholder": "Add review notes or a local media path…", "primary": ("OPEN MEDIA", "open"), "secondary": ("SAVE FEEDBACK", "feedback"), "field": "feedback"},
    "study": {"placeholder": "Paste a question or topic to study…", "primary": ("EXPLAIN", "explain"), "secondary": ("QUIZ ME", "quiz"), "field": "text"},
    "quick_answer": {"placeholder": "Ask for a calculation, fact, definition, or conversion…", "primary": ("ANSWER", "ask"), "secondary": ("COPY", "copy"), "field": "answer"},
    "error_debug": {"placeholder": "Failure details and recovery notes appear here…", "primary": ("RETRY", "retry"), "secondary": ("COPY REPORT", "copy_report"), "field": "report"},
}


class TextActionWidget(WidgetContent):
    """Purpose-configured editor with real actions and state feedback."""

    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        self.config = _TEXT_WIDGETS[state.widget_type]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.addWidget(_label(spec.purpose, soft=True))
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(self.config["placeholder"])
        self.editor.setStyleSheet(f"QPlainTextEdit {{ color:{TEXT}; background:rgba(0,0,0,42); border:1px solid rgba(103,228,238,35); border-radius:8px; padding:8px; }}")
        layout.addWidget(self.editor, 1)
        self.notice = _label(soft=True)
        layout.addWidget(self.notice)
        row = QHBoxLayout()
        row.addStretch(1)
        self.primary = QPushButton(self.config["primary"][0])
        self.secondary = QPushButton(self.config["secondary"][0])
        self.primary.setStyleSheet(button_style(accent=True))
        self.secondary.setStyleSheet(button_style())
        self.primary.clicked.connect(lambda: self.request(self.config["primary"][1], {"text": self.editor.toPlainText().strip()}))
        self.secondary.clicked.connect(lambda: self.request(self.config["secondary"][1], {"text": self.editor.toPlainText().strip()}))
        row.addWidget(self.secondary)
        row.addWidget(self.primary)
        layout.addLayout(row)
        self.apply_state(state)

    def apply_state(self, state):
        super().apply_state(state)
        data = state.data
        field = self.config["field"]
        value = data.get(field)
        if not value and state.widget_type == "error_debug":
            value = data.get("error") or data.get("message")
        if value is not None and self.editor.toPlainText() != str(value):
            self.editor.setPlainText(str(value))
        self.notice.setText(
            "WORKING · request in progress…" if state.loading else
            f"ERROR · {state.error}" if state.error else
            str(data.get("status") or data.get("notice") or "READY")
        )


class AudioPlayerWidget(WidgetContent):
    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        self.track = _label("No local audio selected")
        self.position = QSlider(Qt.Horizontal)
        self.position.setRange(0, 0)
        row = QHBoxLayout()
        self.open = QPushButton("OPEN")
        self.play = QPushButton("PLAY")
        self.open.setStyleSheet(button_style())
        self.play.setStyleSheet(button_style(accent=True))
        row.addWidget(self.open)
        row.addWidget(self.play)
        row.addWidget(self.position, 1)
        layout.addWidget(self.track)
        layout.addStretch(1)
        layout.addLayout(row)
        self.player = None
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            self.audio = QAudioOutput(self)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self.player.positionChanged.connect(self.position.setValue)
            self.player.durationChanged.connect(lambda value: self.position.setRange(0, value))
            self.position.sliderMoved.connect(self.player.setPosition)
        except Exception as exc:
            self.track.setText(f"Audio runtime unavailable · {exc}")
        self.open.clicked.connect(lambda: self.request("open"))
        self.play.clicked.connect(self._toggle)
        self.apply_state(state)

    def apply_state(self, state):
        super().apply_state(state)
        path = state.data.get("path")
        self.track.setText(Path(path).name if path else "No local audio selected")
        self.play.setEnabled(bool(self.player and path and Path(path).is_file()))
        if self.player and path and Path(path).is_file():
            self.player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))

    def _toggle(self):
        if not self.player:
            return
        from PySide6.QtMultimedia import QMediaPlayer
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        self.player.pause() if playing else self.player.play()
        self.play.setText("PLAY" if playing else "PAUSE")
        self.request("pause" if playing else "play", {"path": self.state.data.get("path")})


class VoiceTranscriptWidget(WidgetContent):
    def __init__(self, spec, state, parent=None):
        super().__init__(spec, state, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        self.confidence = _label("Waiting for speech…", soft=True)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Live transcription appears here and can be corrected…")
        copy = QPushButton("COPY / USE CORRECTION")
        copy.setStyleSheet(button_style(accent=True))
        copy.clicked.connect(lambda: self.request("correct", {"text": self.editor.toPlainText().strip()}))
        layout.addWidget(self.confidence)
        layout.addWidget(self.editor, 1)
        layout.addWidget(copy, 0, Qt.AlignRight)
        self.apply_state(state)

    def apply_state(self, state):
        super().apply_state(state)
        text = state.data.get("text") or state.data.get("transcript") or ""
        if text and self.editor.toPlainText() != str(text):
            self.editor.setPlainText(str(text))
        confidence = state.data.get("confidence")
        self.confidence.setText(f"CONFIDENCE · {float(confidence):.0%}" if isinstance(confidence, (int, float)) else "LIVE VOICE TRANSCRIPT")


_CONTENT = {
    "chat": ChatWidget,
    "task_progress": TaskProgressWidget,
    "confirmation": ConfirmationWidget,
    "system_status": SystemStatusWidget,
    "weather": WeatherWidget,
    "video_player": VideoPlayerWidget,
    "file_search": FileSearchWidget,
    "memory_recall": MemoryRecallWidget,
    "audio_player": AudioPlayerWidget,
    "voice_transcript": VoiceTranscriptWidget,
}

for _widget_type in _LIST_WIDGETS:
    _CONTENT[_widget_type] = ListActionWidget
for _widget_type in _TEXT_WIDGETS:
    _CONTENT[_widget_type] = TextActionWidget


def create_widget_content(spec: WidgetSpec, state: WidgetState, parent=None) -> WidgetContent:
    content = _CONTENT.get(state.widget_type)
    if content is None:
        raise KeyError(f"Widget '{state.widget_type}' has no content implementation")
    return content(spec, state, parent)
