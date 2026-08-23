import json
from pathlib import Path

from ui.jarvis.events import JarvisEventBus, JarvisEventType
from ui.jarvis.manager import WidgetManager
from ui.jarvis.models import JarvisState
from ui.jarvis.registry import build_default_registry


def _manager(tmp_path):
    events = JarvisEventBus()
    return WidgetManager(
        build_default_registry(),
        event_bus=events,
        layout_path=tmp_path / "layout.json",
    ), events


def test_catalog_contains_requested_widget_types_and_honest_specs():
    registry = build_default_registry()
    assert len(registry.all()) >= 35
    assert registry.get("chat").implemented is True
    assert registry.get("weather").required_backend == "weather"
    assert "fetched on demand" in registry.get("weather").disabled_message.lower()
    assert registry.get("email").implemented is True
    assert registry.get("email").required_backend == "gmail"
    assert "reports exact failures" in registry.get("email").disabled_message.lower()


def test_state_aliases_normalize_without_rejecting_known_voice_states():
    assert JarvisState.normalize("transcribing") is JarvisState.THINKING
    assert JarvisState.normalize("tool") is JarvisState.EXECUTING_TOOL
    assert JarvisState.normalize(JarvisState.ERROR) is JarvisState.ERROR


def test_widget_lifecycle_geometry_and_persistence(tmp_path):
    manager, _ = _manager(tmp_path)
    state = manager.create("chat", position=(14, 22), size=(420, 310))
    manager.move(state.widget_id, 101, 87)
    manager.resize(state.widget_id, 510, 390)
    manager.toggle_collapsed(state.widget_id)
    manager.toggle_pinned(state.widget_id)

    payload = json.loads((tmp_path / "layout.json").read_text(encoding="utf-8"))
    assert payload["version"] == manager.LAYOUT_VERSION
    assert payload["widgets"][0]["x"] == 101

    restored, _ = _manager(tmp_path)
    values = restored.restore_layout()
    assert len(values) == 1
    recovered = values[0]
    assert (recovered.x, recovered.y) == (101, 87)
    assert (recovered.width, recovered.height) == (510, 390)
    assert recovered.collapsed is True
    assert recovered.pinned is True

    assert restored.close(recovered.widget_id) is True
    assert restored.all() == []


def test_widget_event_routing_creates_updates_focuses_and_closes(tmp_path):
    manager, events = _manager(tmp_path)
    events.publish(JarvisEventType.WIDGET_CREATE, {"widget_type": "weather", "data": {"connected": False}})
    weather = manager.find_type("weather")
    assert weather is not None
    events.publish(JarvisEventType.WIDGET_UPDATE, {"widget_id": weather.widget_id, "loading": True})
    assert manager.get(weather.widget_id).loading is True
    initial_z = weather.z_index
    events.publish(JarvisEventType.WIDGET_FOCUS, {"widget_id": weather.widget_id})
    assert manager.get(weather.widget_id).z_index > initial_z
    events.publish(JarvisEventType.WIDGET_CLOSE, {"widget_id": weather.widget_id})
    assert manager.get(weather.widget_id) is None


def test_singleton_widget_is_reused_instead_of_cluttering_workspace(tmp_path):
    manager, _ = _manager(tmp_path)
    first = manager.create("system_status", data={"mic": "checking"})
    second = manager.create("system_status", data={"mic": "ready"})
    assert first.widget_id == second.widget_id
    assert len(manager.all()) == 1
    assert second.data["mic"] == "ready"


def test_unpositioned_widgets_cascade_below_the_hud(tmp_path):
    manager, _ = _manager(tmp_path)
    first = manager.create("chat")
    second = manager.create("weather")
    assert first.y >= 92
    assert second.y > first.y
    assert (second.x, second.y) != (first.x, first.y)


def test_main_entry_uses_jarvis_by_default_with_legacy_escape_hatch():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'os.getenv("JARVIS_LEGACY_UI"' in source
    assert "window = NovaWindow() if _legacy_ui else JarvisWindow()" in source


def test_main_imports_runtime_settings_before_status_initialization():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    persisted_environment = source.index("_apply_persisted_settings_to_env()\n")
    import_position = source.index("from config.settings import (")
    settings_entry = source.index("    settings,", import_position)
    status_use = source.index("PROVIDER_MODEL_FIELD.get(settings.llm_provider)")
    assert persisted_environment < import_position < settings_entry < status_use
