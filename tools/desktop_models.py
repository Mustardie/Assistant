"""Serializable contracts for safe Windows desktop awareness and control."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class AppStatus(str, Enum):
    RUNNING = "running"
    NOT_RUNNING = "not_running"
    UNKNOWN = "unknown"


class AppAction(str, Enum):
    FIND = "find"
    OPEN = "open"
    FOCUS = "focus"
    OPEN_FILE = "open_file"
    SHOW_IN_FOLDER = "show_in_folder"
    OPEN_FOLDER = "open_folder"
    LIST_WINDOWS = "list_windows"
    GET_ACTIVE_WINDOW = "get_active_window"
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    RESTORE = "restore"
    CLOSE = "close"
    CLOSE_ALL = "close_all"
    KILL_PROCESS = "kill_process"
    RUN_SHELL = "run_shell"
    AUTOMATE_INPUT = "automate_input"


class DesktopRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CONFIRMATION_REQUIRED = "confirmation_required"
    NOT_FOUND = "not_found"


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class SerializableContract:
    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))


@dataclass(frozen=True)
class AppCapability(SerializableContract):
    action: AppAction
    risk: DesktopRisk = DesktopRisk.LOW
    requires_confirmation: bool = False
    available: bool = True
    reason: str = ""


@dataclass(frozen=True)
class AppIdentity(SerializableContract):
    name: str
    canonical_name: str
    executable_path: str | None = None
    process_id: int | None = None
    window_title: str | None = None
    window_handle: int | None = None
    status: AppStatus = AppStatus.UNKNOWN
    active: bool = False
    focused: bool = False
    minimized: bool | None = None
    maximized: bool | None = None
    safe_actions: tuple[str, ...] = ()
    risky_actions: tuple[str, ...] = ()
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    source: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppLaunchRequest(SerializableContract):
    app: str
    file_path: str | None = None
    working_directory: str | None = None
    arguments: tuple[str, ...] = ()
    focus_existing: bool = True
    verify: bool = True
    confirmation_id: str | None = None
    confirmed: bool = False


@dataclass(frozen=True)
class AppLaunchResult(SerializableContract):
    success: bool
    status: ActionStatus
    request: AppLaunchRequest
    app: AppIdentity | None = None
    focused_existing: bool = False
    launched: bool = False
    verified: bool = False
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    error: str | None = None
    suggested_fixes: tuple[str, ...] = ()
    requires_confirmation: bool = False
    confirmation: dict[str, Any] | None = None


@dataclass(frozen=True)
class WindowInfo(SerializableContract):
    app_name: str
    executable_path: str | None
    process_id: int | None
    window_title: str
    window_handle: int
    status: AppStatus = AppStatus.RUNNING
    active: bool = False
    focused: bool = False
    minimized: bool | None = None
    maximized: bool | None = None
    visible: bool = True
    safe_actions: tuple[str, ...] = (
        "focus", "minimize", "maximize", "restore",
    )
    risky_actions: tuple[str, ...] = ("close", "kill_process")
    confidence: float = 0.8
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class DesktopState(SerializableContract):
    supported: bool
    platform: str
    active_window: WindowInfo | None
    windows: tuple[WindowInfo, ...] = ()
    capabilities: tuple[AppCapability, ...] = ()
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class AppActionResult(SerializableContract):
    success: bool
    status: ActionStatus
    action: AppAction
    target: str | None = None
    app: AppIdentity | None = None
    windows: tuple[WindowInfo, ...] = ()
    verified: bool = False
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    error: str | None = None
    requires_confirmation: bool = False
    confirmation: dict[str, Any] | None = None
    next_step: str | None = None


@dataclass(frozen=True)
class DesktopActionPlan(SerializableContract):
    request: str
    intended_action: AppAction | None
    target_app: str | None = None
    target_file: str | None = None
    target_window: int | None = None
    target_process_id: int | None = None
    risk: DesktopRisk = DesktopRisk.LOW
    confirmation_needed: bool = False
    missing_target: bool = False
    expected_result: str = ""
    fallback_strategy: tuple[str, ...] = ()
    verification_method: str = ""
    rationale: tuple[str, ...] = ()
    confirmation_id: str | None = None
    expires_at: str | None = None
