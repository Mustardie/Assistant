import logging
import threading

from voice.player import Player
from voice.recorder import Recorder
from voice.transcriber import Transcriber
from voice.tts import TTS

logger = logging.getLogger(__name__)


class VoiceManager:
    def __init__(self, agent, callbacks=None):
        self.agent = agent
        self.callbacks = callbacks or {}
        self.recorder = Recorder()
        self.transcriber = Transcriber()
        self.tts = TTS()
        self.player = Player()
        self._running = False
        self._lock = threading.Lock()

    def warmup(self):
        self.transcriber.load_model()
        self.tts.load_pipeline()

    def start_voice_session(self):
        with self._lock:
            if self._running:
                return
            self._running = True

        thread = threading.Thread(target=self._run_session, daemon=True)
        thread.start()

    def _run_session(self):
        try:
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

            response = self._get_last_response()
            if response:
                self._emit("on_speaking")
                try:
                    audio, sr = self.tts.synthesize(response)
                    self.player.play(audio, sr)
                except Exception:
                    logger.exception("TTS failed, response shown as text")
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
