import atexit
import json
import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


configure_logging()

_ROOT = Path(__file__).resolve().parent


# ------------------------------------------------------------------ #
# Persisted settings -> environment
#
# The UI settings (data/nova_settings.json) are the source of truth for
# assistant name, theme, provider/model/API key and voice choices. They
# are pushed into the environment BEFORE config.settings / brain / ui are
# imported, so every component built at startup picks them up.
# ------------------------------------------------------------------ #
_ENV_VAR_FOR_FIELD = {
    "ollama_model": "OLLAMA_MODEL",
    "gemini_model": "GEMINI_MODEL", "gemini_api_key": "GEMINI_API_KEY",
    "openrouter_model": "OPENROUTER_MODEL", "openrouter_api_key": "OPENROUTER_API_KEY",
    "openai_model": "OPENAI_MODEL", "openai_api_key": "OPENAI_API_KEY",
    "anthropic_model": "ANTHROPIC_MODEL", "anthropic_api_key": "ANTHROPIC_API_KEY",
    "groq_model": "GROQ_MODEL", "groq_api_key": "GROQ_API_KEY",
    "deepseek_model": "DEEPSEEK_MODEL", "deepseek_api_key": "DEEPSEEK_API_KEY",
}

_EARLY_PROVIDER_MODEL_FIELD = {
    "ollama": "ollama_model", "gemini": "gemini_model",
    "openrouter": "openrouter_model", "openai": "openai_model",
    "anthropic": "anthropic_model", "groq": "groq_model",
    "deepseek": "deepseek_model",
}
_EARLY_PROVIDER_API_KEY_FIELD = {
    "ollama": None, "gemini": "gemini_api_key", "openrouter": "openrouter_api_key",
    "openai": "openai_api_key", "anthropic": "anthropic_api_key",
    "groq": "groq_api_key", "deepseek": "deepseek_api_key",
}


def _apply_provider_to_env(provider_display: str, model: str, api_key: str):
    """Push the selected provider's model/key into the environment using
    the generic provider->field lookup tables in config/settings.py,
    instead of matching against specific model-name substrings. Works for
    every registered provider (Ollama, OpenRouter, Gemini, OpenAI,
    Anthropic, Groq, DeepSeek) with no per-provider special-casing here.
    """
    provider_key = (provider_display or "ollama").strip().lower().replace(" ", "")
    os.environ["LLM_PROVIDER"] = provider_key

    model = (model or "").strip()
    model_field = _EARLY_PROVIDER_MODEL_FIELD.get(provider_key)
    if model and model_field and model_field in _ENV_VAR_FOR_FIELD:
        os.environ[_ENV_VAR_FOR_FIELD[model_field]] = model

    key = (api_key or "").strip()
    key_field = _EARLY_PROVIDER_API_KEY_FIELD.get(provider_key)
    if key and key_field and key_field in _ENV_VAR_FOR_FIELD:
        os.environ[_ENV_VAR_FOR_FIELD[key_field]] = key


def _apply_persisted_settings_to_env():
    try:
        path = _ROOT / "data" / "nova_settings.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return

    name = (data.get("assistant_name") or "").strip()
    if name:
        os.environ["ASSISTANT_NAME"] = name

    theme = (data.get("theme") or "").strip()
    if theme:
        os.environ["NOVA_THEME"] = theme

    provider = data.get("provider") or "Ollama"
    models = data.get("models") or {}
    api_keys = data.get("api_keys") or {}
    # Back-compat: fall back to the old flat "model" / "api_key" fields if
    # this settings file predates the per-provider schema.
    model = models.get(provider) or data.get("model") or ""
    api_key = api_keys.get(provider) or data.get("api_key") or ""
    _apply_provider_to_env(provider, model, api_key)

    voice_engine = (data.get("voice_engine") or "").strip().lower()
    if voice_engine:
        os.environ["TTS_ENGINE"] = voice_engine
    voice = data.get("voice") or ""
    # The label is "Name — voice_id" (some older files stored a mangled
    # separator, so take the id after the LAST space instead of matching a
    # specific dash character).
    voice_id = voice.rsplit(" ", 1)[-1].strip() if voice.strip() else ""
    if voice_engine == "kokoro" and (not voice_id or voice_id.startswith("en_")):
        voice_id = "am_puck"
    elif voice_engine == "piper" and voice_id and not voice_id.startswith(("en_", "de_", "fr_", "es_")):
        voice_id = "en_US-ryan-high"
    if voice_id:
        os.environ["KOKORO_VOICE" if voice_engine == "kokoro" else "PIPER_VOICE"] = voice_id
    try:
        speed = float(data.get("speed", 1.0))
        os.environ["TTS_SPEED"] = str(speed)
    except (TypeError, ValueError):
        pass


