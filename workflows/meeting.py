"""schedule_event -- reusable workflow for meeting/event requests.

OBSERVE    an event/request text (from message or email)
UNDERSTAND extract title, date, time, attendees
PLAN       create the calendar event, then set a reminder
ACT       create_event on whatever calendar capability exists
VERIFY    list_events to confirm the event exists
RESPOND   report with the event id and time
"""

from __future__ import annotations

import logging
import re

from events.models import AppEvent
from workflows.base import BaseWorkflow, WorkflowResult

logger = logging.getLogger(__name__)


def extract_datetime(text: str) -> str | None:
    """Best-effort extraction of an ISO-ish start time. Returns None if none."""
    if not text:
        return None
    # ISO / yyyy-mm-ddThh:mm
    match = re.search(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})\b", text)
    if match:
        return match.group(1).replace(" ", "T")
    # yyyy-mm-dd
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if match:
        return match.group(1)
    return None


class ScheduleEventWorkflow(BaseWorkflow):
    name = "schedule_event"
    description = (
        "Turn a request like 'schedule a meeting tomorrow at 3pm' into a "
        "calendar event + reminder on the connected calendar capability."
    )
    handles_types = ()
    default_risk = "medium"

    # ------------------------------------------------------------------ #
    def _detect_intent(self, event: AppEvent, **kwargs) -> str:
        return "schedule_event"

    def _build_plan(self, event: AppEvent, intent: str, **kwargs) -> list:
        return ["Extract title + time", "Create the calendar event",
                "Set a reminder", "Verify the event exists"]

    # ------------------------------------------------------------------ #
    def _execute(self, event: AppEvent, result: WorkflowResult, **kwargs):
        text = (event.content or "").strip()
        title = (event.metadata or {}).get("title") or _title_from_text(text)
        start = (event.metadata or {}).get("start") or extract_datetime(text)
        duration = (event.metadata or {}).get("duration_minutes") or 60

        if not title:
            title = "Meeting"
        if not start:
            result.warnings.append("No start time found; creating without a time.")
            start = None

        event_dict = self._run_universal("create_event", title=title,
                                         start=start, duration_minutes=duration)
        created = isinstance(event_dict, dict) and event_dict.get("success")
        if not created:
            result.warnings.append(f"create_event failed: {event_dict}")
            result.final_result = "Could not schedule the event (no connected calendar)."
            result.success = False
            return

        result.note_action("create_event", event_dict, verified=True)
        event_id = event_dict.get("id") or event_dict.get("event_id")

        # Reminder (best effort).
        self._run_universal("create_reminder", title=f"Reminder: {title}", when=start)
        result.note_action("create_reminder", {"title": f"Reminder: {title}"})

        # VERIFY: list events and find ours.
        verified = False
        events_dict = self._run_universal("list_events", start=start, limit=10)
        if isinstance(events_dict, dict):
            items = events_dict.get("events") or events_dict.get("items") or []
            verified = any(
                str(e.get("id")) == str(event_id)
                or (e.get("title") or "").lower() == title.lower()
                for e in items
            )
        result.verification.append({"description": "event_created_and_verified", "verified": verified})

        when = start or "at the requested time"
        result.final_result = (
            f"Scheduled '{title}' for {when} ({duration} min). "
            f"Event id {event_id}." + (" Verified." if verified else " (could not verify.)")
        )
        result.success = True


def _title_from_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^(schedule|book|create|add|set up)\s+(a|an|the|)\s*(meeting|event|call)\s*(for|on|at)?\s*",
                  "", text, flags=re.IGNORECASE).strip()
    return text[:120] or "Meeting"