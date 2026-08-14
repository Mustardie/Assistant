"""Event watchers -- background pollers that translate changes in
connected services/filesystem into AppEvents.

The design keeps polling intervals per-integration configurable and
event-driven where possible. Watchers are lightweight (they only wake on
their interval or a filesystem event) and NEVER screenshot or run vision.

Watchers to build out over time:
    FilesystemEventWatcher  -- NEW_FILE / DOWNLOAD_COMPLETED
    (messaging watchers run inside adapters where they can reuse
     push/webhook channels -- e.g. Gmail push, browser bridge)
"""

import logging
import threading
import time
from pathlib import Path

from .dispatcher import event_dispatcher
from .models import make_event, NEW_FILE, DOWNLOAD_COMPLETED

logger = logging.getLogger(__name__)


class BaseWatcher(threading.Thread):
    """Daemon thread that polls `check()` on an interval and stops cleanly."""

    name = "base-watcher"
    interval_s: float = 10.0

    def __init__(self, interval_s: float | None = None, *, name: str | None = None):
        super().__init__(daemon=True)
        if name:
            self.name = name
        if interval_s is not None:
            self.interval_s = max(0.5, float(interval_s))
        self._stop_event = threading.Event()
        self._started = threading.Event()

    def run(self):
        logger.info("[Events] %s started (interval %.1fs)", self.name, self.interval_s)
        self._started.set()
        while not self._stop_event.is_set():
            started = time.time()
            try:
                self.check()
            except Exception as exc:
                logger.warning("[Events] %s check failed: %s", self.name, exc)
            elapsed = time.time() - started
            self._stop_event.wait(max(0.5, self.interval_s - elapsed))

    def check(self):
        """Subclasses implement; emit AppEvents via event_dispatcher."""

    def stop(self, join: bool = True):
        self._stop_event.set()
        if join and self.is_alive():
            self.join(timeout=2)

    def wait_ready(self, timeout: float = 2.0):
        self._started.wait(timeout)


class FilesystemEventWatcher(BaseWatcher):
    """Monitors a folder for new/modified files and emits NEW_FILE /
    DOWNLOAD_COMPLETED events."""

    name = "filesystem-watcher"

    def __init__(self, folder: str | Path, *, interval_s: float = 5.0):
        super().__init__(interval_s=interval_s)
        self.folder = Path(folder).expanduser().resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self._known: dict[str, float] = {}

    def _snapshot(self) -> dict[str, float]:
        snap: dict[str, float] = {}
        for path in self.folder.iterdir():
            try:
                if path.is_file():
                    snap[str(path)] = path.stat().st_mtime
            except OSError:
                continue
        return snap

    def check(self):
        current = self._snapshot()
        for path, mtime in current.items():
            if path not in self._known:
                event_type = DOWNLOAD_COMPLETED if "download" in Path(path).parts[-2].lower() else NEW_FILE
                event_dispatcher.emit(make_event(
                    source="filesystem",
                    event_type=event_type,
                    content=path,
                    metadata={"path": path, "mtime": mtime, "folder": str(self.folder)},
                ))
                logger.info("[Events] %s: %s", event_type, Path(path).name)
            elif self._known[path] != mtime:
                event_dispatcher.emit(make_event(
                    source="filesystem",
                    event_type=NEW_FILE,
                    content=path,
                    metadata={"path": path, "mtime": mtime, "folder": str(self.folder), "modified": True},
                ))
        self._known = current


class WatcherRegistry:
    """Keeps track of running watchers so the app can start/stop them
    cleanly on startup/shutdown."""

    def __init__(self):
        self._watchers: list[BaseWatcher] = []

    def start(self, watcher: BaseWatcher) -> None:
        if any(w is watcher for w in self._watchers):
            return
        watcher.start()
        self._watchers.append(watcher)

    def stop_all(self) -> None:
        for watcher in self._watchers:
            watcher.stop()
        self._watchers.clear()

    @property
    def active_count(self) -> int:
        return len(self._watchers)


watcher_registry = WatcherRegistry()