_apply_persisted_settings_to_env()

# Import the frozen settings singleton only after persisted selections have
# reached the environment.  Importing it earlier silently locked the agent to
# stale .env values until restart/rebuild.
from config.settings import (
    PROVIDER_API_KEY_FIELD,
    PROVIDER_MODEL_FIELD,
    apply_runtime_overrides,
    settings,
)

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from brain.agent import Agent
from app.jarvis_widget_backend import JarvisWidgetBackend
_legacy_ui = os.getenv("JARVIS_LEGACY_UI", "").strip().lower() in {"1", "true", "yes"}
if _legacy_ui:
    from ui.nova_window import NovaWindow
else:
    from ui.jarvis.window import JarvisWindow
from backend.bridge import AssistantBridge
from backend.browser_server import start_bridge, stop_bridge


def _start_embedded_bridge():
    """Start the browser bridge in-process so the Edge extension has a
    backend without requiring a separate terminal. Reuses an existing
    bridge if one is already running on 127.0.0.1:8742."""
    ok = start_bridge()
    if not ok:
        logger.warning(
            "Browser bridge is not available -- browser commands "
            "will fail until it can be started"
        )
    return ok


# Start the browser bridge before anything else (Agent may touch it).
_bridge_started = _start_embedded_bridge()


def _shutdown_bridge():
    """Gracefully stop the embedded bridge when Nova exits."""
    if _bridge_started:
        stop_bridge()
    else:
        logger.info("Browser bridge was not started by this process -- leaving it running")


atexit.register(_shutdown_bridge)

agent = Agent()
_widget_backend = JarvisWidgetBackend(agent=agent)


# ------------------------------------------------------------------ #
# Response routing: text-in -> text-out, voice-in -> voice-out
# ------------------------------------------------------------------ #
# The agent's speak hook fires for every reply. main.py owns the single
# callback and routes it by the mode the turn was started in:
#   text  -> bridge.say()   -> window.append_assistant (text display)
#   voice -> VoiceManager.handle_speak() -> TTS playback (voice only)
_mode = {"value": "text"}

_voice_manager = None


def _on_agent_speak(message: str):
    if _mode["value"] == "voice" and _voice_manager is not None:
        _voice_manager.handle_speak(message)
    else:
        bridge.say(message)


# ------------------------------------------------------------------ #
# Voice mode
# ------------------------------------------------------------------ #

def _start_voice():
    """Mic toggle: starts a listening session, or stops one in progress."""
    if _voice_manager is None:
        logger.warning("Voice manager not available")
        return
    if _voice_manager.is_running():
        logger.info("Voice: stopping active session (mic toggle)")
        _voice_manager.stop()
        return
    _mode["value"] = "voice"
    threading.Thread(
        target=_voice_manager.start_voice_session, daemon=True
    ).start()


def _preload_voice_models():
    try:
        logger.info("Voice: preloading models...")
        if hasattr(window, "runtimeStatus"):
            window.runtimeStatus.emit({"stt": "Loading speech model…", "tts": "Loading voice…"})
        _voice_manager.warmup()
        logger.info("Voice: models ready")
        if hasattr(window, "runtimeStatus"):
            window.runtimeStatus.emit({
                "stt": f"Ready · {settings.voice_model}",
                "tts": f"Ready · {settings.tts_engine}",
                "warning": "Voice, agent, and available connectors are wired to this interface.",
            })
    except Exception as exc:
        logger.exception("Voice model preloading failed")
        if hasattr(window, "runtimeStatus"):
            window.runtimeStatus.emit({"stt": "Unavailable", "tts": "Unavailable", "warning": f"Voice initialization failed: {exc}"})


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #

app = QApplication(sys.argv)
app.setApplicationName(os.environ.get("ASSISTANT_NAME", "Nova"))

# Gracefully stop the embedded bridge when the event loop exits.
app.aboutToQuit.connect(_shutdown_bridge)

bridge = AssistantBridge()


def _handle_text_submit(text: str):
    """Text in -> text out. The reply arrives through bridge.responseReady
    (agent's speak hook -> bridge.say -> window.append_assistant)."""
    _mode["value"] = "text"
    if hasattr(window, "begin_reply"):
        window.begin_reply()
    threading.Thread(target=agent.run, args=(text,), daemon=True).start()


window = NovaWindow() if _legacy_ui else JarvisWindow()

# Single speak hook: routed by mode (see _on_agent_speak).
agent.set_voice_callback(_on_agent_speak)
if hasattr(window, "agentEvent"):
    agent.set_event_callback(lambda event_type, payload: window.agentEvent.emit(event_type, payload))
