from brain.intent_router import Intent, IntentRouter
from brain.planning import Planner
from brain.runtime import AgentRuntime
from brain.state import TaskStatus
from tools.contracts import ToolDecisionLayer, ToolResult, ToolResultVerifier, ToolStatus


def _echo(message):
    return {"success": True, "message": message}


def _delete(path, confirm=False):
    return {"success": confirm, "path": path}


def test_router_does_not_choose_tools_for_direct_question():
    route = IntentRouter().route("Explain photosynthesis")
    assert route.intent == Intent.QUESTION
    assert route.can_answer_directly
    assert route.likely_required_tools == []


def test_router_builds_structured_local_assignment_intent():
    route = IntentRouter().route("Find my assignment and help me finish it")
    assert route.intent == Intent.LOCAL_TASK
    assert route.confidence >= 0.8
    assert route.likely_required_tools == ["file_search"]
    assert route.memory_relevant


def test_router_only_clarifies_genuinely_vague_request():
    route = IntentRouter().route("do it")
    assert route.intent == Intent.AMBIGUOUS
    assert route.clarification_needed
    assert route.missing_info


def test_planner_is_small_and_direct_when_no_tool_is_needed():
    route = IntentRouter().route("What is a binary tree?")
    plan = Planner().create("What is a binary tree?", route)
    assert len(plan.steps) == 1
    assert plan.steps[0].id == "answer"
    assert plan.tools_needed == []


def test_tool_decision_validates_arguments_and_confirmation():
    layer = ToolDecisionLayer({"echo": _echo, "file_delete": _delete})
    missing = layer.assess("echo", {})
    assert not missing.allowed
    assert "Missing required" in missing.reason

    risky = layer.assess("file_delete", {"path": "x.txt"})
    assert not risky.allowed
    assert risky.requires_confirmation

    confirmed = layer.assess("file_delete", {"path": "x.txt"}, confirmed=True)
    assert confirmed.allowed
    assert confirmed.arguments["confirm"] is True


def test_result_normalization_never_turns_reported_failure_into_success():
    result = ToolResult.from_legacy("demo", True, {"success": False, "error": "nope"})
    assert result.status == ToolStatus.ERROR
    assert not result.success
    verification = ToolResultVerifier().verify(result)
    assert not verification.succeeded
    assert verification.should_continue


def test_empty_search_result_is_not_enough_to_claim_completion():
    result = ToolResult.from_legacy("file_search", True, [])
    verification = ToolResultVerifier().verify(result)
    assert result.status == ToolStatus.EMPTY
    assert result.success
    assert not verification.sufficient
    assert verification.retryable


def test_runtime_tracks_state_and_resumes_confirmation():
    runtime = AgentRuntime({"file_delete": _delete})
    state = runtime.start("delete the file x.txt")
    assessment = runtime.assess_tool(state, {"tool": "file_delete", "arguments": {"path": "x.txt"}})
    assert assessment.requires_confirmation
    assert state.status == TaskStatus.WAITING_FOR_USER
    resolved = runtime.resume(state, "yes")
    assert resolved["accepted"] is True
    assert resolved["tool"] == "file_delete"

