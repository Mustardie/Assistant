"""Run the new JARVIS UI without replacing the legacy Nova entry point.

Usage::

    python -m app.jarvis_ui --demo
    python -m app.jarvis_ui
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path


def _apply_saved_runtime_environment() -> None:
    """Match main.py: settings must reach backend imports before startup."""
    path = Path(__file__).resolve().parents[1] / "data" / "nova_settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return
    provider = str(data.get("provider") or "Ollama")
    provider_key = provider.strip().lower().replace(" ", "")
    os.environ["LLM_PROVIDER"] = provider_key
    model_env = {
        "ollama": "OLLAMA_MODEL", "openrouter": "OPENROUTER_MODEL", "gemini": "GEMINI_MODEL",
        "openai": "OPENAI_MODEL", "anthropic": "ANTHROPIC_MODEL", "groq": "GROQ_MODEL", "deepseek": "DEEPSEEK_MODEL",
    }
    key_env = {
        "openrouter": "OPENROUTER_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    }
    model = (data.get("models") or {}).get(provider)
    api_key = (data.get("api_keys") or {}).get(provider)
    if model and provider_key in model_env:
        os.environ[model_env[provider_key]] = str(model)
    if api_key and provider_key in key_env:
        os.environ[key_env[provider_key]] = str(api_key)
    if data.get("assistant_name"):
        os.environ["ASSISTANT_NAME"] = str(data["assistant_name"])
    engine = str(data.get("voice_engine") or "piper").lower()
    os.environ["TTS_ENGINE"] = engine
    voice = str(data.get("voice") or "").rsplit(" ", 1)[-1].strip()
    if engine == "kokoro" and (not voice or voice.startswith("en_")):
        voice = "am_puck"
    elif engine == "piper" and voice and not voice.startswith(("en_", "de_", "fr_", "es_")):
        voice = "en_US-ryan-high"
    if voice:
        os.environ["KOKORO_VOICE" if engine == "kokoro" else "PIPER_VOICE"] = voice
    if data.get("speed") is not None:
        os.environ["TTS_SPEED"] = str(data["speed"])
    connector_environment = {
        "discord_bot_token": "JARVIS_DISCORD_BOT_TOKEN",
        "discord_default_channel": "JARVIS_DISCORD_DEFAULT_CHANNEL",
        "whatsapp_access_token": "JARVIS_WHATSAPP_ACCESS_TOKEN",
        "whatsapp_phone_number_id": "JARVIS_WHATSAPP_PHONE_NUMBER_ID",
        "whatsapp_api_version": "JARVIS_WHATSAPP_API_VERSION",
    }
    for setting_name, environment_name in connector_environment.items():
        value = data.get(setting_name)
        if value:
            os.environ[environment_name] = str(value)


_apply_saved_runtime_environment()

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ui.jarvis.window import JarvisWindow

logger = logging.getLogger(__name__)


class _RuntimeRelay(QObject):
    response = Signal(str)
    state = Signal(str)
    event = Signal(str, object)
    status = Signal(object)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Voice-first JARVIS interface")
    parser.add_argument("--demo", action="store_true", help="Open with mock widgets and no agent, mic, or external services")
    return parser


def _wire_runtime(window: JarvisWindow) -> None:
    """Attach the existing agent and voice stack with Qt-safe relays."""
    relay = _RuntimeRelay(window)
    try:
        from app.jarvis_widget_backend import JarvisWidgetBackend
        from brain.agent import Agent
        from config.settings import PROVIDER_MODEL_FIELD, settings

        agent = Agent()
        widget_backend = JarvisWidgetBackend(agent=agent)
    except Exception as exc:
        logger.exception("JARVIS agent initialization failed")
        window.widget_manager.update(
            window.widget_manager.find_type("system_status").widget_id,
            data={"model": "Unavailable", "warning": f"Agent failed to initialize: {exc}"},
            error=str(exc),
        )
        window.controller.set_state("error", detail="Agent runtime unavailable")
        return

    mode = {"value": "text"}
    voice = {"manager": None}

    def on_speak(message: str) -> None:
        relay.response.emit(str(message))
        manager = voice["manager"]
        if mode["value"] == "voice" and manager is not None:
            threading.Thread(target=manager.handle_speak, args=(message,), daemon=True).start()

    agent.set_voice_callback(on_speak)
    agent.set_event_callback(lambda event_type, payload: relay.event.emit(event_type, payload))
    relay.response.connect(window.append_assistant)
    relay.state.connect(window.set_voice_state)
    relay.event.connect(window.controller.handle_agent_event)
    model_field = PROVIDER_MODEL_FIELD.get(settings.llm_provider)
    active_model = getattr(settings, model_field, "") if model_field else ""
    window.update_system_status(
        model=f"{settings.llm_provider} · {active_model or 'configured'}",
        mic="Ready · click the core",
        connectors="Runtime tools available · accounts checked on use",
        task="Idle",
        error=None,
    )

    def run_text(text: str) -> None:
        mode["value"] = "text"
        window.begin_reply()
        threading.Thread(target=agent.run, args=(text,), daemon=True, name="jarvis-text-task").start()

    window.textSubmitted.connect(run_text)

    def cancel_task() -> None:
        manager = voice["manager"]
        if manager is not None:
            manager.stop()
        progress = window.widget_manager.find_type("task_progress")
        if progress:
            window.widget_manager.update(progress.widget_id, data={"status": "cancel_requested", "retry_status": "Stopping at the next safe boundary."}, loading=False)

    window.taskCancelled.connect(cancel_task)

    def widget_action(widget_id: str, action: str, payload: dict) -> None:
        if widget_id == "settings":
            if action == "test_provider":
                run_text(f"Test the active {(payload or {}).get('provider') or settings.llm_provider} model connection. Reply with a short status and do not call tools.")
            return
        state = window.widget_manager.get(widget_id)
        if state is None:
            return
        if state.widget_type == "file_search" and action == "search":
            query = str((payload or {}).get("query") or "").strip()
            if query:
                window.widget_manager.update(widget_id, loading=True, empty=False, error=None, data={"query": query, "results": []})
                run_text(f"Find my local file or folder matching: {query}")
            return
        if state.widget_type == "system_status" and action == "refresh":
            window.update_system_status(task="Idle", warning="Runtime status refreshed.")
            return
        window.widget_manager.update(widget_id, loading=True, error=None)

        def execute_widget_action():
            result = widget_backend.perform(state.widget_type, action, dict(payload or {}), state.data)
            window.widgetBackendResult.emit(widget_id, result)

        threading.Thread(target=execute_widget_action, daemon=True, name=f"jarvis-widget-{state.widget_type}").start()

    window.widgetAction.connect(widget_action)

    def resolve_confirmation(_confirmation_id: str, approved: bool) -> None:
        threading.Thread(target=agent.run, args=("yes" if approved else "no",), daemon=True, name="jarvis-confirmation").start()

    window.confirmationResolved.connect(resolve_confirmation)

    try:
        from voice import VoiceManager

        manager = VoiceManager(
            agent,
            callbacks={
                "on_listening": lambda: relay.state.emit("listening"),
                "on_transcribing": lambda: relay.state.emit("thinking"),
                "on_thinking": lambda: relay.state.emit("thinking"),
                "on_speaking": lambda: relay.state.emit("speaking"),
                "on_idle": lambda: relay.state.emit("idle"),
            },
        )
        voice["manager"] = manager
        status = window.widget_manager.find_type("system_status")
        if status:
            window.widget_manager.update(status.widget_id, data={"mic": "Ready · click the core", "stt": "Loading speech model…", "tts": "Loading voice…", "task": "Idle"})

        def warmup_voice() -> None:
            try:
                manager.warmup()
                relay.status.emit({"stt": f"Ready · {settings.voice_model}", "tts": f"Ready · {settings.tts_engine}", "warning": "Voice runtime is ready."})
            except Exception as exc:
                logger.exception("Voice warmup failed")
                relay.status.emit({"stt": "Unavailable", "tts": "Unavailable", "warning": f"Voice initialization failed: {exc}"})

        relay.status.connect(lambda values: window.update_system_status(**dict(values or {})))
        threading.Thread(target=warmup_voice, daemon=True, name="jarvis-voice-warmup").start()

        def start_voice() -> None:
            mode["value"] = "voice"
            if manager.is_running():
                manager.stop()
            else:
                manager.start_voice_session()

        window.voicePressed.connect(start_voice)
    except Exception as exc:
        logger.exception("Voice stack initialization failed")
        status = window.widget_manager.find_type("system_status")
        if status:
            window.widget_manager.update(
                status.widget_id,
                data={"mic": "Unavailable", "stt": "Unavailable", "tts": "Unavailable", "warning": f"Voice fallback: {exc}. Open Chat for typed control."},
            )

    window._jarvis_agent = agent
    window._jarvis_voice_manager = voice["manager"]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("JARVIS")
    window = JarvisWindow(demo_mode=args.demo)
    if not args.demo:
        _wire_runtime(window)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
