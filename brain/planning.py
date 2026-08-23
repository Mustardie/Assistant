"""Small, practical plans derived from the intent router."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from brain.intent_router import IntentDecision


@dataclass(frozen=True)
class PlanStep:
    id: str
    action: str
    tools: list[str] = field(default_factory=list)
    expected_output: str = ""


@dataclass(frozen=True)
class TaskPlan:
    goal: str
    assumptions: list[str]
    steps: list[PlanStep]
    tools_needed: list[str]
    expected_outputs: list[str]
    success_criteria: list[str]
    fallback_strategy: list[str]
    confirmation_required: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def prompt_hint(self) -> str:
        steps = " | ".join(f"{step.id}:{step.action}" for step in self.steps)
        success = "; ".join(self.success_criteria)
        fallback = "; ".join(self.fallback_strategy)
        return f"goal={self.goal}; plan={steps}; success={success}; fallback={fallback}"


class Planner:
    def create(self, request: str, route: IntentDecision) -> TaskPlan:
        if route.clarification_needed:
            steps = [PlanStep("clarify", f"Ask only for: {', '.join(route.missing_info)}")]
            expected = ["the missing information needed to proceed"]
        elif route.can_answer_directly:
            steps = [PlanStep("answer", "Answer directly without calling a tool", expected_output="complete answer")]
            expected = ["a complete, relevant answer"]
        else:
            steps = [
                PlanStep("inspect", "Gather only the state or data required for the goal", route.likely_required_tools, "relevant evidence"),
                PlanStep("act", "Perform the minimum safe action that advances the goal", route.likely_required_tools, "requested change or result"),
                PlanStep("verify", "Verify the result against the user's request", [], "evidence of completion"),
                PlanStep("finish", "Answer with the outcome, or the exact blocker", [], "truthful final response"),
            ]
            expected = ["the requested outcome", "verification evidence"]

        confirmation = any("confirmation" in note for note in route.safety_notes)
        return TaskPlan(
            goal=request.strip(),
            assumptions=["Use information already present in the request before asking the user"],
            steps=steps,
            tools_needed=list(route.likely_required_tools),
            expected_outputs=expected,
            success_criteria=["the user-visible outcome is complete", "no tool failure is represented as success"],
            fallback_strategy=[
                "correct obvious arguments and retry once",
                "use a different suitable capability when available",
                "ask the user only when required information or authority is missing",
            ],
            confirmation_required=confirmation,
        )

