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
from config.settings import apply_runtime_overrides, PROVIDER_MODEL_FIELD, PROVIDER_API_KEY_FIELD
from llm.factory import DISPLAY_TO_PROVIDER_KEY

_ENV_VAR_FOR_FIELD = {
    "ollama_model": "OLLAMA_MODEL",
    "gemini_model": "GEMINI_MODEL", "gemini_api_key": "GEMINI_API_KEY",
    "openrouter_model": "OPENROUTER_MODEL", "openrouter_api_key": "OPENROUTER_API_KEY",
    "openai_model": "OPENAI_MODEL", "openai_api_key": "OPENAI_API_KEY",
    "anthropic_model": "ANTHROPIC_MODEL", "anthropic_api_key": "ANTHROPIC_API_KEY",
    "groq_model": "GROQ_MODEL", "groq_api_key": "GROQ_API_KEY",
    "deepseek_model": "DEEPSEEK_MODEL", "deepseek_api_key": "DEEPSEEK_API_KEY",
}


def _apply_provider_to_env(provider_display: str, model: str, api_key: str):
    """Push the selected provider's model/key into the environment using
    the generic provider->field lookup tables in config/settings.py,
    instead of matching against specific model-name substrings. Works for
    every registered provider (Ollama, OpenRouter, Gemini, OpenAI,
    Anthropic, Groq, DeepSeek) with no per-provider special-casing here.
    """
    provider_key = DISPLAY_TO_PROVIDER_KEY.get(provider_display, (provider_display or "ollama").lower())
    os.environ["LLM_PROVIDER"] = provider_key

    model = (model or "").strip()
    model_field = PROVIDER_MODEL_FIELD.get(provider_key)
    if model and model_field and model_field in _ENV_VAR_FOR_FIELD:
        os.environ[_ENV_VAR_FOR_FIELD[model_field]] = model

    key = (api_key or "").strip()
    key_field = PROVIDER_API_KEY_FIELD.get(provider_key)
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
    if voice_id:
        os.environ["PIPER_VOICE"] = voice_id
    try:
        speed = float(data.get("speed", 1.0))
        os.environ["TTS_SPEED"] = str(speed)
    except (TypeError, ValueError):
        pass


_apply_persisted_settings_to_env()

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from brain.agent import Agent
from ui.nova_window import NovaWindow
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

# ------------------------------------------------------------------ #
# Integration layer: adapters + event workflows
# ------------------------------------------------------------------ #
# Registers every built-in adapter (Discord, WhatsApp, Google, ...) into
# the ConnectionManager so the Connections page can list them and the
# universal tools can route capability calls to connected services.
from adapters import register_all as register_adapters
from workflows import register_all as register_workflows

try:
    _adapter_count = register_adapters()
    logger.info("[Startup] Registered %d integration adapters.", _adapter_count)
except Exception:
    logger.exception("[Startup] Adapter registration failed")

try:
    register_workflows()
    logger.info("[Startup] Registered event workflows.")
except Exception:
    logger.exception("[Startup] Workflow registration failed")

agent = Agent()


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
        _voice_manager.warmup()
        logger.info("Voice: models ready")
    except Exception:
        logger.exception("Voice model preloading failed")


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
    threading.Thread(target=agent.run, args=(text,), daemon=True).start()


window = NovaWindow()

# Single speak hook: routed by mode (see _on_agent_speak).
agent.set_voice_callback(_on_agent_speak)
window.textSubmitted.connect(_handle_text_submit)
window.voicePressed.connect(_start_voice)


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
_prev_backend_sig = None
_prev_voice_sig = None


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
        _voice_manager.set_agent(new_agent)
        agent = new_agent
        logger.info("Agent rebuilt: provider=%s model=%s", provider, model)
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
