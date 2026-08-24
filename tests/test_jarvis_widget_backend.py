from pathlib import Path

from app.jarvis_widget_backend import JarvisWidgetBackend
from ui.jarvis.registry import build_default_registry
from ui.jarvis.widget_contents import _CONTENT


def _backend(tmp_path):
    backend = JarvisWidgetBackend()
    backend.notes_file = tmp_path / "notes.json"
    backend.reminders_file = tmp_path / "reminders.json"
    backend.calendar_file = tmp_path / "calendar.json"
    backend.activity_file = tmp_path / "activity.json"
    return backend


def test_every_visible_catalog_widget_has_real_content_class():
    missing = {
        spec.widget_type
        for spec in build_default_registry().all()
        if spec.widget_type != "settings" and spec.widget_type not in _CONTENT
    }
    assert missing == set()
    assert all(spec.implemented for spec in build_default_registry().all())


def test_reminders_create_refresh_and_complete(tmp_path):
    backend = _backend(tmp_path)
    created = backend.perform("reminders", "create", {"text": "Submit assignment"})
    assert created["success"] is True
    assert created["data"]["items"][0]["status"] == "open"
    selected = created["data"]["items"][0]
    completed = backend.perform("reminders", "complete", {"selected": selected})
    assert completed["data"]["items"][0]["status"] == "complete"


def test_notes_save_and_reload_latest_text(tmp_path):
    backend = _backend(tmp_path)
    saved = backend.perform("notes", "save", {"text": "JARVIS widget wiring"})
    assert saved["success"] is True
    loaded = backend.perform("notes", "refresh")
    assert loaded["data"]["text"] == "JARVIS widget wiring"


def test_calendar_requires_confirmation_before_adding_local_event(tmp_path):
    backend = _backend(tmp_path)
    pending = backend.perform("calendar", "create", {"text": "Physics study session at 7"})
    assert pending["confirmation"]["action"] == "Create calendar event"
    assert not backend.calendar_file.exists()
    result = backend.perform("calendar", "create", {"text": "Physics study session at 7", "confirm": True})
    assert result["success"] is True
    assert result["data"]["events"][0]["title"] == "Physics study session at 7"


def test_terminal_requires_confirmation_but_explicit_message_send_does_not(tmp_path):
    backend = _backend(tmp_path)
    terminal = backend.perform("terminal", "run", {"text": "echo hello"})
    message = backend.perform("messaging", "send", {"text": "hello"})
    assert terminal["confirmation"]["action"] == "Run command"
    assert terminal["pending_prompt"].startswith("Run this command only after confirmation")
    assert "confirmation" not in message
    assert "Do not ask for a second confirmation" in message["prompt"]


def test_reasoning_widgets_return_agent_prompts(tmp_path):
    backend = _backend(tmp_path)
    answer = backend.perform("quick_answer", "ask", {"text": "2 + 2"})
    study = backend.perform("study", "quiz", {"text": "photosynthesis"})
    assert answer["prompt"] == "Answer concisely: 2 + 2"
    assert "quiz me" in study["prompt"]


def test_weather_widget_uses_normalized_live_adapter(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "tools.weather.current_weather",
        lambda location: {"success": True, "connected": True, "location": location, "temperature": "24", "forecast": ["Tomorrow 23°C"]},
    )
    result = _backend(tmp_path).perform("weather", "refresh", {"location": "Delhi"})
    assert result["success"] is True
    assert result["data"]["connected"] is True
    assert result["data"]["temperature"] == "24"


def test_system_monitor_returns_real_local_metrics(tmp_path):
    result = _backend(tmp_path).perform("system_monitor", "refresh")
    assert result["success"] is True
    assert result["data"]["metrics"]
