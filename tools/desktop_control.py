"""Reliable, verification-first desktop service for general JARVIS."""

from __future__ import annotations

import os
import platform
import subprocess
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from tools.app_catalog import matches_app, resolve_known_app
from tools.app_discovery import WindowsAppDiscovery
from tools.desktop_file_intent import choose_app_for_path
from tools.desktop_models import (
    ActionStatus, AppAction, AppActionResult, AppCapability, AppIdentity,
    AppLaunchRequest, AppLaunchResult, AppStatus, DesktopActionPlan,
    DesktopRisk, DesktopState, WindowInfo,
)
from tools.desktop_planner import DesktopActionPlanner
from tools.desktop_windows import WindowsWindowBackend


def _default_launch(command: list[str], cwd: str | None = None):
    return subprocess.Popen(command, cwd=cwd or None)


def _default_shell_open(path: str) -> None:
    if not hasattr(os, "startfile"):
        raise RuntimeError("Windows shell file opening is unavailable on this platform")
    os.startfile(path)  # type: ignore[attr-defined]


def _default_kill(process_id: int) -> bool:
    completed = subprocess.run(
        ["taskkill", "/PID", str(process_id), "/T", "/F"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    return completed.returncode == 0


class DesktopControlService:
    def __init__(self, *, discovery=None, windows=None,
                 launcher: Callable[[list[str], str | None], object] | None = None,
                 shell_open: Callable[[str], None] | None = None,
                 process_killer: Callable[[int], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 verify_attempts: int = 6, verify_interval: float = 0.15):
        self.discovery = discovery or WindowsAppDiscovery()
        self.windows = windows or WindowsWindowBackend()
        self.launcher = launcher or _default_launch
        self.shell_open = shell_open or _default_shell_open
        self.process_killer = process_killer or _default_kill
        self.sleep = sleep
        self.verify_attempts = max(1, verify_attempts)
        self.verify_interval = max(0.0, verify_interval)
        self.planner = DesktopActionPlanner()
        self._pending: dict[str, dict] = {}

    @staticmethod
    def _identity_from_window(window: WindowInfo) -> AppIdentity:
        known = resolve_known_app(window.app_name)
        canonical = known.canonical_name if known else window.app_name
        return AppIdentity(
            name=canonical, canonical_name=canonical,
            executable_path=window.executable_path, process_id=window.process_id,
            window_title=window.window_title, window_handle=window.window_handle,
            status=AppStatus.RUNNING, active=window.active, focused=window.focused,
            minimized=window.minimized, maximized=window.maximized,
            safe_actions=window.safe_actions, risky_actions=window.risky_actions,
            confidence=window.confidence, evidence=window.evidence,
            source="visible_window", aliases=known.aliases if known else (),
        )

    @staticmethod
    def _capabilities() -> tuple[AppCapability, ...]:
        low = (AppAction.OPEN, AppAction.FOCUS, AppAction.OPEN_FILE, AppAction.SHOW_IN_FOLDER,
               AppAction.OPEN_FOLDER, AppAction.LIST_WINDOWS, AppAction.GET_ACTIVE_WINDOW,
               AppAction.MINIMIZE, AppAction.MAXIMIZE, AppAction.RESTORE)
        risky = (AppAction.CLOSE, AppAction.CLOSE_ALL, AppAction.KILL_PROCESS,
                 AppAction.RUN_SHELL, AppAction.AUTOMATE_INPUT)
        return tuple(AppCapability(action) for action in low) + tuple(
            AppCapability(action, DesktopRisk.HIGH, True, reason="Can lose work or cause external side effects")
            for action in risky
        )

    def get_state(self) -> DesktopState:
        if not getattr(self.windows, "supported", False):
            return DesktopState(False, platform.system(), None, capabilities=self._capabilities(),
                                error="Win32 desktop APIs are unavailable; no desktop state was inferred.",
                                evidence=("window backend reported unsupported",))
        try:
            windows = tuple(self.windows.list_windows())
            active = next((item for item in windows if item.active), None)
            return DesktopState(True, platform.system(), active, windows, self._capabilities(),
                                confidence=0.98 if active else 0.8,
                                evidence=(f"enumerated {len(windows)} visible top-level window(s)",))
        except Exception as exc:
            return DesktopState(False, platform.system(), None, capabilities=self._capabilities(),
                                error=f"Window enumeration failed: {exc}", evidence=("Win32 call failed",))

    def list_windows(self, app: str | None = None) -> AppActionResult:
        state = self.get_state()
        if not state.supported:
            return AppActionResult(False, ActionStatus.FAILED, AppAction.LIST_WINDOWS,
                                   target=app, error=state.error, evidence=state.evidence)
        values = state.windows
        if app:
            values = tuple(item for item in values if matches_app(item.app_name, app) or matches_app(item.executable_path or "", app))
        return AppActionResult(True, ActionStatus.SUCCESS, AppAction.LIST_WINDOWS, target=app,
                               windows=values, verified=True, confidence=state.confidence,
                               evidence=(f"{len(values)} matching visible window(s)",))

    def active_window(self) -> AppActionResult:
        state = self.get_state()
        if not state.supported:
            return AppActionResult(False, ActionStatus.FAILED, AppAction.GET_ACTIVE_WINDOW,
                                   error=state.error, evidence=state.evidence)
        if not state.active_window:
            return AppActionResult(False, ActionStatus.NOT_FOUND, AppAction.GET_ACTIVE_WINDOW,
                                   error="No visible foreground window was found.", verified=True,
                                   evidence=state.evidence)
        window = state.active_window
        return AppActionResult(True, ActionStatus.SUCCESS, AppAction.GET_ACTIVE_WINDOW,
                               target=window.app_name, app=self._identity_from_window(window),
                               windows=(window,), verified=True, confidence=window.confidence,
                               evidence=window.evidence)

    def find_app(self, query: str) -> AppIdentity | None:
        visible = self.list_windows(query)
        if visible.success and visible.windows:
            return self._identity_from_window(next((item for item in visible.windows if item.active), visible.windows[0]))
        found = self.discovery.find(query)
        return replace(found, status=AppStatus.NOT_RUNNING) if found else None

    def _matching_windows(self, app: str) -> tuple[WindowInfo, ...]:
        result = self.list_windows(app)
        return result.windows if result.success else ()

    def _wait_for_window(self, app: str, previous_handles: set[int] | None = None) -> WindowInfo | None:
        previous_handles = previous_handles or set()
        for attempt in range(self.verify_attempts):
            values = self._matching_windows(app)
            fresh = next((item for item in values if item.window_handle not in previous_handles), None)
            if fresh or (values and not previous_handles):
                return fresh or values[0]
            if attempt + 1 < self.verify_attempts:
                self.sleep(self.verify_interval)
        return None

    def _wait_for_any_new_window(self, previous_handles: set[int]) -> tuple[WindowInfo, ...]:
        for attempt in range(self.verify_attempts):
            values = tuple(self.windows.list_windows())
            fresh = tuple(item for item in values if item.window_handle not in previous_handles)
            if fresh:
                return fresh
            if attempt + 1 < self.verify_attempts:
                self.sleep(self.verify_interval)
        return ()

    def focus_app(self, app: str) -> AppActionResult:
        windows = self._matching_windows(app)
        if not windows:
            return AppActionResult(False, ActionStatus.NOT_FOUND, AppAction.FOCUS, target=app,
                                   error=f"No visible window for '{app}' is running.", verified=True,
                                   next_step="Open the app first or check its name.")
        target = next((item for item in windows if item.active), windows[0])
        if target.active:
            return AppActionResult(True, ActionStatus.SUCCESS, AppAction.FOCUS, target=app,
                                   app=self._identity_from_window(target), windows=(target,),
                                   verified=True, confidence=0.99,
                                   evidence=("target window was already foreground",))
        verified = bool(self.windows.focus(target.window_handle))
        refreshed = self.windows.active_window() if verified else None
        verified = bool(refreshed and refreshed.window_handle == target.window_handle)
        return AppActionResult(verified, ActionStatus.SUCCESS if verified else ActionStatus.FAILED,
                               AppAction.FOCUS, target=app,
                               app=self._identity_from_window(refreshed or target),
                               windows=(refreshed or target,), verified=verified,
                               confidence=0.99 if verified else 0.2,
                               evidence=("foreground handle matches target",) if verified else (),
                               error=None if verified else "Windows did not report the target as foreground after the focus request.")

    def open_app(self, request: AppLaunchRequest) -> AppLaunchResult:
        raw = Path(request.app).expanduser()
        explicit_identity = None
        if raw.suffix.lower() in {".exe", ".com", ".bat", ".cmd", ".ps1", ".msi", ".msix"} and raw.exists():
            authorized = bool(
                request.confirmed and request.confirmation_id
                and self._consume_pending(request.confirmation_id, AppAction.OPEN, str(raw))
            )
            if not authorized:
                pending = self._create_pending(AppAction.OPEN, str(raw), DesktopRisk.HIGH,
                                               {"executable": str(raw)})
                return AppLaunchResult(False, ActionStatus.CONFIRMATION_REQUIRED, request,
                                       error="Opening an executable by path requires confirmation.",
                                       requires_confirmation=True, confirmation=pending)
            known = resolve_known_app(raw.name)
            canonical = known.canonical_name if known else raw.stem
            explicit_identity = AppIdentity(
                canonical, canonical, executable_path=str(raw.resolve()), status=AppStatus.NOT_RUNNING,
                safe_actions=("focus",), risky_actions=("close", "kill_process"),
                confidence=0.7, evidence=("exact executable path confirmed by user",),
                source="confirmed_executable", aliases=known.aliases if known else (),
            )
        existing = self._matching_windows(request.app) if request.focus_existing and not request.file_path else ()
        if existing:
            focused = self.focus_app(request.app)
            return AppLaunchResult(focused.success, focused.status, request, focused.app,
                                   focused_existing=True, launched=False, verified=focused.verified,
                                   confidence=focused.confidence, evidence=focused.evidence, error=focused.error)
        identity = explicit_identity or self.discovery.find(request.app)
        if not identity or not identity.executable_path:
            error, fixes = self.discovery.explain_not_found(request.app)
            return AppLaunchResult(False, ActionStatus.NOT_FOUND, request, error=error, suggested_fixes=fixes)
        previous = {item.window_handle for item in self._matching_windows(identity.canonical_name)}
        command = [identity.executable_path]
        if Path(identity.executable_path).name.lower() == "update.exe" and identity.canonical_name == "Discord":
            command.extend(["--processStart", "Discord.exe"])
        command.extend(request.arguments)
        if request.file_path:
            command.append(request.file_path)
        try:
            process = self.launcher(command, request.working_directory)
        except Exception as exc:
            return AppLaunchResult(False, ActionStatus.FAILED, request, identity,
                                   error=f"Failed to start {identity.canonical_name}: {exc}",
                                   evidence=(f"executable: {identity.executable_path}",))
        window = self._wait_for_window(identity.canonical_name, previous) if request.verify else None
        if window:
            running = self._identity_from_window(window)
            return AppLaunchResult(True, ActionStatus.SUCCESS, request, running, launched=True,
                                   verified=True, confidence=window.confidence,
                                   evidence=(f"launcher accepted executable: {identity.executable_path}",
                                             f"matching visible window handle: {window.window_handle}"))
        pid = getattr(process, "pid", None)
        return AppLaunchResult(False, ActionStatus.UNCERTAIN, request,
                               replace(identity, process_id=pid, status=AppStatus.UNKNOWN),
                               launched=True, verified=False, confidence=0.35,
                               evidence=(f"launcher accepted executable: {identity.executable_path}",
                                         f"reported process id: {pid}" if pid else "no process id reported"),
                               error=f"{identity.canonical_name} was started, but no matching visible window could be verified.",
                               suggested_fixes=("Wait and check desktop state again.", "Check whether the app opened in the background or displayed an error."))

    def _window_action(self, action: AppAction, app: str | None = None, window_handle: int | None = None) -> AppActionResult:
        values = tuple(self.windows.list_windows()) if getattr(self.windows, "supported", False) else ()
        target = next((item for item in values if window_handle and item.window_handle == window_handle), None)
        if target is None and app:
            matches = tuple(item for item in values if matches_app(item.app_name, app) or matches_app(item.executable_path or "", app))
            target = next((item for item in matches if item.active), matches[0] if matches else None)
        if target is None:
            return AppActionResult(False, ActionStatus.NOT_FOUND, action, target=app or str(window_handle),
                                   error="No matching visible window was found.", verified=True)
        method = getattr(self.windows, action.value)
        verified = bool(method(target.window_handle))
        refreshed = next(
            (item for item in self.windows.list_windows() if item.window_handle == target.window_handle),
            target,
        ) if verified else target
        return AppActionResult(verified, ActionStatus.SUCCESS if verified else ActionStatus.FAILED,
                               action, target=app or str(target.window_handle),
                               app=self._identity_from_window(refreshed), windows=(refreshed,),
                               verified=verified, confidence=0.98 if verified else 0.2,
                               evidence=(f"Win32 {action.value} state verified",) if verified else (),
                               error=None if verified else f"Windows did not verify the {action.value} request.")

    def minimize(self, app: str | None = None, window_handle: int | None = None) -> AppActionResult:
        return self._window_action(AppAction.MINIMIZE, app, window_handle)

    def maximize(self, app: str | None = None, window_handle: int | None = None) -> AppActionResult:
        return self._window_action(AppAction.MAXIMIZE, app, window_handle)

    def restore(self, app: str | None = None, window_handle: int | None = None) -> AppActionResult:
        return self._window_action(AppAction.RESTORE, app, window_handle)

    def open_folder(self, path: str) -> AppActionResult:
        target = Path(path).expanduser().resolve()
        if not target.is_dir():
            return AppActionResult(False, ActionStatus.NOT_FOUND, AppAction.OPEN_FOLDER,
                                   target=str(target), error="Folder does not exist.", verified=True)
        return self._launch_explorer([str(target)], AppAction.OPEN_FOLDER, str(target))

    def show_in_folder(self, path: str) -> AppActionResult:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            return AppActionResult(False, ActionStatus.NOT_FOUND, AppAction.SHOW_IN_FOLDER,
                                   target=str(target), error="File or folder does not exist.", verified=True)
        if target.is_dir():
            return self.open_folder(str(target))
        return self._launch_explorer(["/select,", str(target)], AppAction.SHOW_IN_FOLDER, str(target))

    def _launch_explorer(self, arguments: list[str], action: AppAction, target: str) -> AppActionResult:
        before = {item.window_handle for item in self._matching_windows("File Explorer")}
        try:
            self.launcher(["explorer.exe", *arguments], None)
        except Exception as exc:
            return AppActionResult(False, ActionStatus.FAILED, action, target=target,
                                   error=f"File Explorer launch failed: {exc}")
        window = self._wait_for_window("File Explorer", before)
        if window:
            return AppActionResult(True, ActionStatus.SUCCESS, action, target=target,
                                   app=self._identity_from_window(window), windows=(window,),
                                   verified=True, confidence=window.confidence,
                                   evidence=(f"Explorer window handle: {window.window_handle}",))
        return AppActionResult(False, ActionStatus.UNCERTAIN, action, target=target,
                               error="Explorer accepted the request, but its window could not be verified.",
                               evidence=("explorer.exe launch request returned",), confidence=0.35)

    def open_file(self, path: str, app: str | None = None, *, confirmation_id: str | None = None, confirm: bool = False) -> AppActionResult:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            return AppActionResult(False, ActionStatus.NOT_FOUND, AppAction.OPEN_FILE,
                                   target=str(target), error="File does not exist.", verified=True)
        if target.is_dir():
            return self.open_folder(str(target))
        choice = choose_app_for_path(target)
        if choice.requires_confirmation:
            if not (confirmation_id and confirm and self._consume_pending(confirmation_id, AppAction.OPEN_FILE, str(target))):
                pending = self._create_pending(AppAction.OPEN_FILE, str(target), choice.risk,
                                               {"path": str(target), "app": app or choice.app})
                return AppActionResult(False, ActionStatus.CONFIRMATION_REQUIRED, AppAction.OPEN_FILE,
                                       target=str(target), error=choice.reason,
                                       requires_confirmation=True, confirmation=pending,
                                       next_step="Confirm this exact file-open plan before execution.")
        selected_app = app or choice.app
        if choice.use_shell_default and not app:
            before = {item.window_handle for item in self.windows.list_windows()} if getattr(self.windows, "supported", False) else set()
            try:
                self.shell_open(str(target))
            except Exception as exc:
                return AppActionResult(False, ActionStatus.FAILED, AppAction.OPEN_FILE,
                                       target=str(target), error=f"Windows file association failed: {exc}")
            after = self._wait_for_any_new_window(before) if getattr(self.windows, "supported", False) else ()
            if after:
                return AppActionResult(True, ActionStatus.SUCCESS, AppAction.OPEN_FILE,
                                       target=str(target), app=self._identity_from_window(after[0]), windows=after,
                                       verified=True, confidence=after[0].confidence,
                                       evidence=(choice.reason, "new visible window appeared"))
            return AppActionResult(False, ActionStatus.UNCERTAIN, AppAction.OPEN_FILE,
                                   target=str(target), verified=False, confidence=0.35,
                                   evidence=(choice.reason, "Windows accepted the file association request"),
                                   error="The file-open request was sent, but no resulting window could be verified.")
        launch = self.open_app(AppLaunchRequest(selected_app, file_path=str(target), focus_existing=False))
        return AppActionResult(launch.success, launch.status, AppAction.OPEN_FILE,
                               target=str(target), app=launch.app, verified=launch.verified,
                               confidence=launch.confidence, evidence=(choice.reason, *launch.evidence),
                               error=launch.error, requires_confirmation=launch.requires_confirmation,
                               confirmation=launch.confirmation)

    def plan(self, request: str, *, target_path: str | None = None) -> DesktopActionPlan:
        return self.planner.plan(request, target_path=target_path)

    def _create_pending(self, action: AppAction, target: str, risk: DesktopRisk, details: dict) -> dict:
        token = uuid.uuid4().hex
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        self._pending[token] = {"action": action, "target": target, "details": details, "expires": expires}
        return {
            "confirmation_id": token, "action": action.value, "target": target,
            "risk": risk.value, "warning": "Unsaved work may be lost. Only this exact target will be affected.",
            "expires_at": expires.isoformat(),
        }

    def _consume_pending(self, token: str, action: AppAction, target: str | None = None) -> dict | None:
        pending = self._pending.get(token)
        if not pending or pending["expires"] < datetime.now(timezone.utc):
            self._pending.pop(token, None)
            return None
        if pending["action"] != action or (target is not None and pending["target"] != target):
            return None
        return self._pending.pop(token)

    def close_plan(self, app: str, *, close_all: bool = False) -> DesktopActionPlan:
        windows = self._matching_windows(app)
        if not windows:
            return DesktopActionPlan(f"close {app}", AppAction.CLOSE_ALL if close_all else AppAction.CLOSE,
                                     target_app=app, risk=DesktopRisk.HIGH, confirmation_needed=True,
                                     missing_target=True, expected_result="no action",
                                     fallback_strategy=("Check whether the app is running under another name.",),
                                     verification_method="no window was selected",
                                     rationale=("No matching visible window was found.",))
        selected = windows if close_all else (next((item for item in windows if item.active), windows[0]),)
        handles = tuple(item.window_handle for item in selected)
        pending = self._create_pending(AppAction.CLOSE_ALL if close_all else AppAction.CLOSE, app,
                                       DesktopRisk.HIGH, {"handles": handles, "app": app})
        return DesktopActionPlan(f"close {app}", AppAction.CLOSE_ALL if close_all else AppAction.CLOSE,
                                 target_app=app, target_window=handles[0], risk=DesktopRisk.HIGH,
                                 confirmation_needed=True, expected_result=f"close {len(handles)} selected window(s) normally",
                                 fallback_strategy=("If the app refuses to close, report that; do not kill it automatically.",),
                                 verification_method="confirm the selected window handles disappear",
                                 rationale=("Unsaved work cannot be detected reliably.",),
                                 confirmation_id=pending["confirmation_id"], expires_at=pending["expires_at"])

    def close_confirmed(self, confirmation_id: str, *, confirm: bool = False) -> AppActionResult:
        pending = self._pending.get(confirmation_id)
        if not pending or pending["action"] not in {AppAction.CLOSE, AppAction.CLOSE_ALL} or not confirm:
            return AppActionResult(False, ActionStatus.CONFIRMATION_REQUIRED, AppAction.CLOSE,
                                   error="A valid, unexpired close plan and explicit confirmation are required.",
                                   requires_confirmation=True)
        pending = self._consume_pending(confirmation_id, pending["action"], pending["target"])
        handles = tuple(pending["details"]["handles"])
        requested = [handle for handle in handles if self.windows.close(handle)]
        if len(requested) != len(handles):
            return AppActionResult(False, ActionStatus.FAILED, pending["action"], target=pending["target"],
                                   error="Windows rejected one or more normal close requests.", verified=False)
        remaining = handles
        for attempt in range(self.verify_attempts):
            remaining = tuple(handle for handle in handles if self.windows.window_exists(handle))
            if not remaining:
                break
            if attempt + 1 < self.verify_attempts:
                self.sleep(self.verify_interval)
        if remaining:
            return AppActionResult(False, ActionStatus.UNCERTAIN, pending["action"], target=pending["target"],
                                   error="The close request was sent, but one or more windows remain open (possibly an unsaved-work prompt).",
                                   verified=False, confidence=0.4,
                                   evidence=(f"remaining window handles: {remaining}",),
                                   next_step="Check the app for a save/discard prompt; no process was killed.")
        return AppActionResult(True, ActionStatus.SUCCESS, pending["action"], target=pending["target"],
                               verified=True, confidence=0.99,
                               evidence=(f"closed window handles: {handles}",))

    def kill_plan(self, app: str | None = None, *, process_id: int | None = None) -> DesktopActionPlan:
        windows = self._matching_windows(app) if app else ()
        selected_window = next((item for item in windows if item.active and item.process_id), None)
        selected_window = selected_window or next((item for item in windows if item.process_id), None)
        pid = process_id or (selected_window.process_id if selected_window else None)
        name = app or (next((item.app_name for item in windows), None))
        if not pid:
            return DesktopActionPlan(f"kill {app or process_id or ''}".strip(), AppAction.KILL_PROCESS,
                                     target_app=name, target_process_id=process_id,
                                     risk=DesktopRisk.HIGH, confirmation_needed=True, missing_target=True,
                                     expected_result="no action", fallback_strategy=("Provide a running app name or exact PID.",),
                                     verification_method="no process was selected",
                                     rationale=("No matching PID was found.",))
        pending = self._create_pending(AppAction.KILL_PROCESS, str(pid), DesktopRisk.HIGH,
                                       {"process_id": pid, "app": name})
        return DesktopActionPlan(f"kill {name or pid}", AppAction.KILL_PROCESS, target_app=name,
                                 target_process_id=pid, risk=DesktopRisk.HIGH, confirmation_needed=True,
                                 expected_result="the exact confirmed PID is force-terminated",
                                 fallback_strategy=("If termination fails, report the OS error and stop.",),
                                 verification_method="process termination command returns success for exact PID",
                                 rationale=("Forced termination can lose unsaved work.",),
                                 confirmation_id=pending["confirmation_id"], expires_at=pending["expires_at"])

    def kill_confirmed(self, confirmation_id: str, *, confirm: bool = False) -> AppActionResult:
        pending = self._pending.get(confirmation_id)
        if not pending or pending["action"] != AppAction.KILL_PROCESS or not confirm:
            return AppActionResult(False, ActionStatus.CONFIRMATION_REQUIRED, AppAction.KILL_PROCESS,
                                   error="A valid, unexpired kill plan and explicit confirmation are required.",
                                   requires_confirmation=True)
        pending = self._consume_pending(confirmation_id, AppAction.KILL_PROCESS, pending["target"])
        pid = int(pending["details"]["process_id"])
        try:
            success = bool(self.process_killer(pid))
        except Exception as exc:
            return AppActionResult(False, ActionStatus.FAILED, AppAction.KILL_PROCESS,
                                   target=str(pid), error=f"Process termination failed: {exc}")
        return AppActionResult(success, ActionStatus.SUCCESS if success else ActionStatus.FAILED,
                               AppAction.KILL_PROCESS, target=str(pid), verified=success,
                               confidence=0.98 if success else 0.1,
                               evidence=(f"termination command succeeded for PID {pid}",) if success else (),
                               error=None if success else f"Windows did not confirm termination of PID {pid}.")


_default_service: DesktopControlService | None = None


def get_desktop_service() -> DesktopControlService:
    global _default_service
    if _default_service is None:
        _default_service = DesktopControlService()
    return _default_service


def set_desktop_service(service: DesktopControlService | None) -> None:
    """Test hook; normal code uses the process-wide service."""
    global _default_service
    _default_service = service
