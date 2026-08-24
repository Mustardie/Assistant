from __future__ import annotations

import ast
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.jarvis_widget_backend import JarvisWidgetBackend
from brain.intent_router import Intent, IntentRouter
from memory.desktop_context_store import DesktopContextStore
from tools.desktop_context import DesktopContextService, safe_window_title
from tools.desktop_context_models import (
    DesktopContext, DesktopContextSnapshot, DesktopEvent, DesktopEventType,
    MonitoringState, PrivacyMode, WindowContext, WorkMode,
)
from tools.desktop_habits import ContextPredictor, HabitLearner, SuggestionEngine
from tools.desktop_models import AppStatus, DesktopState, WindowInfo
from tools.desktop_startup import DesktopStartupManager
from tools.contracts import ToolDecisionLayer


def _window(app="Visual Studio Code", title="secret.py - Assistant", *, active=True):
    return WindowInfo(app, rf"C:\Apps\{app}.exe", 42, title, 99,
                      AppStatus.RUNNING, active, active, False, False,
                      evidence=("fake window",))


class FakeWindowBackend:
    supported = True

    def __init__(self, idle=3):
        self.idle = idle

    def idle_seconds(self):
        return self.idle


class FakeDesktop:
    def __init__(self, windows=None, idle=3):
        self.values = list(windows or [_window()])
        self.windows = FakeWindowBackend(idle)

    def get_state(self):
        active = next((item for item in self.values if item.active), None)
        return DesktopState(True, "Windows", active, tuple(self.values),
                            confidence=0.98, evidence=("fake Win32 snapshot",))


def _service(tmp_path, *, windows=None, idle=3):
    store = DesktopContextStore(tmp_path / "desktop-context.json", event_limit=20)
    return DesktopContextService(store=store, desktop_service=FakeDesktop(windows, idle),
                                 poll_interval_seconds=0.05,
                                 widget_provider=lambda: ["code_task"],
                                 task_provider=lambda: "coding in Assistant",
                                 connector_provider=lambda: {"discord": "disconnected"})


def _snapshot(*, apps=("Visual Studio Code", "Windows Terminal"), privacy=PrivacyMode.STANDARD,
              state=MonitoringState.RUNNING, idle=2):
    active = next(iter(apps), None)
    context = DesktopContext(active, WindowContext(active, "Assistant", False, True) if active else None,
                             tuple(apps), (), (), "coding", ("code_task",), {}, idle)
    return DesktopContextSnapshot(datetime.now(timezone.utc).isoformat(), context, state, privacy, 0.9, ("fake",))


def test_desktop_context_contracts_serialize_enums_and_evidence():
    value = _snapshot().to_dict()
    assert value["monitoring_state"] == "running"
    assert value["privacy_mode"] == "standard"
    assert value["context"]["active_window"]["active"] is True
    assert value["evidence"] == ["fake"]


def test_privacy_filter_redacts_private_apps_and_strict_mode():
    assert safe_window_title("Discord", "Private chat with Sam", PrivacyMode.STANDARD) == (None, True)
    assert safe_window_title("Visual Studio Code", "main.py - Assistant", PrivacyMode.STRICT) == (None, True)
    assert safe_window_title("Visual Studio Code", "main.py - Assistant", PrivacyMode.STANDARD)[0] == "main.py - Assistant"


def test_snapshot_is_safe_and_includes_structured_context(tmp_path):
    service = _service(tmp_path, windows=[_window("Discord", "Private chat with Sam")])
    value = service.capture_snapshot().to_dict()
    assert value["context"]["active_app"] == "Discord"
    assert value["context"]["active_window"]["title"] is None
    assert value["context"]["active_window"]["title_redacted"] is True
    assert value["context"]["widgets_open"] == ["code_task"]
    assert value["context"]["connector_status"] == {"discord": "disconnected"}


def test_command_event_normalization_never_stores_raw_prompt(tmp_path):
    service = _service(tmp_path)
    service.start(confirm=True)
    service.record_command("My password is hunter2; start coding the secret client")
    record = next(item for item in reversed(service.store.events()) if item["event_type"] == "jarvis_command")
    serialized = str(record).lower()
    assert "hunter2" not in serialized
    assert "secret client" not in serialized
    assert record["metadata"]["intent_terms"] == ["coding"]
    service.stop(confirm=True)


def test_monitoring_off_does_not_collect_activity(tmp_path):
    service = _service(tmp_path)
    result = service.record_command("start coding")
    assert result["stored"] is False
    assert service.store.events() == []


