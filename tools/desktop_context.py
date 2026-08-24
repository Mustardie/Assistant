"""Opt-in background desktop context service with bounded local learning."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from config.paths import get_nova_app_file
from memory.desktop_context_store import DesktopContextStore
from tools.desktop_context_models import (
    DesktopContext, DesktopContextSnapshot, DesktopEvent, DesktopEventType,
    MonitoringState, MonitoringStatus, PrivacyMode, WindowContext, utc_now,
)
from tools.desktop_control import get_desktop_service
from tools.desktop_habits import ContextPredictor, HabitLearner, SuggestionEngine


_SENSITIVE_APPS = {
    "1password", "bitwarden", "keepass", "password", "authenticator",
    "discord", "whatsapp", "telegram", "signal", "outlook", "mail",
    "chrome", "edge", "firefox", "brave", "bank", "wallet",
}
_SENSITIVE_TITLE = re.compile(
    r"\b(password|passcode|token|secret|private|incognito|inprivate|bank|wallet|"
    r"authentication|verification code|inbox|direct message|chat)\b", re.I,
)
_COMMAND_TERMS = {
    "coding", "code", "assignment", "worksheet", "homework", "teacher",
    "study", "research", "minecraft", "recording", "editing", "premiere",
    "download", "pdf", "git", "routine", "skill", "monitoring", "startup",
}


def safe_window_title(app_name: str, title: str | None, privacy_mode: PrivacyMode) -> tuple[str | None, bool]:
    if not title:
        return None, False
    if privacy_mode is PrivacyMode.STRICT:
        return None, True
    lowered_app = app_name.lower()
    if privacy_mode is PrivacyMode.STANDARD and (
        any(term in lowered_app for term in _SENSITIVE_APPS) or _SENSITIVE_TITLE.search(title)
    ):
        return None, True
    clean = " ".join(str(title).replace("\r", " ").replace("\n", " ").split())[:160]
    return clean or None, False


class DesktopContextService:
    def __init__(self, *, store: DesktopContextStore | None = None, desktop_service=None,
                 poll_interval_seconds: float = 10.0,
                 widget_provider: Callable[[], list[str]] | None = None,
                 task_provider: Callable[[], str | None] | None = None,
                 connector_provider: Callable[[], dict[str, str]] | None = None):
        self.store = store or DesktopContextStore(get_nova_app_file("desktop_context.json"))
        self.desktop = desktop_service or get_desktop_service()
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self.widget_provider = widget_provider or (lambda: [])
        self.task_provider = task_provider or (lambda: None)
        self.connector_provider = connector_provider or (lambda: {})
        self.predictor = ContextPredictor()
        self.learner = HabitLearner(self.predictor)
        self.suggestion_engine = SuggestionEngine(self.store)
        config = self.store.config()
        self._state = MonitoringState.STOPPED if config.get("monitoring_enabled") else MonitoringState.DISABLED
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._last_active_app: str | None = None
        self._last_connector_status: dict[str, str] | None = None
        self._last_idle_state: str | None = None
        self._last_snapshot_at: str | None = None
        self._last_persisted_at: datetime | None = None
        self._last_error: str | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    @property
    def privacy_mode(self) -> PrivacyMode:
        try:
            return PrivacyMode(self.store.config().get("privacy_mode", PrivacyMode.STANDARD.value))
        except ValueError:
            return PrivacyMode.STANDARD

    def configure_providers(self, *, widget_provider=None, task_provider=None, connector_provider=None) -> None:
        if widget_provider is not None:
            self.widget_provider = widget_provider
        if task_provider is not None:
            self.task_provider = task_provider
        if connector_provider is not None:
            self.connector_provider = connector_provider

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def _publish(self, payload: dict[str, Any]) -> None:
        for callback in list(self._listeners):
            try:
                callback(dict(payload))
            except Exception:
                continue

    def status(self) -> MonitoringStatus:
        return MonitoringStatus(
            self._state, bool(self.store.config().get("monitoring_enabled")),
            self._state is MonitoringState.PAUSED, self.privacy_mode,
            self.poll_interval_seconds, len(self.store.events()), self._last_snapshot_at, self._last_error,
        )

    def start(self, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            return {
                "success": False, "status": "confirmation_required", "requires_confirmation": True,
                "error": "Background desktop monitoring is opt-in and requires confirmation.",
                "confirmation": {"action": "Enable background desktop monitoring", "risk": "Stores bounded local app/context metadata", "target": str(self.store.path)},
            }
        with self._lock:
            self.store.update_config(monitoring_enabled=True)
            self._state = MonitoringState.RUNNING
            self._stop.clear()
            self._wake.set()
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-desktop-context")
                self._thread.start()
        self.record_event(DesktopEventType.MONITORING_STATUS, summary="monitoring enabled", metadata={"state": "running"})
        self._publish({"event": "monitoring", "status": self.status().to_dict()})
        return {"success": True, "status": self.status().to_dict()}

    def autostart_if_enabled(self) -> bool:
        if not self.store.config().get("monitoring_enabled"):
            return False
        return bool(self.start(confirm=True).get("success"))

    def stop(self, *, confirm: bool = False, disable: bool = True) -> dict[str, Any]:
        if disable and not confirm:
            return {
                "success": False, "status": "confirmation_required", "requires_confirmation": True,
                "error": "Disabling persistent monitoring requires confirmation.",
                "confirmation": {"action": "Disable background desktop monitoring", "risk": "Stops future context collection", "target": str(self.store.path)},
            }
        with self._lock:
            if disable:
                self.store.update_config(monitoring_enabled=False)
            self._state = MonitoringState.DISABLED if disable else MonitoringState.STOPPED
            self._stop.set()
            self._wake.set()
        self.record_event(DesktopEventType.MONITORING_STATUS, summary="monitoring stopped", metadata={"state": self._state.value})
        self._publish({"event": "monitoring", "status": self.status().to_dict()})
        return {"success": True, "status": self.status().to_dict()}

    def pause(self) -> dict[str, Any]:
        if self._state is not MonitoringState.RUNNING:
            return {"success": False, "error": "Monitoring is not running.", "status": self.status().to_dict()}
        self._state = MonitoringState.PAUSED
        self.record_event(DesktopEventType.MONITORING_STATUS, summary="monitoring paused", metadata={"state": "paused"})
        self._publish({"event": "monitoring", "status": self.status().to_dict()})
        return {"success": True, "status": self.status().to_dict()}

    def resume(self) -> dict[str, Any]:
        if not self.store.config().get("monitoring_enabled"):
            return {"success": False, "error": "Monitoring is disabled; enabling it requires confirmation.", "status": self.status().to_dict()}
        self._state = MonitoringState.RUNNING
        self._wake.set()
        self.record_event(DesktopEventType.MONITORING_STATUS, summary="monitoring resumed", metadata={"state": "running"})
        self._publish({"event": "monitoring", "status": self.status().to_dict()})
        return {"success": True, "status": self.status().to_dict()}

    def set_privacy_mode(self, mode: str) -> dict[str, Any]:
        try:
            privacy = PrivacyMode(str(mode).lower())
        except ValueError:
            return {"success": False, "error": "Privacy mode must be standard, strict, or off."}
        self.store.update_config(privacy_mode=privacy.value)
        return {"success": True, "privacy_mode": privacy.value, "titles_redacted": privacy is not PrivacyMode.OFF}

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if self._state is MonitoringState.RUNNING:
                try:
                    snapshot = self.capture_snapshot(persist=True)
                    self._last_error = None
                    self._publish({"event": "context_updated", "snapshot": snapshot.to_dict()})
                except Exception as exc:
                    self._last_error = str(exc)
                    self._state = MonitoringState.ERROR
                    self._publish({"event": "monitor_error", "error": str(exc)})
            self._wake.wait(self.poll_interval_seconds)

    def capture_snapshot(self, *, persist: bool = False) -> DesktopContextSnapshot:
        state = self.desktop.get_state()
        active = state.active_window
        title, redacted = safe_window_title(active.app_name, active.window_title, self.privacy_mode) if active else (None, False)
        active_context = WindowContext(
            active.app_name, title, redacted, True, active.minimized, active.maximized,
        ) if active else None
        apps = tuple(dict.fromkeys(window.app_name for window in state.windows if window.app_name))
        recent = self.store.events(limit=100)
        files = []
        downloads = []
        for event in reversed(recent):
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            if event.get("event_type") == DesktopEventType.FILE_ACTIVITY.value and len(files) < 10:
                files.append({key: metadata.get(key) for key in ("path", "extension", "action", "app_name") if metadata.get(key) is not None})
            elif event.get("event_type") == DesktopEventType.DOWNLOAD_DETECTED.value and len(downloads) < 10:
                downloads.append({key: metadata.get(key) for key in ("path", "extension", "source") if metadata.get(key) is not None})
        idle_seconds = None
        try:
            idle_seconds = self.desktop.windows.idle_seconds()
        except (AttributeError, OSError):
            pass
        task = self._safe_task(self.task_provider())
        context = DesktopContext(
            active.app_name if active else None, active_context, apps,
            tuple(files), tuple(downloads), task,
            tuple(dict.fromkeys(str(item) for item in self.widget_provider() if item)),
            {str(key): str(value) for key, value in self.connector_provider().items()}, idle_seconds,
        )
        evidence = list(state.evidence)
        if redacted:
            evidence.append("active window title redacted by privacy policy")
        snapshot = DesktopContextSnapshot(
            utc_now(), context, self._state, self.privacy_mode,
            state.confidence if state.supported else 0.2, tuple(evidence),
        )
        self._last_snapshot_at = snapshot.timestamp
        if persist:
            changed = context.active_app != self._last_active_app
            if changed:
                self.record_event(DesktopEventType.ACTIVE_APP_CHANGED, app_name=context.active_app,
                                  summary="active app changed", metadata={"apps": list(apps)})
                self._last_active_app = context.active_app
            idle_state = self._idle_bucket(idle_seconds)
            if idle_state != self._last_idle_state:
                self.record_event(DesktopEventType.IDLE_STATUS, summary="idle state changed",
                                  metadata={"state": idle_state})
                self._last_idle_state = idle_state
            connector_state = dict(context.connector_status)
            if connector_state != self._last_connector_status:
                self.record_event(DesktopEventType.CONNECTOR_STATUS, summary="connector status changed",
                                  metadata={"connectors": [f"{name}:{status}" for name, status in sorted(connector_state.items())]})
                self._last_connector_status = connector_state
            now = datetime.now(timezone.utc)
            if changed or self._last_persisted_at is None or now - self._last_persisted_at >= timedelta(minutes=5):
                self.record_event(DesktopEventType.CONTEXT_SNAPSHOT, app_name=context.active_app,
                                  summary="safe context snapshot", metadata={"apps": list(apps), "idle": self._idle_bucket(idle_seconds)})
                self._last_persisted_at = now
                self.learn_habits()
        return snapshot

    def prediction(self) -> dict[str, Any]:
        snapshot = self.capture_snapshot(persist=False)
        prediction = self.predictor.predict(snapshot, self.store.events(limit=50))
        return {"success": True, "prediction": prediction.to_dict(), "snapshot": snapshot.to_dict()}

    def suggestions(self, *, generate: bool = True) -> dict[str, Any]:
        if generate:
            snapshot = self.capture_snapshot(persist=False)
            prediction = self.predictor.predict(snapshot, self.store.events(limit=50))
            created = self.suggestion_engine.generate(snapshot, prediction, self.store.habits())
        else:
            created = []
        now = datetime.now(timezone.utc)
        active = [item for item in self.store.suggestions() if not item.get("dismissed") and self._parse_time(item.get("expires_at")) >= now]
        return {"success": True, "suggestions": active, "new": [item.to_dict() for item in created]}

    def learn_habits(self) -> list[dict[str, Any]]:
        patterns = self.learner.detect(self.store.events(), self.store.habits())
        deleted = set(self.store.config().get("deleted_habit_ids") or [])
        patterns = [item for item in patterns if item.id not in deleted]
        self.store.replace_habits([item.to_dict() for item in patterns])
        return [item.to_dict() for item in patterns]

    def habit_explain(self, habit_id: str) -> dict[str, Any]:
        match = next((item for item in self.store.habits() if item.get("id") == habit_id), None)
        return {"success": bool(match), "habit": match, "error": None if match else "Habit not found.", "raw_events_included": False}

    def habit_delete(self, habit_id: str) -> dict[str, Any]:
        deleted = self.store.delete_habit(habit_id)
        if deleted:
            suppressed = set(self.store.config().get("deleted_habit_ids") or [])
            self.store.update_config(deleted_habit_ids=sorted(suppressed | {habit_id}))
        return {"success": deleted, "deleted": habit_id if deleted else None, "error": None if deleted else "Habit not found."}

    def habit_disable(self, habit_id: str, disabled: bool = True) -> dict[str, Any]:
        changed = self.store.update_habit(habit_id, disabled=bool(disabled), auto_suggest_allowed=not bool(disabled))
        return {"success": changed, "habit_id": habit_id, "disabled": bool(disabled),
                "error": None if changed else "Habit not found."}

    def create_skill_plan(self, habit_id: str) -> dict[str, Any]:
        match = next((item for item in self.store.habits() if item.get("id") == habit_id), None)
        if not match:
            return {"success": False, "error": "Habit not found."}
        return {"success": True, "plan": self.learner.skill_plan(match).to_dict(),
                "notice": "This plan has not been saved or run. User approval is required."}

    def dismiss_suggestion(self, suggestion_id: str) -> dict[str, Any]:
        dismissed = self.suggestion_engine.dismiss(suggestion_id)
        if dismissed:
            self.record_event(DesktopEventType.SUGGESTION_FEEDBACK, summary="suggestion dismissed", metadata={"suggestion_id": suggestion_id, "outcome": "dismissed"})
        return {"success": dismissed, "error": None if dismissed else "Suggestion not found."}

    def accept_suggestion(self, suggestion_id: str) -> dict[str, Any]:
        accepted = self.store.update_suggestion(suggestion_id, accepted=True, accepted_at=utc_now())
        if accepted:
            self.record_event(DesktopEventType.SUGGESTION_FEEDBACK, summary="suggestion accepted",
                              metadata={"suggestion_id": suggestion_id, "outcome": "accepted"})
        return {"success": accepted, "executed": False,
                "notice": "Acceptance was learned, but no action was executed." if accepted else None,
                "error": None if accepted else "Suggestion not found."}

    def disable_suggestion_type(self, suggestion_type: str) -> dict[str, Any]:
        config = self.store.config()
        disabled = sorted(set(config.get("disabled_suggestion_types") or []) | {str(suggestion_type)})
        self.store.update_config(disabled_suggestion_types=disabled)
        return {"success": True, "disabled_suggestion_types": disabled}

    def set_gaming_suggestions(self, allowed: bool) -> dict[str, Any]:
        self.store.update_config(allow_suggestions_during_gaming=bool(allowed))
        return {"success": True, "allow_suggestions_during_gaming": bool(allowed)}

    def mark_prediction_wrong(self, predicted_mode: str, actual_mode: str | None = None) -> dict[str, Any]:
        self.store.add_prediction_feedback({"timestamp": utc_now(), "predicted_mode": predicted_mode, "actual_mode": actual_mode})
        return {"success": True, "recorded": True, "raw_context_stored": False}

    def clear_activity(self) -> dict[str, Any]:
        return {"success": True, "cleared": self.store.clear_events()}

    def activity_timeline(self, limit: int = 50) -> dict[str, Any]:
        values = []
        for event in self.store.events(limit=max(1, min(int(limit), 100))):
            values.append({key: event.get(key) for key in ("timestamp", "event_type", "app_name", "summary", "confidence")})
        return {"success": True, "activity": values, "raw_metadata_included": False}

    def record_command(self, command: str) -> dict[str, Any]:
        words = {word.lower() for word in re.findall(r"[A-Za-z]+", str(command))}
        terms = sorted(words & _COMMAND_TERMS)
        return self.record_event(DesktopEventType.JARVIS_COMMAND, summary="JARVIS command issued",
                                 metadata={"intent_terms": terms, "length_bucket": self._length_bucket(len(str(command)))})

    def record_system_lifecycle(self, state: str) -> dict[str, Any]:
        normalized = "startup" if str(state).lower() == "startup" else "shutdown"
        return self.record_event(DesktopEventType.SYSTEM_LIFECYCLE, summary=f"system {normalized}",
                                 metadata={"state": normalized})

    def observe_runtime_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        payload = dict(payload or {})
        if event_type in {"tool_started", "tool_finished", "tool_failed"}:
            self.record_event(DesktopEventType.TOOL_STATUS, summary="tool status changed",
                              metadata={"tool": str(payload.get("tool") or "unknown")[:80],
                                        "status": event_type.removeprefix("tool_"),
                                        "verified": bool(payload.get("verified"))})
        elif event_type.startswith("skill_"):
            self.record_event(DesktopEventType.SKILL_STATUS, summary="skill status changed",
                              metadata={"status": event_type.removeprefix("skill_")})
        elif event_type == "confirmation_resolved":
            self.record_event(DesktopEventType.CONFIRMATION, summary="confirmation resolved",
                              metadata={"approved": bool(payload.get("approved"))})

    def observe_ui_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        payload = dict(payload or {})
        if event_type in {"widget_state_changed", "widget_create", "widget_close", "widget_action"}:
            widget = payload.get("widget") if isinstance(payload.get("widget"), dict) else {}
            operation = str(payload.get("operation") or event_type)[:40]
            if event_type == "widget_state_changed" and operation not in {"create", "close"}:
                return
            self.record_event(DesktopEventType.WIDGET_STATUS, summary="widget status changed",
                              metadata={"widget_type": str(widget.get("widget_type") or payload.get("widget_type") or "unknown")[:80],
                                        "operation": operation})
        elif event_type == "confirmation_resolved":
            self.record_event(DesktopEventType.CONFIRMATION, summary="confirmation resolved",
                              metadata={"approved": bool(payload.get("approved"))})

    def record_file_activity(self, action: str, path: str, app_name: str | None = None, *, download=False, source="jarvis") -> dict[str, Any]:
        target = Path(str(path))
        event_type = DesktopEventType.DOWNLOAD_DETECTED if download else DesktopEventType.FILE_ACTIVITY
        return self.record_event(event_type, app_name=app_name, summary="file activity",
                                 metadata={"action": str(action)[:40], "path": str(target)[:512],
                                           "extension": target.suffix.lower(), "app_name": app_name, "source": source})

    def record_event(self, event_type: DesktopEventType, *, app_name: str | None = None,
                     summary: str, metadata: dict[str, Any]) -> dict[str, Any]:
        if not self.store.config().get("monitoring_enabled"):
            return {"stored": False, "reason": "desktop monitoring is disabled"}
        event = DesktopEvent(event_type, app_name=app_name, summary=summary[:120],
                             metadata=self._safe_metadata(metadata))
        return self.store.append_event(event)

    @staticmethod
    def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "state", "apps", "idle", "action", "path", "extension", "app_name", "source",
            "intent_terms", "length_bucket", "tool", "status", "verified", "approved",
            "widget_type", "operation", "suggestion_id", "outcome",
            "connectors",
        }
        result = {}
        for key, value in metadata.items():
            if key not in allowed:
                continue
            if isinstance(value, list):
                result[key] = [str(item)[:120] for item in value[:30]]
            elif isinstance(value, (str, bool, int, float)) or value is None:
                result[key] = value if not isinstance(value, str) else value[:512]
        return result

    def _safe_task(self, task: str | None) -> str | None:
        if not task or self.privacy_mode is PrivacyMode.STRICT:
            return None
        text = " ".join(str(task).replace("\n", " ").split())
        if _SENSITIVE_TITLE.search(text):
            return "Private JARVIS task"
        terms = sorted({word.lower() for word in re.findall(r"[A-Za-z]+", text)} & _COMMAND_TERMS)
        return f"Active JARVIS task · {', '.join(terms)}" if terms else "Active JARVIS task"

    @staticmethod
    def _idle_bucket(seconds: float | None) -> str:
        if seconds is None:
            return "unknown"
        return "idle" if seconds >= 600 else "active"

    @staticmethod
    def _length_bucket(length: int) -> str:
        return "short" if length < 40 else "medium" if length < 160 else "long"

    @staticmethod
    def _parse_time(value: str | None) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return datetime.now(timezone.utc) - timedelta(days=1)


_context_service: DesktopContextService | None = None


def get_desktop_context_service() -> DesktopContextService:
    global _context_service
    if _context_service is None:
        _context_service = DesktopContextService()
    return _context_service


def set_desktop_context_service(service: DesktopContextService | None) -> None:
    global _context_service
    _context_service = service
