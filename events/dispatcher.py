"""EventDispatcher -- routes AppEvents to registered handlers.

Handlers subscribe to either a specific source, a specific event type,
or everything. Emission is synchronous and ordered by registration so
workflows can rely on deterministic handling. Handlers are expected to be
fast (they typically enqueue work); anything slow should run in its own
thread.
"""

import logging
import threading
from collections import defaultdict

from .models import AppEvent, make_event

logger = logging.getLogger(__name__)


class EventDispatcher:
    def __init__(self):
        self._by_type: dict[str, list] = defaultdict(list)
        self._by_source: dict[str, list] = defaultdict(list)
        self._all: list = []
        self._lock = threading.RLock()
        self._history: list[dict] = []
        self._max_history = 500

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register_handler(self, handler, *, event_type: str | None = None,
                         source: str | None = None) -> None:
        """Register a handler callable. Scope by event_type and/or source
        (both None = all events)."""
        with self._lock:
            if event_type:
                self._by_type[event_type.upper()].append(handler)
            if source:
                self._by_source[source.lower()].append(handler)
            if not event_type and not source:
                self._all.append(handler)

    def unregister_handler(self, handler) -> None:
        with self._lock:
            for bucket in list(self._by_type.values()):
                if handler in bucket:
                    bucket.remove(handler)
            for bucket in list(self._by_source.values()):
                if handler in bucket:
                    bucket.remove(handler)
            if handler in self._all:
                self._all.remove(handler)

    def clear(self) -> None:
        with self._lock:
            self._by_type.clear()
            self._by_source.clear()
            self._all.clear()
            self._history.clear()

    # ------------------------------------------------------------------ #
    # Emission
    # ------------------------------------------------------------------ #

    def emit(self, event: AppEvent) -> None:
        """Dispatch an event to all matching handlers. Never raises to the
        emitter: handler exceptions are logged and swallowed so a single
        bad handler can't take down the pipeline."""
        with self._lock:
            handlers = list(self._all)
            handlers += list(self._by_type.get(event.type.upper(), []))
            handlers += list(self._by_source.get(event.source.lower(), []))
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:
                logger.exception("[Events] Handler failed for %s: %s", event.type, exc)

        with self._lock:
            self._history.append(event.to_dict())
            if len(self._history) > self._max_history:
                del self._history[: len(self._history) - self._max_history]

    def emit_event(self, source: str, event_type: str, **kwargs) -> AppEvent:
        event = make_event(source, event_type, **kwargs)
        self.emit(event)
        return event

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def handler_count(self) -> int:
        with self._lock:
            return len(self._all) + sum(len(v) for v in self._by_type.values()) \
                + sum(len(v) for v in self._by_source.values())

    def recent_events(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._history[-limit:])

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()


event_dispatcher = EventDispatcher()