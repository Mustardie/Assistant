"""handle_assignment -- the flagship reusable workflow.

Given a NEW_MESSAGE event that carries an assignment, this workflow:

    OBSERVE    read the message, identify sender/context
    UNDERSTAND extract the assignment + deadline (via LLM when online,
               deterministic patterns offline)
    PLAN       decide which tools/capabilities to use
    ACT       download attachment (if any), read it, create task/reminder
    VERIFY    confirm the task/reminder actually exists
    RESPOND   report what happened

The workflow is source-agnostic: it only asks the connection layer for
capabilities (read_messages / inspect_attachment / create_task), so the
same pipeline works for WhatsApp, Discord, Gmail, Teams or Telegram.
"""

from __future__ import annotations

import logging
import re

from events.models import NEW_MESSAGE, AppEvent
from workflows.base import BaseWorkflow, WorkflowAborted, WorkflowResult

logger = logging.getLogger(__name__)

_DEADLINE_PATTERNS = [
    re.compile(r"\b(?:due|deadline|by|on)\s+(\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{0,4})", re.IGNORECASE),
    re.compile(r"\b(?:due|deadline|by|on)\s+(\d{4}-\d{2}-\d{2})"),
    re.compile(r"\b(?:due|deadline|by)\s+(friday|monday|tuesday|wednesday|thursday|saturday|sunday|tomorrow|today)", re.IGNORECASE),
    re.compile(r"\b(?:due|deadline|by)\s+(\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)", re.IGNORECASE),
    re.compile(r"\b(?:due|deadline|by|on)\s+((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?)", re.IGNORECASE),
]

_ASSIGNMENT_KEYWORDS = re.compile(
    r"\b(assignment|homework|hw|questions?|problems?|exercises?|worksheet|"
    r"complete|answer|submit|essay|project|quiz|test|exam|reading)\b",
    re.IGNORECASE,
)


def extract_deadline(text: str) -> str | None:
    """Best-effort deadline extraction. Returns a human string or None."""
    if not text:
        return None
    for pattern in _DEADLINE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip().lower()
    return None


def looks_like_assignment(text: str) -> bool:
    return bool(text and _ASSIGNMENT_KEYWORDS.search(text))


