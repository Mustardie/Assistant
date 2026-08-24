"""Small, bounded local store for safe desktop summaries and habits."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from threading import RLock
from typing import Any

from tools.desktop_context_models import DesktopEvent, PrivacyMode


class DesktopContextStore:
    VERSION = 1

    def __init__(self, path: Path | str, *, event_limit: int = 500,
                 habit_limit: int = 100, suggestion_limit: int = 100):
        self.path = Path(path)
        self.event_limit = max(20, int(event_limit))
        self.habit_limit = max(5, int(habit_limit))
        self.suggestion_limit = max(10, int(suggestion_limit))
        self._lock = RLock()

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "version": DesktopContextStore.VERSION,
            "config": {
                "monitoring_enabled": False,
                "privacy_mode": PrivacyMode.STANDARD.value,
                "allow_suggestions_during_gaming": False,
                "disabled_suggestion_types": [],
                "deleted_habit_ids": [],
                "suggestion_cooldown_minutes": 60,
            },
            "events": [],
            "habits": [],
            "suggestions": [],
            "prediction_feedback": [],
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            except (OSError, json.JSONDecodeError):
                value = {}
            defaults = self._defaults()
            if not isinstance(value, dict):
                return defaults
            for key in ("events", "habits", "suggestions", "prediction_feedback"):
                if not isinstance(value.get(key), list):
                    value[key] = []
            config = value.get("config") if isinstance(value.get("config"), dict) else {}
            value["config"] = {**defaults["config"], **config}
            value["version"] = self.VERSION
            return value

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def config(self) -> dict[str, Any]:
        return dict(self.load()["config"])

    def update_config(self, **changes) -> dict[str, Any]:
        with self._lock:
            value = self.load()
            value["config"].update(changes)
            value["config"]["disabled_suggestion_types"] = sorted(set(value["config"].get("disabled_suggestion_types") or []))
            value["config"]["deleted_habit_ids"] = sorted(set(value["config"].get("deleted_habit_ids") or []))
            self._write(value)
            return dict(value["config"])

    def append_event(self, event: DesktopEvent | dict[str, Any]) -> dict[str, Any]:
        record = event.to_dict() if isinstance(event, DesktopEvent) else dict(event)
        with self._lock:
            value = self.load()
            value["events"] = [*value["events"], record][-self.event_limit:]
            self._write(value)
        return record

    def events(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        values = list(self.load()["events"])
        return values[-max(0, int(limit)):] if limit is not None else values

    def clear_events(self) -> int:
        with self._lock:
            value = self.load()
            count = len(value["events"])
            value["events"] = []
            self._write(value)
            return count

    def habits(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.load()["habits"] if isinstance(item, dict)]

    def replace_habits(self, habits: list[dict[str, Any]]) -> None:
        with self._lock:
            value = self.load()
            value["habits"] = [dict(item) for item in habits][-self.habit_limit:]
            self._write(value)

    def delete_habit(self, habit_id: str) -> bool:
        with self._lock:
            value = self.load()
            before = len(value["habits"])
            value["habits"] = [item for item in value["habits"] if item.get("id") != habit_id]
            if len(value["habits"]) == before:
                return False
            self._write(value)
            return True

    def update_habit(self, habit_id: str, **changes) -> bool:
        with self._lock:
            value = self.load()
            match = next((item for item in value["habits"] if item.get("id") == habit_id), None)
            if match is None:
                return False
            match.update(changes)
            self._write(value)
            return True

    def save_suggestion(self, suggestion: dict[str, Any]) -> None:
        with self._lock:
            value = self.load()
            others = [item for item in value["suggestions"] if item.get("id") != suggestion.get("id")]
            value["suggestions"] = [*others, dict(suggestion)][-self.suggestion_limit:]
            self._write(value)

    def suggestions(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.load()["suggestions"] if isinstance(item, dict)]

    def update_suggestion(self, suggestion_id: str, **changes) -> bool:
        with self._lock:
            value = self.load()
            found = False
            for item in value["suggestions"]:
                if item.get("id") == suggestion_id:
                    item.update(changes)
                    found = True
            if found:
                self._write(value)
            return found

    def add_prediction_feedback(self, feedback: dict[str, Any]) -> None:
        with self._lock:
            value = self.load()
            value["prediction_feedback"] = [*value["prediction_feedback"], dict(feedback)][-100:]
            self._write(value)

    def debug_summary(self) -> dict[str, Any]:
        """Aggregates only; raw events and window titles are deliberately omitted."""
        value = self.load()
        event_counts = Counter(str(item.get("event_type") or "unknown") for item in value["events"])
        app_counts = Counter(str(item.get("app_name")) for item in value["events"] if item.get("app_name"))
        return {
            "version": value.get("version"),
            "config": dict(value["config"]),
            "event_count": len(value["events"]),
            "event_types": dict(event_counts),
            "top_apps": [{"app": app, "count": count} for app, count in app_counts.most_common(10)],
            "habit_count": len(value["habits"]),
            "suggestion_count": len(value["suggestions"]),
            "prediction_feedback_count": len(value["prediction_feedback"]),
            "raw_history_included": False,
        }
