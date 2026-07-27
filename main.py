import logging
import threading
import sys

from PySide6.QtWidgets import QApplication

from brain.agent import Agent
from ui.main_window import AssistantWindow
from backend.bridge import AssistantBridge

logger = logging.getLogger(__name__)


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


configure_logging()

agent = Agent()


# ------------------------------------------------------------------ #
# Voice mode
# ------------------------------------------------------------------ #

_voice_manager = None


def _start_voice():
    if _voice_manager is None:
        logger.warning("Voice manager not available")
        return

    _voice_manager.start_voice_session()


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #

app = QApplication(sys.argv)

window = AssistantWindow()

from voice import VoiceManager

_voice_manager = VoiceManager(
    agent,
    callbacks={
        "on_listening": lambda: window.set_state("listening"),
        "on_transcribing": lambda: window.set_state("thinking"),
        "on_thinking": lambda: window.set_state("thinking"),
        "on_speaking": lambda: window.set_state("speaking"),
        "on_idle": lambda: window.set_state("idle"),
    },
)


def _preload_voice_models():
    try:
        logger.info("Voice: preloading models...")
        _voice_manager.warmup()
        logger.info("Voice: models ready")
    except Exception:
        logger.exception("Voice model preloading failed")


threading.Thread(target=_preload_voice_models, daemon=True, name="voice-warmup").start()

import keyboard
keyboard.add_hotkey("ctrl+space", window.trigger_hotkey)

bridge = AssistantBridge()

window.textSubmitted.connect(agent.run)
window.activateAssistant.connect(_start_voice)
window.voicePressed.connect(_start_voice)

app.exec()