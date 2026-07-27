"""AssistantBridge: the seam between this UI and the existing agent stack
(agent.py / brain.py / agent_loop.py / etc.).

Per the brief, this file adds HOOKS ONLY — no assistant logic. It exists so
`ui/` never imports anything backend-shaped and `backend/` never imports
anything Qt-widget-shaped; main.py is the only place both are known about.

--------------------------------------------------------------------------
HOW THIS PLUGS INTO THE EXISTING CODEBASE (agent.py / main.py you shared)
--------------------------------------------------------------------------
Your current main.py does two things this bridge is designed to slot into
without changing your backend architecture:

1. It builds `agent = Agent()` once at startup and calls `agent.run(user)`
   for each line of text input. Wire that here:

       bridge.textSubmitted.connect(lambda text: agent.run(text))

   `Agent.run()` already does its own speaking via `self._speak()` /
   `self.assistant.speak()` — you can have `Assistant.speak()` call
   `bridge.say(text)` (added below) instead of/alongside whatever TTS
   playback it does now, so the UI can show response text if desired.

2. It defines a VoiceManager with on_listening/on_transcribing/on_thinking/
   on_speaking/on_idle callbacks that currently just `print(...)`. Point
   those callbacks at this bridge's state setters instead:

       def on_listening():
           bridge.listening()
       def on_transcribing():
           bridge.transcribing()
       ...

   That is the ENTIRE integration for voice status — no changes needed
   inside VoiceManager or Agent/Brain/AgentLoop themselves.

3. The global hotkey in your main.py currently calls `_start_voice()`
   directly. Keep that exactly as-is, but also call
   `window.trigger_hotkey()` (see ui/main_window.py) so the UI opens in
   sync with the real voice session starting:

       def _start_voice():
           ...
           window.trigger_hotkey()
           _voice_manager.start_voice_session()

--------------------------------------------------------------------------
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class AssistantBridge(QObject):
    """Backend-facing signal hub. UI widgets never see this class directly;
    main.py connects AssistantWindow's signals to slots here (or straight
    to the agent), and connects this bridge's outgoing signals to
    AssistantWindow.set_state() / show_history() / show_settings()."""

    # -- outgoing: UI intent -> backend (main.py connects these to Agent) --- #
    voicePressed = Signal()
    textSubmitted = Signal(str)
    historyRequested = Signal()
    settingsRequested = Signal()
    closeRequested = Signal()
    activateAssistant = Signal()
    stopListening = Signal()

    # -- incoming: backend state -> UI (main.py connects these to AssistantWindow) #
    stateChanged = Signal(str)        # "idle" | "listening" | "thinking" | "speaking"
    responseReady = Signal(str)       # assistant's spoken/text response, for optional display
    historyReady = Signal(list)       # list[{"role": ..., "content": ...}]

    # -- convenience emitters (optional sugar; plain .emit() also works) --- #

    def listening(self) -> None:
        self.stateChanged.emit("listening")

    def transcribing(self) -> None:
        self.stateChanged.emit("thinking")

    def thinking(self) -> None:
        self.stateChanged.emit("thinking")

    def speaking(self) -> None:
        self.stateChanged.emit("speaking")

    def idle(self) -> None:
        self.stateChanged.emit("idle")

    def say(self, text: str) -> None:
        self.responseReady.emit(text)

    def deliver_history(self, entries: list[dict]) -> None:
        self.historyReady.emit(entries)
