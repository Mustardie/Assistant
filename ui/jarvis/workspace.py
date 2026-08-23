"""Absolute-position widget workspace and Qt-safe event adapter."""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, Qt, Signal, Slot
from PySide6.QtWidgets import QWidget

from ui.jarvis.events import JarvisEvent, JarvisEventBus, JarvisEventType
from ui.jarvis.manager import WidgetManager
from ui.jarvis.models import WidgetState
from ui.jarvis.widget_contents import ChatWidget, ConfirmationWidget, TaskProgressWidget, create_widget_content
from ui.jarvis.widget_shell import WidgetShell


class _EventRelay(QObject):
    received = Signal(object)


class WidgetWorkspace(QWidget):
    TOP_SAFE_AREA = 84
    BOTTOM_SAFE_AREA = 78
    chatSubmitted = Signal(str)
    taskCancelled = Signal()
    confirmationResolved = Signal(str, bool)
    widgetAction = Signal(str, str, object)

    def __init__(self, manager: WidgetManager, event_bus: JarvisEventBus, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.events = event_bus
        self.shells: dict[str, WidgetShell] = {}
        self.snap_enabled = True
        self.hover_effects_enabled = True
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._relay = _EventRelay(self)
        self._relay.received.connect(self._handle_event)
        self._unsubscribe = event_bus.subscribe("*", self._relay.received.emit)

    def restore(self) -> None:
        restored = self.manager.restore_layout()
        for state in restored:
            state.x = max(8, state.x)
            state.y = max(self.TOP_SAFE_AREA, state.y)
            self._create_shell(state)
        if restored:
            self.manager.save_layout()

    def constrain_position(self, shell: QWidget, target: QPoint) -> QPoint:
        """Keep the complete header reachable inside the interactive area."""
        max_x = max(8, self.width() - min(shell.width(), 120))
        max_y = max(self.TOP_SAFE_AREA, self.height() - self.BOTTOM_SAFE_AREA - 42)
        result = QPoint(
            max(8, min(target.x(), max_x)),
            max(self.TOP_SAFE_AREA, min(target.y(), max_y)),
        )
        if self.snap_enabled:
            result.setX(round(result.x() / 8) * 8)
            result.setY(round(result.y() / 8) * 8)
            result.setX(max(8, min(result.x(), max_x)))
            result.setY(max(self.TOP_SAFE_AREA, min(result.y(), max_y)))
        return result

    def set_hover_effects(self, enabled: bool):
        self.hover_effects_enabled = bool(enabled)
        for shell in self.shells.values():
            shell.set_hover_effects(enabled)

    def sync_all(self) -> None:
        for state in self.manager.all():
            if state.widget_id not in self.shells:
                self._create_shell(state)
            else:
                self.shells[state.widget_id].apply_state(state)

    def _create_shell(self, state: WidgetState) -> WidgetShell:
        existing = self.shells.get(state.widget_id)
        if existing:
            existing.apply_state(state)
            return existing
        spec = self.manager.registry.get(state.widget_type)
        content = create_widget_content(spec, state)
        shell = WidgetShell(self.manager, state, content, self)
        shell.set_hover_effects(self.hover_effects_enabled)
        shell.setGeometry(state.x, state.y, state.width, state.height)
        constrained = self.constrain_position(shell, shell.pos())
        state.x, state.y = constrained.x(), constrained.y()
        shell.move(constrained)
        content.actionRequested.connect(
            lambda action, payload, widget_id=state.widget_id: self._content_action(widget_id, action, payload)
        )
        if isinstance(content, ChatWidget):
            content.messageSubmitted.connect(self.chatSubmitted)
        if isinstance(content, TaskProgressWidget):
            content.cancelled.connect(self.taskCancelled)
        if isinstance(content, ConfirmationWidget):
            content.resolved.connect(self.confirmationResolved)
        self.shells[state.widget_id] = shell
        shell.show()
        self._raise_in_order()
        return shell

    def _raise_in_order(self) -> None:
        states = self.manager.all()
        for state in [item for item in states if not item.pinned] + [item for item in states if item.pinned]:
            shell = self.shells.get(state.widget_id)
            if shell:
                shell.raise_()

    def _content_action(self, widget_id: str, action: str, payload: object) -> None:
        value = dict(payload or {})
        self.events.publish(
            JarvisEventType.WIDGET_ACTION,
            {"widget_id": widget_id, "action": action, "payload": value},
            source="widget",
        )
        self.widgetAction.emit(widget_id, action, value)

    @Slot(object)
    def _handle_event(self, event: JarvisEvent) -> None:
        if event.event_type == "widget_state_changed":
            operation = event.payload.get("operation")
            raw = event.payload.get("widget")
            if operation == "reset":
                for shell in self.shells.values():
                    shell.deleteLater()
                self.shells.clear()
            elif operation == "close" and raw:
                shell = self.shells.pop(raw.get("widget_id"), None)
                if shell:
                    shell.deleteLater()
            elif raw:
                state = self.manager.get(raw.get("widget_id"))
                if state:
                    shell = self._create_shell(state)
                    shell.apply_state(state)
                    self._raise_in_order()
            return
        if event.event_type == JarvisEventType.CHAT_MESSAGE.value:
            state = self.manager.find_type("chat")
            if state:
                shell = self.shells.get(state.widget_id)
                if shell and isinstance(shell.content, ChatWidget):
                    shell.content.add_message(event.payload.get("role", "assistant"), event.payload.get("text", ""))

    def resizeEvent(self, event):
        for shell in self.shells.values():
            state = self.manager.get(shell.state.widget_id)
            if state and state.expanded:
                shell.setGeometry(
                    16,
                    self.TOP_SAFE_AREA,
                    max(320, self.width() - 32),
                    max(220, self.height() - self.TOP_SAFE_AREA - self.BOTTOM_SAFE_AREA),
                )
                continue
            constrained = self.constrain_position(shell, shell.pos())
            if constrained != shell.pos():
                shell.move(constrained)
                state = self.manager.get(shell.state.widget_id)
                if state:
                    state.x, state.y = constrained.x(), constrained.y()
        self.manager.save_layout()
        super().resizeEvent(event)
