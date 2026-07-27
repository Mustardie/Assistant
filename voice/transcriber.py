import numpy as np

from config.paths import get_nova_cache_dir
from config.settings import settings


class Transcriber:
    _model = None

    def __init__(self, model_size=None, language=None, device="cpu"):
        self.model_size = model_size or settings.voice_model
        self.language = language or settings.voice_language
        self.device = device

    def load_model(self):
        if Transcriber._model is not None:
            return
        from faster_whisper import WhisperModel
        model_dir = get_nova_cache_dir() / "whisper"
        model_dir.mkdir(parents=True, exist_ok=True)
        Transcriber._model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type="int8",
            download_root=str(model_dir),
        )

    def transcribe(self, audio: np.ndarray) -> str:
        self.load_model()
        if audio.size == 0:
            return ""
        segments, _ = Transcriber._model.transcribe(
            audio,
            language=self.language,
            beam_size=5,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
