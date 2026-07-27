import numpy as np

from config.settings import settings


class TTS:
    _pipeline = None

    def __init__(self, voice=None, speed=None, lang_code="a"):
        self.voice = voice or settings.tts_voice
        self.speed = speed or settings.tts_speed
        self.lang_code = lang_code

    def load_pipeline(self):
        if TTS._pipeline is not None:
            return
        from kokoro import KPipeline
        TTS._pipeline = KPipeline(lang_code=self.lang_code)

    def synthesize(self, text: str):
        self.load_pipeline()
        if not text.strip():
            return np.zeros(0, dtype=np.float32), 24000
        gen = TTS._pipeline(text, voice=self.voice, speed=self.speed)
        audio_parts = []
        for result in gen:
            audio_parts.append(result[-1])
        if not audio_parts:
            return np.zeros(0, dtype=np.float32), 24000
        sample_rate = getattr(TTS._pipeline, "sample_rate", 24000)
        return np.concatenate(audio_parts), sample_rate