def test_monitor_requires_opt_in_and_supports_lifecycle(tmp_path):
    service = _service(tmp_path)
    assert service.start()["requires_confirmation"] is True
    assert service.start(confirm=True)["status"]["state"] == "running"
    assert service.pause()["status"]["state"] == "paused"
    assert service.resume()["status"]["state"] == "running"
    assert service.stop()["requires_confirmation"] is True
    assert service.stop(confirm=True)["status"]["state"] == "disabled"
    assert service.store.config()["monitoring_enabled"] is False


def test_monitor_contains_poll_errors_instead_of_crashing(tmp_path):
    class BrokenDesktop:
        def get_state(self):
            raise OSError("fake Win32 failure")

    service = DesktopContextService(store=DesktopContextStore(tmp_path / "broken.json"),
                                    desktop_service=BrokenDesktop(), poll_interval_seconds=0.05)
    service.start(confirm=True)
    deadline = time.monotonic() + 1
    while service.status().state is not MonitoringState.ERROR and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.status().state is MonitoringState.ERROR
    assert "fake Win32 failure" in service.status().last_error
    service.stop(confirm=True)


def test_store_rotates_events_and_debug_is_aggregate_only(tmp_path):
    store = DesktopContextStore(tmp_path / "store.json", event_limit=20)
    for index in range(30):
        store.append_event(DesktopEvent(DesktopEventType.APP_ACTION, app_name=f"App {index}", summary="safe"))
    assert len(store.events()) == 20
    summary = store.debug_summary()
    assert summary["event_count"] == 20
    assert summary["raw_history_included"] is False
    assert "events" not in summary


def test_startup_enable_is_planned_confirmed_verified_and_reversible(tmp_path):
    manager = DesktopStartupManager(startup_dir=tmp_path / "Startup", python_executable=tmp_path / "python.exe",
                                    project_root=tmp_path / "project", platform_name="Windows")
    plan = manager.enable_plan()
    assert plan["plan"]["requires_confirmation"] is True
    assert manager.enable_confirmed(plan["plan"]["confirmation_id"])["requires_confirmation"] is True
    enabled = manager.enable_confirmed(manager.enable_plan()["plan"]["confirmation_id"], confirm=True)
    assert enabled["success"] and manager.status().enabled
    assert "--start-minimized" in manager.entry_path.read_text(encoding="utf-8")
    assert manager.disable()["requires_confirmation"] is True
    assert manager.disable(confirm=True)["success"] and not manager.status().enabled


def test_habit_learning_requires_repeated_separate_sessions():
    now = datetime.now(timezone.utc)
    events = []
    for offset in (0, 60):
        timestamp = (now + timedelta(minutes=offset)).isoformat()
        events.append({"event_type": "context_snapshot", "timestamp": timestamp,
                       "app_name": "Visual Studio Code",
                       "metadata": {"apps": ["Visual Studio Code", "Windows Terminal", "LM Studio"], "action": "open"}})
    patterns = HabitLearner().detect(events)
    assert len(patterns) == 1
    assert patterns[0].frequency == 2
    assert patterns[0].confirmation_required is True
    assert "LM Studio" in patterns[0].apps


def test_deleted_habit_stays_forgotten_and_habit_can_be_disabled(tmp_path):
    service = _service(tmp_path)
    service.store.update_config(monitoring_enabled=True)
    now = datetime.now(timezone.utc)
    for offset in (0, 60):
        service.store.append_event({"event_type": "context_snapshot", "timestamp": (now + timedelta(minutes=offset)).isoformat(),
                                    "app_name": "Visual Studio Code", "metadata": {"apps": ["Visual Studio Code", "LM Studio"]}})
    habit = service.learn_habits()[0]
    assert service.habit_disable(habit["id"])["disabled"] is True
    assert service.store.habits()[0]["auto_suggest_allowed"] is False
    assert service.habit_delete(habit["id"])["success"] is True
    assert service.learn_habits() == []


def test_mode_prediction_uses_safe_app_and_idle_evidence():
    coding = ContextPredictor().predict(_snapshot())
    idle = ContextPredictor().predict(_snapshot(idle=900))
    assert coding.mode is WorkMode.CODING and coding.confidence > 0.5
    assert "code_task" in coding.suggested_widgets
    assert idle.mode is WorkMode.IDLE


