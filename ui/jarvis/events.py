"""Small event bus shared by the brain bridge and widget workspace."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any, Callable


class JarvisEventType(str, Enum):
    JARVIS_STATE_CHANGED = "jarvis_state_changed"
    USER_VOICE_STARTED = "user_voice_started"
    USER_VOICE_FINISHED = "user_voice_finished"
    ASSISTANT_THINKING = "assistant_thinking"
    ASSISTANT_SPEAKING = "assistant_speaking"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    TOOL_FAILED = "tool_failed"
    WIDGET_CREATE = "widget_create"
    WIDGET_UPDATE = "widget_update"
    WIDGET_FOCUS = "widget_focus"
    WIDGET_CLOSE = "widget_close"
    WIDGET_ACTION = "widget_action"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_RESOLVED = "confirmation_resolved"
    TASK_UPDATED = "task_updated"
    CHAT_MESSAGE = "chat_message"
    TRANSCRIPT_UPDATED = "transcript_updated"
    ERROR = "error"


@dataclass(frozen=True)
class JarvisEvent:
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "system"
    timestamp: float = field(default_factory=time.time)


class JarvisEventBus:
    """Thread-safe publish/subscribe with a short debug history."""

    def __init__(self, history_limit: int = 200):
        self._subscribers: dict[str, list[Callable[[JarvisEvent], None]]] = defaultdict(list)
        self._history = deque(maxlen=history_limit)
        self._lock = RLock()

    @staticmethod
    def _key(event_type: str | JarvisEventType) -> str:
        return event_type.value if isinstance(event_type, JarvisEventType) else str(event_type)

    def subscribe(self, event_type: str | JarvisEventType, callback: Callable[[JarvisEvent], None]) -> Callable[[], None]:
        key = self._key(event_type)
        with self._lock:
            self._subscribers[key].append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers.get(key, []):
                    self._subscribers[key].remove(callback)

        return unsubscribe

    def publish(
        self,
        event_type: str | JarvisEventType,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "system",
    ) -> JarvisEvent:
        event = JarvisEvent(self._key(event_type), dict(payload or {}), source)
        with self._lock:
            self._history.append(event)
            listeners = list(self._subscribers.get(event.event_type, []))
            listeners.extend(self._subscribers.get("*", []))
        for callback in listeners:
            callback(event)
        return event

    def history(self, event_type: str | JarvisEventType | None = None) -> list[JarvisEvent]:
        with self._lock:
            values = list(self._history)
        if event_type is None:
            return values
        key = self._key(event_type)
        return [event for event in values if event.event_type == key]

