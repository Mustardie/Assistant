"""Runtime task state for the general JARVIS decision loop."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETE = "complete"
    BLOCKED = "blocked"


@dataclass
class AgentState:
    goal: str = ""
    intent: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    completed_steps: list[dict] = field(default_factory=list)
    failed_steps: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    results_gathered: list[Any] = field(default_factory=list)
    pending_decisions: list[dict] = field(default_factory=list)
    next_action: str | None = None
    final_answer_ready: bool = False
    status: TaskStatus = TaskStatus.PLANNING
    history: list = field(default_factory=list)
    last_action: dict | None = None
    last_result: object = None
    finished: bool = False

    def reset(self):
        fresh = type(self)()
        self.__dict__.update(fresh.__dict__)

    def add_action(self, action, result):
        self.last_action = action
        self.last_result = result
        self.history.append({"action": action, "result": result})

    def record_tool(self, step: dict, result: dict) -> None:
        entry = {"step": step, "result": result}
        self.tool_calls.append(entry)
        self.results_gathered.append(result)
        self.add_action(step, result)
        if result.get("success"):
            self.completed_steps.append(entry)
        else:
            self.failed_steps.append(entry)
        self.status = TaskStatus.RUNNING

    def require_decision(self, decision: dict) -> None:
        if decision not in self.pending_decisions:
            self.pending_decisions.append(decision)
        self.next_action = "await_user_confirmation"
        self.status = TaskStatus.WAITING_FOR_USER

    def consume_confirmation(self, accepted: bool) -> dict | None:
        if not self.pending_decisions:
            return None
        decision = self.pending_decisions.pop(0)
        decision = {**decision, "accepted": accepted}
        self.status = TaskStatus.RUNNING if accepted else TaskStatus.BLOCKED
        self.next_action = None
        return decision

    def mark_complete(self) -> None:
        self.status = TaskStatus.COMPLETE
        self.final_answer_ready = True
        self.finished = True
        self.next_action = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data
