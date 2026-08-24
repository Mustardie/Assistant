import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from ui.jarvis.models import WidgetState
from ui.jarvis.registry import build_default_registry
from ui.jarvis.widget_contents import ChatWidget, ConfirmationWidget, VideoPlayerWidget, WeatherWidget
from ui.jarvis.widget_contents import create_widget_content
from ui.jarvis.window import JarvisWindow
from ui.jarvis.settings_view import JarvisSettingsView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _state(widget_type, **data):
    spec = build_default_registry().get(widget_type)
    return spec, WidgetState.create(spec, data=data)


def test_chat_is_the_only_widget_with_typed_submission(qapp):
    spec, state = _state("chat")
    widget = ChatWidget(spec, state)
    sent = []
    widget.messageSubmitted.connect(sent.append)
    widget.input.setText("inspect the current plan")
    widget._submit()
    assert sent == ["inspect the current plan"]
    assert "inspect the current plan" in widget.history.toPlainText()


def test_weather_prompts_for_live_location_and_video_is_honest_without_path(qapp):
    weather_spec, weather_state = _state("weather", connected=False, location="No provider")
    weather = WeatherWidget(weather_spec, weather_state)
    assert "keyless weather is ready" in weather.notice.text()

    video_spec, video_state = _state("video_player", path=None)
    video = VideoPlayerWidget(video_spec, video_state)
    assert "No local video selected" in video.message.text()
    assert video.play.isEnabled() is False


def test_confirmation_emits_explicit_approval(qapp):
    spec, state = _state("confirmation", confirmation_id="c-1", action="Delete", risk="Permanent", target="draft.txt")
    widget = ConfirmationWidget(spec, state)
    values = []
    widget.resolved.connect(lambda confirmation_id, approved: values.append((confirmation_id, approved)))
    widget._resolve(True)
    assert values == [("c-1", True)]


def test_window_state_and_widget_creation(qapp, tmp_path):
    window = JarvisWindow(layout_path=tmp_path / "layout.json", demo_mode=False)
    window.set_voice_state("executing_tool")
    qapp.processEvents()
    assert window.core.state.value == "executing_tool"
    weather = window.request_widget("weather", data={"connected": False})
    qapp.processEvents()
    assert weather.widget_id in window.workspace.shells
    window.close()


def test_widget_header_drag_persists_and_close_is_always_reachable(qapp, tmp_path):
    window = JarvisWindow(layout_path=tmp_path / "layout.json", demo_mode=False)
    window.resize(1200, 760)
    window.show()
    state = window.request_widget("weather", position=(80, 120), data={"connected": False})
    qapp.processEvents()
    shell = window.workspace.shells[state.widget_id]
    before = shell.pos()
    shell._drag_start(QPoint(200, 200))
    shell._drag_move(QPoint(310, 275))
    shell._drag_finish(QPoint(310, 275))
    assert shell.pos() != before
    assert shell.y() >= window.workspace.TOP_SAFE_AREA
    persisted = window.widget_manager.get(state.widget_id)
    assert (persisted.x, persisted.y) == (shell.x(), shell.y())
    QTest.mouseClick(shell.close_button, Qt.LeftButton)
    qapp.processEvents()
    assert window.widget_manager.get(state.widget_id) is None
    assert state.widget_id not in window.workspace.shells
    window.close()


def test_palette_and_settings_stay_inside_main_window(qapp, tmp_path):
    window = JarvisWindow(layout_path=tmp_path / "layout.json", demo_mode=False)
    window.resize(1100, 720)
    window.show()
    window.toggle_palette()
    qapp.processEvents()
    assert window.palette.isVisible()
    assert window.palette.geometry().right() <= window.rect().right()
    assert window.palette.geometry().bottom() <= window.rect().bottom()
    assert all("planned" not in item.spec.title.lower() for item in window.palette._items)
    assert window.palette.window() is window
    window.open_settings()
    qapp.processEvents()
    assert window.settings_panel.isVisible()
    assert window.settings_panel.geometry().right() <= window.rect().right()
    assert window.settings_panel.window() is window
    window.close()


def test_core_click_acknowledges_listening_immediately(qapp, tmp_path):
    window = JarvisWindow(layout_path=tmp_path / "layout.json", demo_mode=False)
    pressed = []
    window.voicePressed.connect(lambda: pressed.append(True))
    window.core.activated.emit()
    qapp.processEvents()
    assert window.core.state.value == "listening"
    assert window.voice_button._active is True
    assert "Listening" in window.transcript.text()
    assert pressed == [True]
    window.close()


def test_settings_cover_jarvis_runtime_safety_and_layout():
    required = {
        "core_animation", "widget_hover_effects", "auto_open_widgets",
        "show_live_transcript", "voice_preload", "targeted_memory",
        "confirm_destructive", "confirm_external_actions", "confirm_commands",
        "layout_snap", "provider", "models", "api_keys", "voice_engine", "hotkey",
        "discord_bot_token", "discord_default_channel", "whatsapp_access_token",
        "whatsapp_phone_number_id", "whatsapp_api_version",
    }
    assert required <= JarvisSettingsView.DEFAULTS.keys()


def test_settings_keep_voice_choices_compatible_with_engine(qapp):
    panel = JarvisSettingsView()
    values = dict(JarvisSettingsView.DEFAULTS)
    values["voice_engine"] = "kokoro"
    values["voice"] = "Ryan — en_US-ryan-high"
    panel.load(values)
    assert panel.settings()["voice"] == "Puck — am_puck"
    assert panel._voice_choice.currentData() == "Puck — am_puck"


def test_new_settings_is_native_jarvis_surface(qapp):
    panel = JarvisSettingsView()
    assert panel.stack.count() == 7
    assert "CONTROL MATRIX" in panel.findChildren(QLabel)[0].text()
    assert panel.objectName() == "jarvisSettingsView"


def test_every_catalog_widget_builds_interactive_content(qapp):
    registry = build_default_registry()
    for spec in registry.all():
        if spec.widget_type == "settings":
            continue
        state = WidgetState.create(spec)
        content = create_widget_content(spec, state)
        assert content.findChildren(QPushButton) or spec.widget_type == "voice_transcript"
        content.deleteLater()
