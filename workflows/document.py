"""process_document -- reusable workflow for incoming files.

OBSERVE    a NEW_FILE / DOWNLOAD_COMPLETED event with a path
UNDERSTAND read the document (pdf/docx/xlsx/pptx/txt) and summarize
PLAN       summarize + (optional) index into file manager for search
ACT       read the document, extract the text, summarize it
VERIFY    confirm text was extracted
RESPOND   report the summary

Offline-safe: uses stdlib readers + extractive fallback, LLM only when
available.
"""

from __future__ import annotations

import logging
import os

from events.models import NEW_FILE, DOWNLOAD_COMPLETED, AppEvent
from workflows.base import BaseWorkflow, WorkflowResult

logger = logging.getLogger(__name__)


class ProcessDocumentWorkflow(BaseWorkflow):
    name = "process_document"
    description = (
        "Read a newly arrived file (pdf/docx/xlsx/pptx/txt), extract its "
        "text and summarize what it contains."
    )
    handles_types = (NEW_FILE, DOWNLOAD_COMPLETED)
    requires_capabilities = ("read_document",)
    default_risk = "low"

    # ------------------------------------------------------------------ #
    def _detect_intent(self, event: AppEvent, **kwargs) -> str:
        path = event.content or ""
        if path.lower().endswith((".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md")):
            return "document"
        return "unknown_file"

    def _build_plan(self, event: AppEvent, intent: str, **kwargs) -> list:
        if intent == "unknown_file":
            return ["Recognize the file type"]
        return ["Read the document", "Extract text", "Summarize the contents"]

    # ------------------------------------------------------------------ #
    def _execute(self, event: AppEvent, result: WorkflowResult, **kwargs):
        path = event.content or ""
        if not path:
            result.warnings.append("No file path on the event.")
            result.final_result = "No document path provided."
            result.success = False
            return

        if result.intent == "unknown_file":
            result.final_result = f"New file received: {os.path.basename(path)} (unrecognized type)."
            result.success = True
            return

        doc = self._run_universal("read_document", path=path)
        text = ""
        if isinstance(doc, dict) and doc.get("success"):
            text = doc.get("text") or ""
            result.note_action("read_document", {
                "path": path, "chars": len(text), "pages": doc.get("pages"),
            }, verified=bool(text))
        else:
            result.warnings.append(f"Could not read {os.path.basename(path)}: {doc}")
            result.final_result = f"Could not read the document {os.path.basename(path)}."
            result.success = False
            return

        summary = _summarize(text, path)
        result.note_action("summarize", {"summary": summary[:200]}, verified=bool(summary))
        result.final_result = (
            f"Read {os.path.basename(path)} ({len(text)} chars). "
            f"Summary: {summary[:400]}"
        )
        result.success = True


def _summarize(text: str, path: str) -> str:
    """LLM summary when online, extractive (first meaningful lines) offline."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return _extractive_summary(text)
    try:
        from brain.brain import Brain

        brain = Brain()
        prompt = (
            "Summarize the following document contents in 2-3 sentences, "
            "highlighting what is most important.\n\n"
            f"File: {os.path.basename(path)}\n\n{text[:6000]}"
        )
        result = str(brain.client.chat_text("You are a concise summarizer.", prompt) or "").strip()
        return result if result else _extractive_summary(text)
    except Exception as exc:
        logger.info("[Workflow] LLM summarize unavailable: %s", exc)
        return _extractive_summary(text)


def _extractive_summary(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "(empty document)"
    meaningful = [ln for ln in lines if len(ln.split()) >= 3][:3]
    return " / ".join(meaningful) if meaningful else lines[0][:200]