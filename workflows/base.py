"""Reusable WORKFLOW system.

A workflow (a.k.a. skill) is a reusable, source-agnostic pipeline:
it accepts an AppEvent (or structured input), runs tools from ANY
connected adapter, and reports a structured result -- following the
OBSERVE -> UNDERSTAND -> PLAN -> ACT -> VERIFY -> RESPOND loop.

The same workflow works whether the input arrived via WhatsApp, Discord,
Gmail, Teams or Telegram: it never hardcodes an app, only capabilities.

Example (handle_assignment):

    AppEvent(NEW_MESSAGE, sender=..., content=..., attachment=...)
        -> extract sender/context
        -> read message
        -> inspect attachment
        -> extract assignment + deadline
        -> create task/reminder  (MEDIUM risk -> confirm or policy)
        -> verify the task exists
        -> report what happened
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from events.models import AppEvent

logger = logging.getLogger(__name__)


@dataclass
class WorkflowResult:
    """Structured result of running a workflow."""

    workflow: str
    event: dict | None = None
    intent: str = ""
    plan: list = field(default_factory=list)
    actions_completed: list = field(default_factory=list)
    verification: list = field(default_factory=list)
    final_result: str = ""
    success: bool = True
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "workflow": self.workflow,
            "event": self.event,
            "intent": self.intent,
            "plan": self.plan,
            "actions_completed": self.actions_completed,
            "verification": self.verification,
            "final_result": self.final_result,
            "success": self.success,
            "warnings": self.warnings,
        }

    def note_action(self, action: str, result: Any = None, *, verified: bool = False):
        entry = {"action": action}
        if result is not None:
            entry["result"] = result if not isinstance(result, (dict,)) else {
                k: v for k, v in result.items() if k not in ("text",)
            } or {"ok": True}
        if verified:
            entry["verified"] = True
        self.actions_completed.append(entry)


class BaseWorkflow:
    """Base class for reusable workflows."""

    name: str = "base"
    description: str = ""
    # Event types this workflow wants to consume.
    handles_types: tuple = ()
    # Tool capabilities this workflow may require.
    requires_capabilities: tuple = ()
    default_risk: str = "low"

    def __init__(self):
        self._last_result: WorkflowResult | None = None

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #

    def can_handle(self, event: AppEvent) -> bool:
        """Return True when this workflow should process the event."""
        if not self.handles_types:
            return False
        return event.type in self.handles_types

    def run(self, event: AppEvent, **kwargs) -> WorkflowResult:
        """Execute the workflow end-to-end. Subclasses override `_execute`."""
        result = WorkflowResult(workflow=self.name, event=event.to_dict())
        result.intent = self._detect_intent(event, **kwargs)
        result.plan = self._build_plan(event, result.intent, **kwargs)
        try:
            self._execute(event, result, **kwargs)
        except WorkflowAborted as exc:
            result.success = False
            result.final_result = str(exc)
        except Exception as exc:
            logger.exception("[Workflow:%s] crashed", self.name)
            result.success = False
            result.final_result = f"Workflow failed: {exc}"
        self._last_result = result
        return result

    @property
    def last_result(self) -> WorkflowResult | None:
        return self._last_result

    # ------------------------------------------------------------------ #
    # Subclass hooks
    # ------------------------------------------------------------------ #

    def _detect_intent(self, event: AppEvent, **kwargs) -> str:
        return self.name

    def _build_plan(self, event: AppEvent, intent: str, **kwargs) -> list:
        return []

    def _execute(self, event: AppEvent, result: WorkflowResult, **kwargs):
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Helpers for subclasses
    # ------------------------------------------------------------------ #

    @staticmethod
    def _run_tool(tool: str, arguments: dict | None = None):
        from tools.tool_registry import run_tool

        ok, result = run_tool(tool, arguments or {})
        return ok, result

    def _run_universal(self, tool: str, **kwargs) -> dict:
        from tools.tool_registry import run_tool

        ok, result = run_tool(tool, kwargs)
        if not ok:
            return {"success": False, "error": str(result)}
        if isinstance(result, dict):
            return result
        return {"success": True, "result": result}

    def _verify_action(self, description: str, check: Callable[[], bool], *,
                       result: WorkflowResult) -> bool:
        try:
            verified = bool(check())
        except Exception as exc:
            logger.warning("[Workflow:%s] verify '%s' failed: %s", self.name, description, exc)
            verified = False
        result.verification.append({"description": description, "verified": verified})
        return verified


class WorkflowAborted(Exception):
    """Raised inside _execute to abort with a clean message."""


class WorkflowRegistry:
    def __init__(self):
        self._workflows: dict[str, BaseWorkflow] = {}

    def register(self, workflow: BaseWorkflow) -> None:
        self._workflows[workflow.name] = workflow

    def get(self, name: str) -> BaseWorkflow | None:
        return self._workflows.get(name)

    def list(self) -> list[dict]:
        return [
            {"name": w.name, "description": w.description, "handles_types": list(w.handles_types)}
            for w in self._workflows.values()
        ]

    def find_for_event(self, event: AppEvent) -> BaseWorkflow | None:
        """Return the first workflow that handles this event type."""
        for workflow in self._workflows.values():
            if workflow.can_handle(event):
                return workflow
        return None

    def route_event(self, event: AppEvent, **kwargs) -> WorkflowResult | None:
        """Dispatch an event to its workflow (if any) and run it."""
        workflow = self.find_for_event(event)
        if workflow is None:
            return None
        return workflow.run(event, **kwargs)

    def clear(self):
        self._workflows.clear()


workflow_registry = WorkflowRegistry()