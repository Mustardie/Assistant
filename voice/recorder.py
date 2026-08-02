import threading

import numpy as np
import sounddevice as sd

from config.settings import settings


class Recorder:
    def __init__(self, sample_rate=None, silence_duration=None, device=None):
        self.sample_rate = sample_rate or settings.voice_sample_rate
        self.silence_duration = silence_duration or settings.voice_silence_seconds
        self.device = device or (settings.voice_device or None)
        self._threshold = 0.01
        self._stop = threading.Event()

    def reset(self):
        """Clear any previous stop request before a new session."""
        self._stop.clear()

    def stop(self):
        """Ask record() to return as soon as possible (mic toggle)."""
        self._stop.set()

    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def record(self) -> np.ndarray:
        chunk = int(self.sample_rate * 0.1)
        audio_buf = []
        voice_active = False
        silence_chunks = 0
        silence_limit = int(self.silence_duration / 0.1)
        done = threading.Event()

        def callback(indata, frames, time_info, status):
            nonlocal voice_active, silence_chunks
            rms = np.sqrt(np.mean(indata ** 2))
            if rms > self._threshold:
                if not voice_active:
                    voice_active = True
                silence_chunks = 0
            elif voice_active:
                silence_chunks += 1
                if silence_chunks >= silence_limit:
                    done.set()
            if voice_active:
                audio_buf.append(indata.copy())

        with sd.InputStream(
            samplerate=self.sample_rate,
            device=self.device,
            channels=1,
            dtype="float32",
            blocksize=chunk,
            callback=callback,
        ):
            while not done.is_set() and not self._stop.is_set():
                done.wait(0.1)

        if self._stop.is_set() and not voice_active:
            return np.zeros(0, dtype=np.float32)
        if not audio_buf:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(audio_buf).ravel()