window.textSubmitted.connect(_handle_text_submit)
window.voicePressed.connect(_start_voice)


def _cancel_current_task():
    if _voice_manager is not None:
        _voice_manager.stop()
    progress = window.widget_manager.find_type("task_progress") if hasattr(window, "widget_manager") else None
    if progress:
        window.widget_manager.update(progress.widget_id, data={"status": "cancel_requested", "retry_status": "The current model call will stop at its next safe boundary."}, loading=False)
    if hasattr(window, "runtimeStatus"):
        window.runtimeStatus.emit({"task": "Cancellation requested", "warning": "JARVIS will stop at the next safe task boundary."})


if hasattr(window, "taskCancelled"):
    window.taskCancelled.connect(_cancel_current_task)


def _handle_widget_action(widget_id: str, action: str, payload: dict):
    if not hasattr(window, "widget_manager"):
        return
    if widget_id == "settings":
        if action == "test_provider":
            provider = str((payload or {}).get("provider") or settings.llm_provider)
            _handle_text_submit(f"Test the active {provider} model connection. Reply with a short status and do not call tools.")
        return
    state = window.widget_manager.get(widget_id)
    if state is None:
        return
    if state.widget_type == "file_search" and action == "search":
        query = str((payload or {}).get("query") or "").strip()
        if query:
            window.widget_manager.update(widget_id, loading=True, empty=False, error=None, data={"query": query, "results": []})
            _handle_text_submit(f"Find my local file or folder matching: {query}")
        return
    if state.widget_type == "system_status" and action == "refresh":
        window.runtimeStatus.emit({"connectors": _connector_summary(), "task": "Idle", "warning": "Runtime status refreshed."})
        return
    window.widget_manager.update(widget_id, loading=True, error=None)

    def execute_widget_action():
        result = _widget_backend.perform(state.widget_type, action, dict(payload or {}), state.data)
        window.widgetBackendResult.emit(widget_id, result)

    threading.Thread(target=execute_widget_action, daemon=True, name=f"jarvis-widget-{state.widget_type}").start()


if hasattr(window, "widgetAction"):
    window.widgetAction.connect(_handle_widget_action)


def _display_response(text: str):
    window.append_assistant(text)


bridge.responseReady.connect(_display_response)

from voice import VoiceManager

_voice_manager = VoiceManager(
    agent,
    callbacks={
        "on_listening": bridge.listening,
        "on_transcribing": bridge.transcribing,
        "on_thinking": bridge.thinking,
        "on_speaking": bridge.speaking,
        "on_idle": bridge.idle,
    },
)


def _connector_summary() -> str:
    parts = ["Browser bridge ready" if _bridge_started else "Browser bridge unavailable"]
    try:
        from youtube_auth import TOKEN_PATH
        parts.append("Google authorized" if TOKEN_PATH.exists() else "Google sign-in required")
    except Exception:
        parts.append("Google status unavailable")
    return " · ".join(parts)


if hasattr(window, "update_system_status"):
    model_field = PROVIDER_MODEL_FIELD.get(settings.llm_provider)
    active_model = getattr(settings, model_field, "") if model_field else ""
    window.update_system_status(
        model=f"{settings.llm_provider} · {active_model or 'configured'}",
        mic="Ready · click the core",
        stt=f"Initializing · {settings.voice_model}",
        tts=f"Initializing · {settings.tts_engine}",
        connectors=_connector_summary(),
        task="Idle",
        warning="Initializing speech models in the background…",
        error=None,
    )


def _on_state_changed(state: str):
    window.set_voice_state(state)
    if state == "listening":
        window.show_and_raise()


bridge.stateChanged.connect(_on_state_changed)

threading.Thread(target=_preload_voice_models, daemon=True, name="voice-warmup").start()


# ------------------------------------------------------------------ #
# Global hotkey (configurable from Settings)
# ------------------------------------------------------------------ #
import keyboard

_hotkey_handle = None


def _on_hotkey():
    # keyboard's callback fires on its own background thread -- hop back
    # onto the Qt thread before touching the window.
    QTimer.singleShot(0, window.toggle)
    QTimer.singleShot(0, _start_voice)


def _register_hotkey(combo: str):
    global _hotkey_handle
    combo = (combo or "ctrl+space").strip().lower()
    try:
        if _hotkey_handle is not None:
            keyboard.remove_hotkey(_hotkey_handle)
    except Exception:
        pass
    _hotkey_handle = None
    try:
        _hotkey_handle = keyboard.add_hotkey(combo, _on_hotkey)
        logger.info("Global hotkey registered: %s", combo)
    except Exception:
        logger.exception("Failed to register hotkey %r", combo)


