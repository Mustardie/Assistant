"""handle_new_email -- reusable workflow for incoming email.

OBSERVE    read the email body + subject
UNDERSTAND detect urgency / action items (due dates, tasks, requests)
PLAN       decide what to do (draft reply, create task, summarize)
ACT       create task for anything actionable, summarize the rest
VERIFY    confirm the task/summary landed
RESPOND   report

Works for any connected mail capability (Gmail, Outlook, ...).
"""

from __future__ import annotations

import logging

from events.models import NEW_EMAIL, AppEvent
from workflows.base import BaseWorkflow, WorkflowResult

logger = logging.getLogger(__name__)


class HandleNewEmailWorkflow(BaseWorkflow):
    name = "handle_new_email"
    description = (
        "Process a NEW_EMAIL: summarize what it needs, create tasks for "
        "any action items, and flag urgent messages."
    )
    handles_types = (NEW_EMAIL,)
    requires_capabilities = ("read_messages", "create_task")
    default_risk = "medium"

    _URGENT_WORDS = ("urgent", "asap", "as soon as possible", "deadline",
                     "overdue", "immediately", "action required", "final notice")

    # ------------------------------------------------------------------ #
    def _detect_intent(self, event: AppEvent, **kwargs) -> str:
        text = (event.content or "").lower()
        if any(w in text for w in self._URGENT_WORDS):
            return "urgent_email"
        if "?" in (event.content or "") or any(
            w in text for w in ("please", "could you", "can you", "need")
        ):
            return "actionable_email"
        return "informational_email"

    def _build_plan(self, event: AppEvent, intent: str, **kwargs) -> list:
        if intent == "informational_email":
            return ["Summarize the email", "No tasks to create"]
        return ["Read the email", "Extract action items", "Create a task", "Verify"]

    # ------------------------------------------------------------------ #
    def _execute(self, event: AppEvent, result: WorkflowResult, **kwargs):
        text = (event.content or "").strip()
        sender = event.sender or "unknown"
        subject = (event.metadata or {}).get("subject", "")
        intent = result.intent

        result.note_action("understand", {
            "intent": intent, "sender": sender, "subject": subject,
            "urgent": intent == "urgent_email",
        })

        if intent == "informational_email":
            result.final_result = (
                f"Email from {sender} is informational. "
                f"Summary: {text[:300] or '(no body)'}"
            )
            result.success = True
            return

        # ACTIONABLE / URGENT -> create a task.
        task_title = subject or (sender + " email")
        task = self._run_universal("create_task", title=task_title[:120],
                                   due=(event.metadata or {}).get("due"))
        if isinstance(task, dict) and task.get("success"):
            result.note_action("create_task", task, verified=True)
            result.verification.append({"description": "task_created", "verified": True})
            result.final_result = (
                f"From {sender} ({'URGENT' if intent == 'urgent_email' else 'actionable'}). "
                f"Created task: {task.get('title', task_title)}"
            )
            result.success = True
        else:
            result.warnings.append(f"create_task failed: {task}")
            result.final_result = (
                f"Could not auto-create the task from {sender}'s email. "
                f"Action required: {text[:300]}"
            )
            result.success = False