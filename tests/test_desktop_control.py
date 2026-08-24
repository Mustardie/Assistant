from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.jarvis_widget_backend import JarvisWidgetBackend
from brain.intent_router import Intent, IntentRouter
from tools.app_catalog import resolve_known_app
from tools.app_discovery import WindowsAppDiscovery
from tools.desktop_control import DesktopControlService, set_desktop_service
from tools.desktop_file_intent import choose_app_for_path
from tools.desktop_models import (
    ActionStatus, AppAction, AppIdentity, AppLaunchRequest, AppStatus,
    DesktopRisk, WindowInfo,
)
from tools.desktop_planner import DesktopActionPlanner


def _window(app="Discord", handle=10, pid=100, active=False, minimized=False, maximized=False):
    executable = {
        "Discord": r"C:\Apps\Discord.exe",
        "Visual Studio Code": r"C:\Apps\Code.exe",
        "File Explorer": r"C:\Windows\explorer.exe",
        "Spotify": r"C:\Apps\Spotify.exe",
        "Minecraft Launcher": r"C:\Apps\MinecraftLauncher.exe",
    }.get(app, rf"C:\Apps\{app}.exe")
    return WindowInfo(
        app_name=app, executable_path=executable, process_id=pid,
        window_title=f"{app} window", window_handle=handle,
        active=active, focused=active, minimized=minimized, maximized=maximized,
        confidence=0.98, evidence=("fake Win32 evidence",),
    )


class FakeWindows:
    supported = True

    def __init__(self, windows=(), *, close_removes=True):
        self.values = list(windows)
        self.close_removes = close_removes

    def list_windows(self):
        return list(self.values)

    def active_window(self):
        return next((item for item in self.values if item.active), None)

    def focus(self, handle):
        found = False
        updated = []
        for item in self.values:
            active = item.window_handle == handle
            found = found or active
            updated.append(replace(item, active=active, focused=active, minimized=False if active else item.minimized))
        self.values = updated
        return found

    def minimize(self, handle):
        return self._state(handle, minimized=True, maximized=False)

    def maximize(self, handle):
        return self._state(handle, minimized=False, maximized=True)

    def restore(self, handle):
        return self._state(handle, minimized=False, maximized=False)

    def _state(self, handle, **changes):
        for index, item in enumerate(self.values):
            if item.window_handle == handle:
                self.values[index] = replace(item, **changes)
                return True
        return False

    def close(self, handle):
        if not any(item.window_handle == handle for item in self.values):
            return False
        if self.close_removes:
            self.values = [item for item in self.values if item.window_handle != handle]
        return True

    def window_exists(self, handle):
        return any(item.window_handle == handle for item in self.values)


class FakeDiscovery:
    def __init__(self, apps=()):
        self.apps = list(apps)

    def discover(self):
        return list(self.apps)

    def find(self, query):
        known = resolve_known_app(query)
        return next((item for item in self.apps if known and item.canonical_name == known.canonical_name), None)

    def explain_not_found(self, query):
        return f"not found: {query}", ("configure an alias",)


def _app(name, path):
    return AppIdentity(name, name, executable_path=str(path), status=AppStatus.NOT_RUNNING,
                       safe_actions=("open", "focus"), risky_actions=("close", "kill_process"),
                       confidence=0.9, evidence=("fake discovery",), source="test")


def test_app_identity_schema_serializes_enums_and_evidence():
    value = _app("Discord", r"C:\Apps\Discord.exe").to_dict()
    assert value["status"] == "not_running"
    assert value["safe_actions"] == ["open", "focus"]
    assert value["evidence"] == ["fake discovery"]


def test_aliases_resolve_to_canonical_apps():
    assert resolve_known_app("chrome").canonical_name == "Google Chrome"
    assert resolve_known_app("mc launcher").canonical_name == "Minecraft Launcher"
    assert resolve_known_app("code").canonical_name == "Visual Studio Code"


def test_discovery_uses_fake_start_menu_shortcuts(tmp_path):
    root = tmp_path / "Start Menu"
    root.mkdir()
    shortcut = root / "Discord.lnk"
    shortcut.write_text("fake", encoding="utf-8")
    executable = tmp_path / "Discord.exe"
    executable.write_text("fake", encoding="utf-8")
    discovery = WindowsAppDiscovery(
        env={}, shortcut_roots=[root],
        shortcut_resolver=lambda path: str(executable) if path == shortcut else None,
        path_lookup=lambda _: None, include_registry=False,
    )
    found = discovery.find("discord")
    assert found.canonical_name == "Discord"
    assert found.source == "start_menu"
    assert "Start Menu shortcut" in found.evidence[0]


