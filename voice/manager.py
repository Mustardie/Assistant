import logging
import threading

from voice.player import Player
from voice.recorder import Recorder
from voice.transcriber import Transcriber
from voice.tts import TTS

logger = logging.getLogger(__name__)


def _extract_spoken_text(text: str) -> str:
    """Strip anything that is not the Nova assistant's spoken response.
    The conversation history may contain tool logs or reasoning mixed in
    with the response; this heuristic keeps only the actual output."""
    if not text:
        return ""
    lines = text.split("\n")
    spoken = []
    for line in lines:
        stripped = line.strip()
        # Skip reasoning blocks, tool names, and JSON blobs
        if stripped.startswith("[") and stripped.endswith("]"):
            continue
        if stripped.startswith("{") or stripped.startswith("["):
            continue
        if stripped.startswith("```"):
            continue
        spoken.append(line)
    result = "\n".join(spoken).strip()
    # Remove leading "Nova:" prefix if present (already redundant for TTS)
    if result.startswith("Nova:"):
        result = result[5:].strip()
    return result


class VoiceManager:
    _WARMUP_TIMEOUT = 60.0

    def __init__(self, agent, callbacks=None):
        self.agent = agent
        self.callbacks = callbacks or {}
        self.recorder = Recorder()
        self.transcriber = Transcriber()
        self.tts = TTS()
        self.player = Player()
        self._running = False
        self._lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self._warmup_done = threading.Event()
        self._last_spoken_text = ""

        # Register callback on agent so TTS fires immediately when
        # Nova speaks (instead of waiting for agent.run() to finish).
        self.agent.set_voice_callback(self._on_agent_speak)

    def _on_agent_speak(self, message: str):
        """Called from agent._speak() the moment Nova: is printed.
        Synthesises and plays immediately on a background thread so
        it doesn't block the agent loop."""
        spoken = _extract_spoken_text(message)
        if not spoken:
            return

        # Deduplicate: skip if this is identical to the last spoken text.
        if spoken == self._last_spoken_text:
            logger.info("[TTS] Duplicate playback prevented: %s", spoken[:60])
            return

        self._last_spoken_text = spoken
        self._emit("on_speaking")
        logger.info("[TTS] Response queued: %s", spoken[:80])
        try:
            audio, sr = self.tts.synthesize(spoken)
            self.player.play(audio, sr)
            logger.info("[TTS] Playback started: %s", spoken[:80])
        except Exception:
            logger.exception("TTS failed in live callback")

    def warmup(self):
        try:
            self.transcriber.load_model()
            self.tts.load_pipeline()
        finally:
            self._warmup_done.set()

    def _await_warmup(self):
        if self._warmup_done.is_set():
            return
        logger.info("Voice: waiting for warmup to complete...")
        self._warmup_done.wait(timeout=self._WARMUP_TIMEOUT)
        if not self._warmup_done.is_set():
            logger.warning("Voice: warmup timed out, loading models synchronously")
            self.transcriber.load_model()
            self.tts.load_pipeline()
            self._warmup_done.set()

    def start_voice_session(self):
        with self._lock:
            if self._running:
                logger.warning("Voice: already running, dropping duplicate session")
                return
            self._running = True
            # Cancel any previous playback immediately
            self.player.stop()
            self.player.resume()

        thread = threading.Thread(target=self._run_session, daemon=True)
        thread.start()

    def _run_session(self):
        try:
            self._await_warmup()
            self._last_spoken_text = ""
            self._emit("on_listening")
            audio = self.recorder.record()
            if audio.size == 0:
                logger.warning("No audio captured")
                return

            self._emit("on_transcribing")
            text = self.transcriber.transcribe(audio)
            if not text:
                logger.info("Voice: empty transcription")
                return

            logger.info("Voice: recognized '%s'", text)

            self._emit("on_thinking")
            self.agent.run(text)
        except Exception:
            logger.exception("Voice session failed")
        finally:
            self._running = False
            self._emit("on_idle")

    def _get_last_response(self) -> str:
        try:
            history = self.agent.memory_manager.get_conversation_history()
            for msg in reversed(history):
                if msg.get("role") == "assistant":
                    return msg.get("content", "")
        except Exception:
            pass
        return ""

    def _emit(self, event):
        callback = self.callbacks.get(event)
        if callback:
            try:
                callback()
            except Exception:
                logger.exception("Voice callback '%s' failed", event)
