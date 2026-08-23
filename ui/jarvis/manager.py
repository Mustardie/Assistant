"""Widget lifecycle, geometry, focus, and layout persistence."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ui.jarvis.events import JarvisEvent, JarvisEventBus, JarvisEventType
from ui.jarvis.models import WidgetState
from ui.jarvis.registry import WidgetRegistry

logger = logging.getLogger(__name__)


class WidgetManager:
    LAYOUT_VERSION = 1

    def __init__(self, registry: WidgetRegistry, *, event_bus: JarvisEventBus | None = None, layout_path: Path | str | None = None):
        self.registry = registry
        self.events = event_bus or JarvisEventBus()
        self.layout_path = Path(layout_path) if layout_path else None
        self._widgets: dict[str, WidgetState] = {}
        self._z_counter = 0
        self.events.subscribe(JarvisEventType.WIDGET_CREATE, self._on_create_event)
        self.events.subscribe(JarvisEventType.WIDGET_UPDATE, self._on_update_event)
        self.events.subscribe(JarvisEventType.WIDGET_FOCUS, self._on_focus_event)
        self.events.subscribe(JarvisEventType.WIDGET_CLOSE, self._on_close_event)

    def all(self, *, include_minimized: bool = True) -> list[WidgetState]:
        values = sorted(self._widgets.values(), key=lambda item: item.z_index)
        return values if include_minimized else [item for item in values if not item.minimized]

    def get(self, widget_id: str) -> WidgetState | None:
        return self._widgets.get(widget_id)

    def find_type(self, widget_type: str) -> WidgetState | None:
        return next((item for item in self._widgets.values() if item.widget_type == widget_type), None)

    def create(
        self,
        widget_type: str,
        *,
        widget_id: str | None = None,
        position: tuple[int, int] | None = None,
        size: tuple[int, int] | None = None,
        data: dict[str, Any] | None = None,
        title: str | None = None,
        loading: bool = False,
        emit: bool = True,
    ) -> WidgetState:
        spec = self.registry.get(widget_type)
        if spec.singleton:
            existing = self.find_type(widget_type)
            if existing:
                existing.minimized = False
                if data:
                    existing.data.update(data)
                existing.loading = loading
                existing.touch()
                self.focus(existing.widget_id)
                if emit:
                    self.events.publish("widget_state_changed", {"operation": "update", "widget": existing.to_dict()}, source="widget_manager")
                return existing
        self._z_counter += 1
        if position is None:
            cascade = len(self._widgets) % 8
            position = (32 + cascade * 26, 92 + cascade * 22)
        state = WidgetState.create(spec, widget_id=widget_id, position=position, size=size, data=data, title=title)
        state.z_index = self._z_counter
        state.loading = loading
        self._widgets[state.widget_id] = state
        self.save_layout()
        if emit:
            self.events.publish("widget_state_changed", {"operation": "create", "widget": state.to_dict()}, source="widget_manager")
        return state

    def update(self, widget_id: str, **changes) -> WidgetState:
        state = self._require(widget_id)
        for key, value in changes.items():
            if key == "data":
                state.data.update(value or {})
            elif hasattr(state, key) and key not in {"widget_id", "widget_type", "created_at"}:
                setattr(state, key, value)
        state.touch()
        self.save_layout()
        self.events.publish("widget_state_changed", {"operation": "update", "widget": state.to_dict()}, source="widget_manager")
        return state

    def move(self, widget_id: str, x: int, y: int) -> WidgetState:
        return self.update(widget_id, x=max(0, int(x)), y=max(0, int(y)))

    def resize(self, widget_id: str, width: int, height: int) -> WidgetState:
        state = self._require(widget_id)
        minimum = self.registry.get(state.widget_type).min_size
        return self.update(widget_id, width=max(int(width), minimum[0]), height=max(int(height), minimum[1]))

    def focus(self, widget_id: str) -> WidgetState:
        state = self._require(widget_id)
        self._z_counter += 1
        state.z_index = self._z_counter
        state.minimized = False
        state.touch()
        self.save_layout()
        self.events.publish("widget_state_changed", {"operation": "focus", "widget": state.to_dict()}, source="widget_manager")
        return state

    def toggle_collapsed(self, widget_id: str) -> WidgetState:
        state = self._require(widget_id)
        return self.update(widget_id, collapsed=not state.collapsed)

    def toggle_pinned(self, widget_id: str) -> WidgetState:
        state = self._require(widget_id)
        return self.update(widget_id, pinned=not state.pinned)

    def close(self, widget_id: str) -> bool:
        state = self._widgets.pop(widget_id, None)
        if state is None:
            return False
        self.save_layout()
        self.events.publish("widget_state_changed", {"operation": "close", "widget": state.to_dict()}, source="widget_manager")
        return True

    def reset_layout(self) -> None:
        self._widgets.clear()
        self._z_counter = 0
        self.save_layout()
        self.events.publish("widget_state_changed", {"operation": "reset"}, source="widget_manager")

    def save_layout(self) -> None:
        if self.layout_path is None:
            return
        payload = {
            "version": self.LAYOUT_VERSION,
            "widgets": [state.to_dict() for state in self.all()],
        }
        try:
            self.layout_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.layout_path.with_suffix(self.layout_path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.layout_path)
        except Exception:
            logger.exception("Unable to persist JARVIS widget layout")

    def restore_layout(self) -> list[WidgetState]:
        if self.layout_path is None or not self.layout_path.exists():
            return []
        try:
            payload = json.loads(self.layout_path.read_text(encoding="utf-8"))
            if payload.get("version") != self.LAYOUT_VERSION:
                return []
            restored = []
            for raw in payload.get("widgets", []):
                state = WidgetState.from_dict(raw)
                self.registry.get(state.widget_type)
                self._widgets[state.widget_id] = state
                self._z_counter = max(self._z_counter, state.z_index)
                restored.append(state)
            return restored
        except Exception:
            logger.exception("Unable to restore JARVIS widget layout")
            return []

    def _require(self, widget_id: str) -> WidgetState:
        state = self.get(widget_id)
        if state is None:
            raise KeyError(f"Unknown widget id: {widget_id}")
        return state

    def _on_create_event(self, event: JarvisEvent) -> None:
        payload = event.payload
        if "widget_type" in payload:
            self.create(
                payload["widget_type"],
                position=tuple(payload["position"]) if payload.get("position") else None,
                size=tuple(payload["size"]) if payload.get("size") else None,
                data=payload.get("data"),
                title=payload.get("title"),
                loading=bool(payload.get("loading")),
            )

    def _on_update_event(self, event: JarvisEvent) -> None:
        widget_id = event.payload.get("widget_id")
        if widget_id and widget_id in self._widgets:
            changes = {key: value for key, value in event.payload.items() if key != "widget_id"}
            self.update(widget_id, **changes)

    def _on_focus_event(self, event: JarvisEvent) -> None:
        widget_id = event.payload.get("widget_id")
        if widget_id and widget_id in self._widgets:
            self.focus(widget_id)

    def _on_close_event(self, event: JarvisEvent) -> None:
        widget_id = event.payload.get("widget_id")
        if widget_id:
            self.close(widget_id)
