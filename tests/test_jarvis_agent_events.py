from unittest.mock import MagicMock, patch

from brain.agent_loop import AgentLoop


def _loop(events):
    brain = MagicMock()
    loop = AgentLoop(
        brain,
        speak=lambda message: None,
        record_tool=lambda tool, result: None,
        emit_event=lambda event_type, payload=None: events.append((event_type, payload or {})),
    )
    loop.task_state = loop.runtime.start("Open the requested site")
    return loop


def test_verified_tool_success_emits_started_then_finished():
    events = []
    loop = _loop(events)
    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True, "url": "https://example.com"})):
        outcome = loop._execute_tool_step("open example", {"tool": "browser_open", "arguments": {"url": "https://example.com"}})
    assert outcome["success"] is True
    assert [item[0] for item in events] == ["tool_started", "tool_finished"]
    assert events[-1][1]["verified"] is True


def test_verified_tool_failure_is_not_reported_as_success():
    events = []
    loop = _loop(events)
    with patch("brain.agent_loop.run_tool", return_value=(False, {"success": False, "error": "bridge offline", "retryable": True})):
        outcome = loop._execute_tool_step("open example", {"tool": "browser_open", "arguments": {"url": "https://example.com"}})
    assert outcome["success"] is False
    assert events[-1][0] == "tool_failed"
    assert events[-1][1]["error"] == "bridge offline"
    assert events[-1][1]["verified"] is True


def test_risky_tool_emits_confirmation_before_execution():
    events = []
    loop = _loop(events)
    with patch("brain.agent_loop.run_tool") as execute:
        outcome = loop._execute_tool_step("delete draft", {"tool": "file_delete", "arguments": {"path": "draft.txt"}})
    execute.assert_not_called()
    assert outcome["needs_user"] is True
    assert events[-1][0] == "confirmation_required"
    assert events[-1][1]["target"] == "{'path': 'draft.txt'}"


def test_unexpected_executor_exception_emits_truthful_failure():
    events = []
    loop = _loop(events)
    with patch("brain.agent_loop.run_tool", side_effect=RuntimeError("executor crashed")):
        try:
            loop._execute_tool_step("open example", {"tool": "browser_open", "arguments": {"url": "https://example.com"}})
        except RuntimeError:
            pass
    assert events[-1][0] == "tool_failed"
    assert events[-1][1]["verified"] is False
    assert events[-1][1]["error"] == "executor crashed"