def test_suggestion_cooldown_dismissal_and_privacy_suppression(tmp_path):
    store = DesktopContextStore(tmp_path / "suggestions.json")
    engine = SuggestionEngine(store)
    prediction = ContextPredictor().predict(_snapshot())
    created = engine.generate(_snapshot(), prediction)
    assert len(created) == 1 and created[0].dismissible
    assert engine.generate(_snapshot(), prediction) == []
    engine.dismiss(created[0].id)
    prior = store.suggestions()[0]
    store.update_suggestion(prior["id"], dismiss_count=2,
                            created_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    assert engine.generate(_snapshot(), prediction) == []
    assert engine.generate(_snapshot(privacy=PrivacyMode.STRICT), prediction) == []


def test_gaming_suggestions_are_suppressed_by_default(tmp_path):
    store = DesktopContextStore(tmp_path / "gaming.json")
    snapshot = _snapshot(apps=("Minecraft Launcher", "OBS Studio"))
    prediction = ContextPredictor().predict(snapshot)
    assert prediction.mode is WorkMode.RECORDING
    assert SuggestionEngine(store).generate(snapshot, prediction) == []


def test_skill_plan_from_routine_is_never_saved_or_run():
    routine = {
        "id": "coding-1", "name": "Coding routine", "frequency": 3,
        "suggested_skill": "Start coding mode",
        "apps": ["Visual Studio Code", "Windows Terminal", "LM Studio"],
    }
    plan = HabitLearner.skill_plan(routine).to_dict()
    assert plan["saved"] is False
    assert plan["requires_approval_to_save"] is True
    assert plan["requires_approval_to_run"] is True
    assert [step["tool"] for step in plan["steps"][:3]] == ["app_open"] * 3


def test_context_widget_backend_returns_structured_results(tmp_path):
    service = _service(tmp_path)
    backend = JarvisWidgetBackend(context_service=service)
    backend.activity_file = tmp_path / "activity.json"
    context = backend.perform("desktop_context", "refresh")
    mode = backend.perform("current_mode", "refresh")
    monitoring = backend.perform("privacy_monitoring", "refresh")
    assert context["success"] and context["data"]["snapshot"]["privacy_mode"] == "standard"
    assert mode["data"]["prediction"]["mode"] == "coding"
    assert monitoring["data"]["status"]["state"] == "disabled"


def test_assistant_routes_background_habits_and_current_context():
    router = IntentRouter()
    assert router.route("Run in the background.").intent is Intent.DESKTOP_CONTEXT
    assert router.route("Start with Windows.").likely_required_tools == ["desktop_startup_enable_plan"]
    assert router.route("What do you think I'm working on?").likely_required_tools == ["desktop_context_snapshot", "desktop_mode_predict"]
    assert router.route("What habits did you learn?").likely_required_tools == ["desktop_habits_list"]
    assert router.route("Don't bother me while gaming.").likely_required_tools == ["desktop_gaming_suggestions_set"]


def test_registry_declares_all_desktop_context_tool_contracts_without_importing_optional_backends():
    source = Path("tools/tool_registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    mapping = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "TOOLS" for target in node.targets))
    registered = {key.value for key in mapping.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    expected = {
        "desktop_context_snapshot", "desktop_monitor_start", "desktop_monitor_stop",
        "desktop_monitor_pause", "desktop_monitor_resume", "desktop_monitor_status",
        "desktop_startup_status", "desktop_startup_enable_plan", "desktop_startup_enable_confirmed",
        "desktop_startup_disable", "desktop_habits_list", "desktop_habit_explain",
        "desktop_habit_disable", "desktop_habit_delete", "desktop_activity_list", "desktop_activity_clear",
        "desktop_mode_predict", "desktop_suggestions_list", "desktop_suggestion_accept",
        "desktop_suggestion_dismiss", "desktop_suggestion_type_disable",
        "desktop_gaming_suggestions_set", "desktop_privacy_set", "desktop_context_debug_summary",
        "desktop_prediction_mark_wrong", "desktop_create_skill_from_routine_plan",
    }
    assert expected <= registered


def test_persistent_and_destructive_context_tools_are_runtime_guarded():
    tools = {
        "desktop_monitor_start": lambda confirm=False: None,
        "desktop_habit_delete": lambda habit_id, confirm=False: None,
        "desktop_activity_clear": lambda confirm=False: None,
    }
    layer = ToolDecisionLayer(tools)
    assert layer.assess("desktop_monitor_start", {}).requires_confirmation is True
    assert layer.assess("desktop_habit_delete", {"habit_id": "routine-1"}).requires_confirmation is True
    assert layer.assess("desktop_activity_clear", {}, confirmed=True).allowed is True