def _load_hotkey_from_disk() -> str:
    try:
        path = _ROOT / "data" / "nova_settings.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("hotkey") or "ctrl+space"
    except Exception:
        pass
    return "ctrl+space"


# ------------------------------------------------------------------ #
# Live settings application
# ------------------------------------------------------------------ #
_initial_ui_settings = dict(getattr(window, "_settings", {}) or {})
_initial_provider = _initial_ui_settings.get("provider") or "Ollama"
_prev_backend_sig = (
    _initial_provider,
    (_initial_ui_settings.get("models") or {}).get(_initial_provider) or "",
    (_initial_ui_settings.get("api_keys") or {}).get(_initial_provider) or "",
)
_initial_engine = str(_initial_ui_settings.get("voice_engine") or "piper").lower()
_initial_voice = str(_initial_ui_settings.get("voice") or "").rsplit(" ", 1)[-1].strip()
_prev_voice_sig = (_initial_engine, _initial_voice, float(_initial_ui_settings.get("speed", 1.0)))


def _rebuild_agent(provider: str, model: str, api_key: str):
    """Rebuild the agent so provider/model/API-key changes take effect
    immediately. Skipped (with a warning) while a run is in progress."""
    global agent
    old_agent = agent
    if not old_agent._run_lock.acquire(blocking=False):
        logger.warning(
            "Agent is busy -- provider/model change will apply on next launch"
        )
        return
    try:
        _apply_provider_to_env(provider, model, api_key)
        # Push the new values into the frozen Settings singleton too, so
        # code that was built BEFORE the change (Brain, LLM clients, TTS)
        # picks them up on rebuild -- not just code reading env at import.
        # Every field in _ENV_VAR_FOR_FIELD is refreshed from the
        # environment we just updated, so this covers all 7 providers
        # rather than only OpenRouter/Gemini.
        overrides = {
            field: os.environ[env_var]
            for field, env_var in _ENV_VAR_FOR_FIELD.items()
            if os.environ.get(env_var)
        }
        apply_runtime_overrides(llm_provider=os.environ.get("LLM_PROVIDER", ""), **overrides)
        new_agent = Agent()
        new_agent.set_voice_callback(_on_agent_speak)
        if hasattr(window, "agentEvent"):
            new_agent.set_event_callback(lambda event_type, payload: window.agentEvent.emit(event_type, payload))
        _voice_manager.set_agent(new_agent)
        agent = new_agent
        _widget_backend.agent = new_agent
        logger.info("Agent rebuilt: provider=%s model=%s", provider, model)
        if hasattr(window, "runtimeStatus"):
            window.runtimeStatus.emit({"model": f"{provider} · {model or 'configured'}"})
    except Exception:
        logger.exception("Agent rebuild failed; keeping current agent")
    finally:
        old_agent._run_lock.release()


def _on_settings_changed_main(settings: dict):
    global _prev_backend_sig, _prev_voice_sig
    _register_hotkey(settings.get("hotkey"))

    name = (settings.get("assistant_name") or "").strip()
    if name:
        apply_runtime_overrides(assistant_name=name)

    theme = (settings.get("theme") or "").strip()
    if theme:
        apply_runtime_overrides(nova_theme=theme)

    provider = settings.get("provider") or "Ollama"
    model = (settings.get("models") or {}).get(provider) or ""
    api_key = (settings.get("api_keys") or {}).get(provider) or ""
    sig = (provider, model, api_key)
    if sig != _prev_backend_sig:
        _prev_backend_sig = sig
        _rebuild_agent(provider, model, api_key)

    voice_engine = (settings.get("voice_engine") or "piper").strip().lower()
    voice = settings.get("voice") or ""
    voice_id = voice.rsplit(" ", 1)[-1].strip() if voice.strip() else ""
    if voice_engine == "kokoro" and (not voice_id or voice_id.startswith("en_")):
        voice_id = "am_puck"
    elif voice_engine == "piper" and voice_id and not voice_id.startswith(("en_", "de_", "fr_", "es_")):
        voice_id = "en_US-ryan-high"
    try:
        speed = float(settings.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    v_sig = (voice_engine, voice_id, speed)
    if v_sig != _prev_voice_sig:
        _prev_voice_sig = v_sig
        apply_runtime_overrides(
            tts_engine=voice_engine,
            piper_voice=voice_id if voice_engine == "piper" else "",
            kokoro_voice=voice_id if voice_engine == "kokoro" else "",
            tts_speed=speed,
        )
        _voice_manager.reset_voice(voice_engine, voice_id, speed)


window.settingsChanged.connect(_on_settings_changed_main)

_register_hotkey(_load_hotkey_from_disk())

window.show()
app.exec()
