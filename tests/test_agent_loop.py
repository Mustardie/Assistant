import json
from unittest.mock import MagicMock, patch

import pytest

from brain.agent_loop import AgentLoop


def _make_loop(brain_decisions, *, speak=None, record_tool=None, fallback=None):
    brain = MagicMock()
    brain.think.side_effect = brain_decisions
    brain.recover.return_value = {
        "reasoning": "recovery",
        "response": "Trying another way.",
        "done": False,
        "ask_user": False,
        "step": None,
    }

    spoken = []
    recorded = []

    loop = AgentLoop(
        brain,
        speak=speak or spoken.append,
        record_tool=record_tool or (lambda tool, result: recorded.append((tool, result))),
        fallback=fallback,
    )
    return loop, brain, spoken, recorded


def test_agent_loop_executes_single_step_and_stops_when_done():
    loop, brain, spoken, recorded = _make_loop([
        {
            "reasoning": "Open browser",
            "response": "Opening YouTube.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_open", "arguments": {"url": "https://youtube.com"}},
        },
        {
            "reasoning": "Done",
            "response": "YouTube is open.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, "opened")) as mock_run:
        loop.run("open youtube")

    assert mock_run.call_count == 1
    assert mock_run.call_args[0] == ("browser_open", {"url": "https://youtube.com"})
    assert recorded == [("browser_open", "opened")]
    assert "Opening YouTube." in spoken
    assert "YouTube is open." in spoken
    assert brain.think.call_count == 2


def test_agent_loop_asks_user_and_stops():
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "Ambiguous request",
            "response": "What do you mean by old?",
            "done": False,
            "ask_user": True,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool") as mock_run:
        loop.run("delete my old videos")

    mock_run.assert_not_called()
    assert spoken == ["What do you mean by old?"]
    assert brain.think.call_count == 1


def test_agent_loop_does_not_repeat_failed_action():
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "Try file open",
            "response": "Opening file.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "file_open", "arguments": {"path": "/missing"}},
        },
        {
            "reasoning": "Retry same",
            "response": "Retrying.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "file_open", "arguments": {"path": "/missing"}},
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(False, "not found")):
        loop.run("open my file")

    assert any("stop repeating" in msg.lower() for msg in spoken)


def test_agent_loop_uses_fallback_when_no_step_on_first_turn():
    fallback_called = []

    def fallback(user, hint):
        fallback_called.append((user, hint))
        return True

    loop, brain, _, _ = _make_loop(
        [{"reasoning": "unsure", "response": "", "done": False, "ask_user": False, "step": None}],
        fallback=fallback,
    )

    with patch("brain.agent_loop.run_tool"):
        loop.run("find my notes", intent_hint="file hint")

    assert fallback_called == [("find my notes", "file hint")]


def test_agent_loop_passes_observations_on_subsequent_turns():
    loop, brain, _, _ = _make_loop([
        {
            "reasoning": "Search first",
            "response": "Searching.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "file_search", "arguments": {"query": "notes"}},
        },
        {
            "reasoning": "Open result",
            "response": "Opening.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    search_result = {"status": "ok", "intent": "open_file", "result": {"path": "/notes.pdf"}}

    with patch("brain.agent_loop.run_tool", return_value=(True, search_result)):
        loop.run("open my notes")

    second_call_kwargs = brain.think.call_args_list[1].kwargs
    observations = second_call_kwargs.get("observations") or brain.think.call_args_list[1][1].get("observations")
    if observations is None:
        observations = brain.think.call_args_list[1].kwargs.get("observations")
    assert len(observations) == 1
    assert observations[0]["step"]["tool"] == "file_search"
    assert observations[0]["result"] == search_result