def test_active_window_and_listing_are_normalized_from_mock_backend():
    windows = FakeWindows([_window("Discord", active=True), _window("Spotify", handle=11, pid=101)])
    service = DesktopControlService(discovery=FakeDiscovery(), windows=windows, verify_attempts=1)
    active = service.active_window()
    listed = service.list_windows("spotify")
    assert active.success and active.app.canonical_name == "Discord"
    assert listed.success and listed.windows[0].app_name == "Spotify"


def test_open_focuses_existing_app_instead_of_duplicate_launch():
    launches = []
    windows = FakeWindows([_window("Discord")])
    service = DesktopControlService(
        discovery=FakeDiscovery(), windows=windows,
        launcher=lambda command, cwd: launches.append((command, cwd)), verify_attempts=1,
    )
    result = service.open_app(AppLaunchRequest("discord"))
    assert result.success and result.focused_existing and result.verified
    assert launches == []
    assert windows.active_window().app_name == "Discord"


def test_open_app_reports_verified_success_and_honest_uncertainty(tmp_path):
    executable = tmp_path / "Discord.exe"
    executable.write_text("fake", encoding="utf-8")
    identity = _app("Discord", executable)

    verified_windows = FakeWindows()
    def verified_launch(command, cwd):
        verified_windows.values.append(_window("Discord", active=True))
        return type("Process", (), {"pid": 100})()
    verified = DesktopControlService(discovery=FakeDiscovery([identity]), windows=verified_windows,
                                     launcher=verified_launch, verify_attempts=1)
    assert verified.open_app(AppLaunchRequest("discord")).success

    uncertain = DesktopControlService(discovery=FakeDiscovery([identity]), windows=FakeWindows(),
                                      launcher=lambda command, cwd: type("Process", (), {"pid": 222})(), verify_attempts=1)
    result = uncertain.open_app(AppLaunchRequest("discord"))
    assert not result.success and result.status == ActionStatus.UNCERTAIN
    assert result.launched and not result.verified


def test_open_app_failure_is_not_reported_as_success(tmp_path):
    executable = tmp_path / "Code.exe"
    executable.write_text("fake", encoding="utf-8")
    service = DesktopControlService(
        discovery=FakeDiscovery([_app("Visual Studio Code", executable)]), windows=FakeWindows(),
        launcher=lambda command, cwd: (_ for _ in ()).throw(OSError("blocked")), verify_attempts=1,
    )
    result = service.open_app(AppLaunchRequest("code"))
    assert not result.success and result.status == ActionStatus.FAILED
    assert "blocked" in result.error


def test_unknown_executable_path_requires_target_bound_confirmation(tmp_path):
    executable = tmp_path / "custom-tool.exe"
    executable.write_text("fake", encoding="utf-8")
    windows = FakeWindows()
    launches = []

    def launch(command, cwd):
        launches.append(command)
        windows.values.append(_window("custom-tool", active=True))
        return type("Process", (), {"pid": 456})()

    service = DesktopControlService(discovery=FakeDiscovery(), windows=windows,
                                    launcher=launch, verify_attempts=1)
    pending = service.open_app(AppLaunchRequest(str(executable)))
    assert pending.requires_confirmation and launches == []
    token = pending.confirmation["confirmation_id"]
    confirmed = service.open_app(AppLaunchRequest(str(executable), confirmation_id=token, confirmed=True))
    assert confirmed.success and confirmed.verified
    assert launches == [[str(executable.resolve())]]


def test_file_to_app_selection_and_executable_confirmation(tmp_path):
    assert choose_app_for_path(tmp_path / "lesson.pdf").app == "Microsoft Edge"
    assert choose_app_for_path(tmp_path / "scene.blend").app == "Blender"
    assert choose_app_for_path(tmp_path / "edit.prproj").app == "Adobe Premiere Pro"
    assert choose_app_for_path(tmp_path / "main.py").app == "Visual Studio Code"
    installer = tmp_path / "setup.exe"
    installer.write_text("fake", encoding="utf-8")
    service = DesktopControlService(discovery=FakeDiscovery(), windows=FakeWindows(), verify_attempts=1)
    result = service.open_file(str(installer))
    assert not result.success and result.requires_confirmation
    assert result.status == ActionStatus.CONFIRMATION_REQUIRED
    assert result.confirmation["risk"] == "high"


