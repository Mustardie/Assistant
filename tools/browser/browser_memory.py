import time
from collections import deque


class BrowserMemory:
    def __init__(self, max_history_per_tab: int = 30, max_failures: int = 20):
        self.max_history_per_tab = max_history_per_tab
        self.max_failures = max_failures

        self._history: dict[str, deque] = {}
        self._failures: deque = deque(maxlen=max_failures)
        self.current_task: str = ""
        self.progress_notes: list[str] = []

    # ------------------------------------------------------------------ #
    # Task / progress
    # ------------------------------------------------------------------ #

    def set_task(self, description: str) -> None:
        self.current_task = description
        self.progress_notes = []

    def add_progress(self, note: str) -> None:
        self.progress_notes.append(note)

    # ------------------------------------------------------------------ #
    # Navigation history
    # ------------------------------------------------------------------ #

    def record_navigation(self, tab_id: str, url: str, title: str = "") -> None:
        history = self._history.setdefault(tab_id, deque(maxlen=self.max_history_per_tab))
        history.append({"url": url, "title": title, "at": time.time()})

    def get_history(self, tab_id: str) -> list:
        return list(self._history.get(tab_id, []))

    # ------------------------------------------------------------------ #
    # Failures
    # ------------------------------------------------------------------ #

    def record_failure(self, tab_id: str, action: str, description: str, error: str) -> None:
        self._failures.append({
            "tab_id": tab_id, "action": action, "description": description,
            "error": error, "at": time.time(),
        })

    def recent_failures(self, limit: int = 10) -> list:
        return list(self._failures)[-limit:]

    def has_recently_failed(self, action: str, description: str, within_last: int = 3) -> bool:
        recent = list(self._failures)[-within_last:]
        return any(f["action"] == action and f["description"] == description for f in recent)

    # ------------------------------------------------------------------ #
    # State summary for the reasoning loop
    # ------------------------------------------------------------------ #

    def get_state(self, tabs_snapshot: list) -> dict:
        return {
            "current_task": self.current_task,
            "progress_notes": self.progress_notes,
            "open_tabs": tabs_snapshot,
            "recent_failures": self.recent_failures(),
        }
