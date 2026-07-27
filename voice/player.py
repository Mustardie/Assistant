import threading
from collections import deque

import numpy as np
import sounddevice as sd


class Player:
    def __init__(self):
        self._queue = deque()
        self._thread = None
        self._stop = threading.Event()

    def play(self, audio: np.ndarray, sample_rate: int = 24000):
        if audio.size == 0:
            return
        self._queue.append((audio, sample_rate))
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.is_set() and self._queue:
            audio, sample_rate = self._queue.popleft()
            sd.play(audio, samplerate=sample_rate)
            sd.wait()

    def stop(self):
        self._stop.set()
        sd.stop()