def test_close_plan_requires_bound_confirmation_and_does_not_kill():
    windows = FakeWindows([_window("Spotify", active=True)])
    service = DesktopControlService(discovery=FakeDiscovery(), windows=windows, verify_attempts=1)
    plan = service.close_plan("spotify")
    assert plan.confirmation_needed and plan.confirmation_id
    rejected = service.close_confirmed(plan.confirmation_id, confirm=False)
    assert rejected.requires_confirmation
    assert windows.window_exists(10)
    closed = service.close_confirmed(plan.confirmation_id, confirm=True)
    assert closed.success and closed.verified
    assert not windows.window_exists(10)


def test_close_reports_unsaved_prompt_uncertainty_without_force_kill():
    windows = FakeWindows([_window("Spotify")], close_removes=False)
    service = DesktopControlService(discovery=FakeDiscovery(), windows=windows, verify_attempts=1)
    plan = service.close_plan("spotify")
    result = service.close_confirmed(plan.confirmation_id, confirm=True)
    assert result.status == ActionStatus.UNCERTAIN
    assert "unsaved-work prompt" in result.error


def test_kill_process_requires_plan_and_exact_confirmation():
    killed = []
    service = DesktopControlService(
        discovery=FakeDiscovery(), windows=FakeWindows([_window("Minecraft Launcher", pid=987)]),
        process_killer=lambda pid: killed.append(pid) or True, verify_attempts=1,
    )
    plan = service.kill_plan("minecraft")
    assert plan.target_process_id == 987 and plan.confirmation_id
    assert not service.kill_confirmed("wrong", confirm=True).success
    result = service.kill_confirmed(plan.confirmation_id, confirm=True)
    assert result.success and killed == [987]


def test_desktop_planner_selects_actions_risk_and_missing_targets(tmp_path):
    planner = DesktopActionPlanner()
    assert planner.plan("open Discord").intended_action == AppAction.OPEN
    assert planner.plan("focus WhatsApp").intended_action == AppAction.FOCUS
    assert planner.plan("what app am I using?").intended_action == AppAction.GET_ACTIVE_WINDOW
    close = planner.plan("close Spotify")
    assert close.intended_action == AppAction.CLOSE and close.confirmation_needed
    kill = planner.plan("kill Minecraft, it froze")
    assert kill.risk == DesktopRisk.HIGH and kill.confirmation_needed
    missing = planner.plan("focus the app")
    assert missing.missing_target
    reveal = planner.plan("show this file in folder")
    assert reveal.intended_action == AppAction.SHOW_IN_FOLDER and reveal.missing_target


def test_assistant_router_prefers_desktop_control_for_app_requests():
    router = IntentRouter()
    for request in ("open Discord", "focus WhatsApp", "what app am I using?", "close Spotify"):
        route = router.route(request)
        assert route.intent == Intent.APP_CONTROL
        assert route.likely_required_tools == ["desktop_plan"]
    assert router.route("open the latest render").intent == Intent.LOCAL_TASK


def test_tool_registry_contains_structured_desktop_contracts(monkeypatch):
    from tools.tool_registry import TOOLS, desktop_get_state
    required = {
        "desktop_get_state", "desktop_active_window", "desktop_list_windows", "app_find",
        "app_open", "app_focus", "app_open_file", "app_show_in_folder", "app_minimize",
        "app_maximize", "app_restore", "app_close_plan", "app_close_confirmed",
        "process_kill_plan", "process_kill_confirmed",
    }
    assert required <= set(TOOLS)
    service = DesktopControlService(discovery=FakeDiscovery(), windows=FakeWindows([_window(active=True)]), verify_attempts=1)
    set_desktop_service(service)
    try:
        state = desktop_get_state()
        assert state["supported"] and state["active_window"]["app_name"] == "Discord"
    finally:
        set_desktop_service(None)


def test_widget_backend_returns_structured_app_action_and_desktop_state(tmp_path):
    windows = FakeWindows([_window("Discord", active=True)])
    service = DesktopControlService(discovery=FakeDiscovery(), windows=windows, verify_attempts=1)
    backend = JarvisWidgetBackend(desktop_service=service)
    backend.activity_file = tmp_path / "activity.json"
    launched = backend.perform("app_launcher", "launch", {"query": "discord"})
    status = backend.perform("system_status", "refresh", {}, {"model": "test"})
    assert launched["success"]
    assert launched["data"]["app_action"]["verified"]
    assert launched["data"]["target_app"] == "discord"
    assert status["data"]["active_app"] == "Discord"
    assert status["data"]["desktop"] == "Ready"
