"""Privacy-first contracts for background desktop context and habits."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


class Contract:
    def to_dict(self) -> dict[str, Any]:
        return _json(asdict(self))


class PrivacyMode(str, Enum):
    STANDARD = "standard"
    STRICT = "strict"
    OFF = "off"


class MonitoringState(str, Enum):
    DISABLED = "disabled"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class DesktopEventType(str, Enum):
    CONTEXT_SNAPSHOT = "context_snapshot"
    ACTIVE_APP_CHANGED = "active_app_changed"
    APP_ACTION = "app_action"
    FILE_ACTIVITY = "file_activity"
    DOWNLOAD_DETECTED = "download_detected"
    JARVIS_COMMAND = "jarvis_command"
    TOOL_STATUS = "tool_status"
    SKILL_STATUS = "skill_status"
    WIDGET_STATUS = "widget_status"
    CONFIRMATION = "confirmation"
    CONNECTOR_STATUS = "connector_status"
    SYSTEM_LIFECYCLE = "system_lifecycle"
    IDLE_STATUS = "idle_status"
    MONITORING_STATUS = "monitoring_status"
    SUGGESTION_FEEDBACK = "suggestion_feedback"


class WorkMode(str, Enum):
    CODING = "coding"
    EDITING = "editing"
    STUDY = "study"
    RECORDING = "minecraft_recording"
    RESEARCH = "browsing_research"
    COMMUNICATION = "communication"
    ASSIGNMENT = "assignment"
    IDLE = "idle"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WindowContext(Contract):
    app_name: str
    title: str | None = None
    title_redacted: bool = False
    active: bool = False
    minimized: bool | None = None
    maximized: bool | None = None


@dataclass(frozen=True)
class DesktopEvent(Contract):
    event_type: DesktopEventType
    timestamp: str = field(default_factory=utc_now)
    app_name: str | None = None
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0


@dataclass(frozen=True)
class AppUsageEvent(Contract):
    app_name: str
    action: str
    timestamp: str = field(default_factory=utc_now)
    active: bool = False
    title: str | None = None


@dataclass(frozen=True)
class FileActivityEvent(Contract):
    action: str
    path: str
    timestamp: str = field(default_factory=utc_now)
    app_name: str | None = None
    extension: str = ""


@dataclass(frozen=True)
class MonitoringStatus(Contract):
    state: MonitoringState
    enabled: bool
    paused: bool
    privacy_mode: PrivacyMode
    poll_interval_seconds: float
    events_stored: int = 0
    last_snapshot_at: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class StartupConfig(Contract):
    enabled: bool
    supported: bool
    startup_path: str | None = None
    start_minimized: bool = True
    method: str = "windows_startup_folder"
    requires_confirmation: bool = True
    evidence: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class DesktopContext(Contract):
    active_app: str | None
    active_window: WindowContext | None
    important_running_apps: tuple[str, ...] = ()
    recently_opened_files: tuple[dict[str, Any], ...] = ()
    recent_downloads: tuple[dict[str, Any], ...] = ()
    current_task: str | None = None
    widgets_open: tuple[str, ...] = ()
    connector_status: dict[str, str] = field(default_factory=dict)
    idle_seconds: float | None = None


@dataclass(frozen=True)
class DesktopContextSnapshot(Contract):
    timestamp: str
    context: DesktopContext
    monitoring_state: MonitoringState
    privacy_mode: PrivacyMode
    confidence: float
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class HabitSignal(Contract):
    signal_type: str
    value: str
    weight: float
    timestamp: str
    evidence: str


@dataclass(frozen=True)
class RoutinePattern(Contract):
    id: str
    name: str
    trigger_conditions: tuple[str, ...]
    apps: tuple[str, ...]
    files: tuple[str, ...]
    actions: tuple[str, ...]
    frequency: int
    confidence: float
    last_seen: str
    examples: tuple[dict[str, Any], ...]
    suggested_automation: str
    suggested_skill: str
    auto_suggest_allowed: bool = True
    confirmation_required: bool = True
    disabled: bool = False


@dataclass(frozen=True)
class ContextPrediction(Contract):
    mode: WorkMode
    confidence: float
    evidence: tuple[str, ...]
    suggested_widgets: tuple[str, ...] = ()
    suggested_actions: tuple[str, ...] = ()
    suggested_skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProactiveSuggestion(Contract):
    id: str
    suggestion_type: str
    title: str
    message: str
    created_at: str
    expires_at: str
    confidence: float
    evidence: tuple[str, ...]
    action_plan: tuple[dict[str, Any], ...] = ()
    requires_confirmation: bool = False
    dismissible: bool = True
    routine_id: str | None = None


@dataclass(frozen=True)
class SkillSuggestionPlan(Contract):
    routine_id: str
    skill_name: str
    description: str
    trigger_phrases: tuple[str, ...]
    steps: tuple[dict[str, Any], ...]
    required_tools: tuple[str, ...]
    requires_approval_to_save: bool = True
    requires_approval_to_run: bool = True
    saved: bool = False

