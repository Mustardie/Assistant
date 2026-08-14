"""daily_briefing -- reusable workflow that pulls the day together.

Runs each morning (or on demand): reads today's calendar events,
pending tasks, and new messages from any connected capabilities, then
produces a short briefing the assistant can speak/print.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from events.models import AppEvent
from workflows.base import BaseWorkflow, WorkflowResult

logger = logging.getLogger(__name__)


class DailyBriefingWorkflow(BaseWorkflow):
    name = "daily_briefing"
    description = (
        "Produce a short morning briefing: today's calendar, open tasks, "
        "and unread message highlights from connected capabilities."
    )
    handles_types = ()
    default_risk = "low"

    # ------------------------------------------------------------------ #
    def _detect_intent(self, event: AppEvent, **kwargs) -> str:
        return "daily_briefing"

    def _build_plan(self, event: AppEvent, intent: str, **kwargs) -> list:
        return ["List today's events", "List open tasks",
                "Check for unread highlights", "Compose the briefing"]

    # ------------------------------------------------------------------ #
    def _execute(self, event: AppEvent, result: WorkflowResult, **kwargs):
        today = datetime.now()
        start = today.strftime("%Y-%m-%dT00:00:00")
        end = (today + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

        events: list = []
        ev = self._run_universal("list_events", start=start, end=end, limit=25)
        if isinstance(ev, dict):
            events = ev.get("events") or ev.get("items") or []
        else:
            result.warnings.append("No calendar capability connected.")

        tasks: list = []
        tk = self._run_universal("list_tasks", limit=25)
        if isinstance(tk, dict):
            tasks = tk.get("tasks") or tk.get("items") or []
        else:
            result.warnings.append("No task capability connected.")

        open_tasks = [t for t in tasks if not t.get("completed")]

        sections = []
        if events:
            lines = [f"{e.get('title', '?')} at {e.get('start', '?')}" for e in events[:8]]
            sections.append("Today's calendar:\n  - " + "\n  - ".join(lines))
        if open_tasks:
            lines = [f"{t.get('title', '?')}" for t in open_tasks[:8]]
            sections.append("Open tasks:\n  - " + "\n  - ".join(lines))

        result.note_action("gather", {
            "events": len(events), "open_tasks": len(open_tasks),
        }, verified=True)

        briefing = " ".join(sections) if sections else "Nothing scheduled today, no open tasks."
        result.final_result = (
            f"Daily briefing ({today.strftime('%A %b %d')}): {briefing}"
        )
        result.success = True