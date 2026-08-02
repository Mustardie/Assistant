import logging
import threading

from config.settings import settings
from voice.player import Player
from voice.recorder import Recorder
from voice.transcriber import Transcriber
from voice.tts import TTS

logger = logging.getLogger(__name__)


def _extract_spoken_text(text: str) -> str:
    """Strip anything that is not the assistant's spoken response.
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
    # Remove the "Name:" prefix if present (already redundant for TTS)
    prefix = f"{settings.assistant_name}:"
    if result.startswith(prefix):
        result = result[len(prefix):].strip()
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

    def set_agent(self, agent):
        """Point the manager at a (rebuilt) agent."""
        self.agent = agent

    def is_running(self) -> bool:
        return self._running

    def handle_speak(self, message: str):
        """Called from the agent's speak hook (wired by main.py) the moment
        the assistant speaks. Synthesises and plays immediately on a
        background thread so it doesn't block the agent loop."""
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

    def stop(self):
        """Abort a running voice session (mic toggle / hotkey press again)."""
        self.player.stop()
        self.player.resume()
        self.recorder.stop()

    def reset_voice(self, engine: str, voice: str, speed: float):
        """Apply voice-engine/voice/speed changes at runtime: drop the cached
        TTS pipeline and rebuild the wrapper so the next utterance uses the
        newly selected engine/voice. Keeps the previous setup on failure."""
        TTS._pipeline = None
        TTS._engine = None
        try:
            self.tts = TTS(voice=voice or None, speed=speed)
            logger.info("Voice re-initialized: engine=%s voice=%s speed=%s",
                        engine, voice, speed)
        except Exception:
            logger.exception("Failed to re-initialize TTS; keeping previous voice")

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
            self.recorder.reset()
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
            if self._stop_requested():
                logger.info("Voice: session stopped by user")
                return
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

    def _stop_requested(self) -> bool:
        return self.recorder.stop_requested()

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
