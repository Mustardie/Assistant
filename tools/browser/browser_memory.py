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
        self.current_step: int = 0
        self.total_steps: int = 0
        self.completed_steps: list[str] = []
        self._page_summaries: dict[str, list[dict]] = {}
        self._extracted_data: dict[str, dict] = {}
        self._plan: list[str] = []
        self._notes: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Task / progress
    # ------------------------------------------------------------------ #

    def set_task(self, description: str, total_steps: int = 0) -> None:
        self.current_task = description
        self.progress_notes = []
        self.current_step = 0
        self.total_steps = total_steps
        self.completed_steps = []

    def add_progress(self, note: str) -> None:
        self.progress_notes.append(note)

    def complete_step(self, step_description: str) -> None:
        self.current_step += 1
        self.completed_steps.append(step_description)
        self.add_progress(f"Step {self.current_step}/{self.total_steps}: {step_description}")

    # ------------------------------------------------------------------ #
    # Navigation history
    # ------------------------------------------------------------------ #

    def record_navigation(self, tab_id: str, url: str, title: str = "") -> None:
        history = self._history.setdefault(tab_id, deque(maxlen=self.max_history_per_tab))
        history.append({"url": url, "title": title, "at": time.time()})

    def get_history(self, tab_id: str) -> list:
        return list(self._history.get(tab_id, []))

    # ------------------------------------------------------------------ #
    # Page summaries (what was on each page the agent visited)
    # ------------------------------------------------------------------ #

    def record_page_summary(self, tab_id: str, summary: dict) -> None:
        summaries = self._page_summaries.setdefault(tab_id, [])
        summaries.append({
            "url": summary.get("url", ""),
            "title": summary.get("title", ""),
            "page_type": summary.get("page_type", "unknown"),
            "button_count": len(summary.get("interactables", {}).get("buttons", [])),
            "link_count": len(summary.get("interactables", {}).get("links", [])),
            "input_count": len(summary.get("interactables", {}).get("inputs", [])),
            "heading_summary": [h["text"] for h in summary.get("structure", {}).get("headings", [])[:5]],
            "at": time.time(),
        })

    def get_page_summaries(self, tab_id: str) -> list[dict]:
        return self._page_summaries.get(tab_id, [])

    # ------------------------------------------------------------------ #
    # Extracted data (information collected from pages)
    # ------------------------------------------------------------------ #

    def record_extracted_data(self, tab_id: str, key: str, data) -> None:
        tab_data = self._extracted_data.setdefault(tab_id, {})
        tab_data[key] = {"value": data, "at": time.time()}

    def get_extracted_data(self, tab_id: str) -> dict:
        return self._extracted_data.get(tab_id, {})

    def get_all_extracted_data(self) -> dict:
        return dict(self._extracted_data)

    # ------------------------------------------------------------------ #
    # Plan (explicit ordered step list for multi-tab workflows)
    # ------------------------------------------------------------------ #

    def set_plan(self, steps: list[str]) -> None:
        self._plan = list(steps)

    def get_plan(self) -> list[str]:
        return self._plan

    # ------------------------------------------------------------------ #
    # Session notes (cross-tab working memory, e.g. cheapest_flight=₹25k)
    # ------------------------------------------------------------------ #

    def set_note(self, key: str, value: str) -> None:
        self._notes[key] = value

    def get_note(self, key: str) -> str | None:
        return self._notes.get(key)

    def get_all_notes(self) -> dict[str, str]:
        return dict(self._notes)

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
        state = {
            "current_task": self.current_task,
            "progress_notes": self.progress_notes,
            "step": f"{self.current_step}/{self.total_steps}",
            "completed_steps": self.completed_steps,
            "open_tabs": tabs_snapshot,
            "recent_failures": self.recent_failures(),
            "page_summaries": {
                tid: summaries[-3:] if summaries else []
                for tid, summaries in self._page_summaries.items()
            },
        }
        if self._plan:
            state["plan"] = self._plan
            done = set(self.completed_steps)
            state["plan_progress"] = [
                {"step": s, "done": s in done} for s in self._plan
            ]
        if self._notes:
            state["notes"] = dict(self._notes)
        if self._extracted_data:
            state["extracted_data"] = {
                tid: {k: v["value"] for k, v in tab_data.items()}
                for tid, tab_data in self._extracted_data.items()
            }
        return state