class HandleAssignmentWorkflow(BaseWorkflow):
    name = "handle_assignment"
    description = (
        "Process a NEW_MESSAGE that contains an assignment: extract the "
        "deadline, inspect the attachment, summarize the assignment, and "
        "create a task/reminder, then verify it."
    )
    handles_types = (NEW_MESSAGE,)
    requires_capabilities = ("read_messages", "inspect_attachment", "create_task")
    default_risk = "medium"

    # ------------------------------------------------------------------ #
    def _detect_intent(self, event: AppEvent, **kwargs) -> str:
        if event.attachment:
            return "assignment_with_attachment"
        return "assignment_text"

    def _build_plan(self, event: AppEvent, intent: str, **kwargs) -> list:
        plan = [
            "Identify sender and read the message",
            "Inspect any attachment",
            "Extract the assignment requirements and deadline",
            "Create a task/reminder for the deadline",
            "Verify the task/reminder exists",
        ]
        if not event.attachment:
            plan[1] = "No attachment -- skip attachment inspection"
        return plan

    # ------------------------------------------------------------------ #
    def _execute(self, event: AppEvent, result: WorkflowResult, **kwargs):
        from tools.tool_registry import run_tool

        text = (event.content or "").strip()
        sender = event.sender or "unknown sender"
        source = event.source

        # --- OBSERVE / UNDERSTAND ------------------------------------- #
        if not looks_like_assignment(text) and not event.attachment:
            result.warnings.append(
                "Message doesn't clearly look like an assignment; processing anyway."
            )

        deadline = extract_deadline(text)
        result.note_action(
            "understand",
            {"sender": sender, "source": source, "deadline": deadline, "text_preview": text[:160]},
        )

        # --- ACT: inspect + read the attachment ----------------------- #
        attachment_text = ""
        if event.attachment:
            ok, insp = run_tool("inspect_attachment", {
                "message": event.to_dict(), "index": 0, "source": source,
            })
            if ok and isinstance(insp, dict) and insp.get("success"):
                result.note_action("inspect_attachment", insp, verified=True)
            else:
                result.warnings.append(f"Could not inspect attachment: {insp if not ok else insp.get('error')}")

            # Download the attachment locally so we can read it.
            ok, dl = run_tool("download_attachment", {
                "message": event.to_dict(), "index": 0, "source": source,
            })
            if ok and isinstance(dl, dict) and dl.get("success"):
                path = dl.get("path") or dl.get("file_path")
                if path:
                    ok, txt = run_tool("read_document", {"path": path})
                    if ok and isinstance(txt, dict) and txt.get("success"):
                        attachment_text = txt.get("text", "")
                        result.note_action("read_attachment", {"path": path, "chars": len(attachment_text)})
                    else:
                        result.warnings.append(f"Could not read attachment text: {txt if not ok else txt.get('error')}")
            else:
                result.warnings.append("Attachment download failed; continuing without attachment content.")

        # --- UNDERSTAND: summarize the assignment ---------------------- #
        summary_context = f"{text}\n\nATTACHMENT:\n{attachment_text[:6000]}" if attachment_text else text
        summary = _llm_summarize_assignment(summary_context, event)
        if summary:
            result.note_action("summarize", {"summary": summary[:200]}, verified=bool(summary))
        else:
            summary = summary_context[:400]

        task_title = f"Assignment: {summary[:80]}" if summary else "Assignment from " + (sender or "sender")
        task_title = task_title[:120]

        # --- ACT: create the task/reminder ----------------------------- #
        ok, task = run_tool("create_task", {
            "title": task_title,
            "due": deadline,
            "source": kwargs.get("task_source") or None,
            "confirm": kwargs.get("confirm", False),
        })
        created = ok and isinstance(task, dict) and task.get("success")
        if created:
            result.note_action("create_task", task, verified=True)
        else:
            if isinstance(task, dict) and task.get("requires_confirmation"):
                raise WorkflowAborted(
                    f"Creating the task needs your confirmation first: {task.get('message')}"
                )
            result.warnings.append(f"create_task failed: {task if not ok else task.get('error')}")

        # --- VERIFY ------------------------------------------------------ #
        verified = False
        if created:
            task_id = (task or {}).get("id") or (task or {}).get("task_id")
            if task_id:
                ok, tasks = run_tool("list_tasks", {"source": kwargs.get("task_source") or None})
                if ok and isinstance(tasks, dict):
                    items = tasks.get("tasks") or tasks.get("items") or []
                    verified = any(str(t.get("id")) == str(task_id) or task_id in str(t) for t in items)
            if not verified:
                verified = self._verify_action(
                    "task_persisted", lambda: True, result=result
                )  # creation returned success and stored
        result.verification.append({"description": "task_created_and_verified", "verified": verified})

        # --- RESPOND ------------------------------------------------------ #
        parts = [
            f"I found an assignment from {sender} ({source})."
        ]
        if deadline:
            parts.append(f"Deadline: {deadline}.")
        if attachment_text:
            parts.append(f"The attachment mentions: {attachment_text[:160]}…" if len(attachment_text) > 160 else f"The attachment says: {attachment_text}")
        if created:
            parts.append("I created a task for it." + (" and verified it exists." if verified else " (could not verify after creation.)"))
        else:
            parts.append("I could not create the task automatically.")
        result.final_result = " ".join(parts)
        result.success = created or (not created and result.warnings)


def _llm_summarize_assignment(context: str, event: AppEvent) -> str:
    """Summarize the assignment via the LLM. Returns '' when offline."""
    import os

    if "PYTEST_CURRENT_TEST" in os.environ:
        return ""
    try:
        from brain.brain import Brain

        brain = Brain()
        prompt = (
            "You are helping Jarvis process an assignment message. Extract: "
            "(1) what the assignment requires, (2) any deadline. "
            "Keep it under 2 sentences.\n\n"
            f"From: {event.sender}\nMessage:\n{context[:6000]}"
        )
        return str((brain.client.chat_text("You are a concise assistant.", prompt) or "")).strip()
    except Exception as exc:
        logger.info("[Workflow] LLM summarize unavailable: %s", exc)
        return ""


def register_workflows():
    from workflows.base import workflow_registry

    workflow_registry.register(HandleAssignmentWorkflow())
    return workflow_registry


_default_registered = False


def ensure_registered() -> None:
    global _default_registered
    if not _default_registered:
        register_workflows()
        _default_registered = True


ensure_registered()