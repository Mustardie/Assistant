import threading
from pathlib import Path
from typing import Callable, Optional


class FileWatcher:
    def __init__(self, root: Path, callback: Optional[Callable[[str], None]] = None):
        self.root = Path(root).expanduser().resolve()
        self.callback = callback
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self.callback:
                self.callback(str(self.root))
            self._stop_event.wait(2)
