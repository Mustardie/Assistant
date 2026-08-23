"""Coordinator for intent, plan, tool decisions, verification, and state."""

from __future__ import annotations

from brain.intent_router import IntentRouter
from brain.planning import Planner
from brain.state import AgentState, TaskStatus
from tools.contracts import ToolAssessment, ToolDecisionLayer, ToolResult, ToolResultVerifier


_ACCEPT = {"yes", "y", "confirm", "confirmed", "ok", "okay", "go ahead", "do it"}


class AgentRuntime:
    def __init__(self, tools: dict):
        self.router = IntentRouter()
        self.planner = Planner()
        self.decider = ToolDecisionLayer(tools)
        self.verifier = ToolResultVerifier()

    def start(self, goal: str) -> AgentState:
        route = self.router.route(goal)
        plan = self.planner.create(goal, route)
        return AgentState(
            goal=goal,
            intent=route.to_dict(),
            plan=plan.to_dict(),
            next_action=plan.steps[0].id if plan.steps else None,
            status=TaskStatus.RUNNING,
        )

    def resume(self, state: AgentState, answer: str) -> dict | None:
        if not state.pending_decisions:
            return None
        accepted = answer.strip().lower() in _ACCEPT
        return state.consume_confirmation(accepted)

    def prompt_context(self, state: AgentState) -> str:
        intent = state.intent
        plan_steps = " | ".join(f"{step['id']}:{step['action']}" for step in state.plan.get("steps", []))
        likely = ", ".join(intent.get("likely_required_tools", [])) or "none"
        return (
            f"intent={intent.get('intent')}; confidence={intent.get('confidence', 0):.2f}; "
            f"can_answer_directly={str(intent.get('can_answer_directly', False)).lower()}; "
            f"clarification_needed={str(intent.get('clarification_needed', False)).lower()}; "
            f"memory_relevant={str(intent.get('memory_relevant', False)).lower()}; "
            f"likely_tools={likely}; plan={plan_steps}; "
            "Do not call a tool outside the goal merely because it is available."
        )

    def assess_tool(self, state: AgentState, step: dict, *, confirmed: bool = False) -> ToolAssessment:
        assessment = self.decider.assess(
            step.get("tool"), step.get("arguments", {}), confirmed=confirmed
        )
        if assessment.requires_confirmation:
            state.require_decision({
                "kind": "confirmation",
                "tool": assessment.tool,
                "arguments": assessment.arguments,
                "reason": assessment.reason,
            })
        return assessment

    def observe(self, state: AgentState, step: dict, executor_success: bool, raw_result):
        result = ToolResult.from_legacy(step.get("tool", "unknown"), executor_success, raw_result)
        verification = self.verifier.verify(result)
        state.record_tool(step, {**result.to_dict(), "verification": verification.reason})
        state.next_action = "repair" if not verification.succeeded else "continue_or_finish"
        return result, verification

