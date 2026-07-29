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


def test_agent_loop_forces_real_step_when_done_only_narrates_action():
    """Reproduces the reported bug: the LLM says 'I will now proceed with
    opening Google Flights...' and marks done=true with no step. The loop
    must NOT accept that as finished or speak the bogus promise -- it
    should reject it and get the model to return the actual tool call."""
    loop, brain, spoken, recorded = _make_loop([
        {
            "reasoning": "stalling",
            "response": "I will now proceed with opening Google Flights to search for flights.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
        {
            "reasoning": "actually act",
            "response": "Searching Google Flights for Mumbai to Singapore.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_open_tab", "arguments": {"url": "https://flights.google.com"}},
        },
        {
            "reasoning": "done",
            "response": "Found some options.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})) as mock_run:
        result = loop.run("Book a flight from Mumbai to Singapore from September 10-15 under 25k.")

    assert mock_run.call_count == 1
    assert mock_run.call_args[0][0] == "browser_open_tab"
    assert "I will now proceed" not in spoken
    assert "Searching Google Flights for Mumbai to Singapore." in spoken
    assert result is None  # genuinely finished, nothing left to resume


def test_agent_loop_forces_real_step_when_ask_user_only_narrates_action():
    """Same trap, but via ask_user=true instead of done=true -- the model
    treats a normal, non-destructive action as if it needed permission."""
    loop, brain, spoken, recorded = _make_loop([
        {
            "reasoning": "stalling",
            "response": "I'll now go ahead and open YouTube for you.",
            "done": False,
            "ask_user": True,
            "step": None,
        },
        {
            "reasoning": "actually act",
            "response": "Opening YouTube.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_open", "arguments": {"url": "https://youtube.com"}},
        },
        {
            "reasoning": "done",
            "response": "YouTube is open.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, "opened")) as mock_run:
        result = loop.run("open youtube")

    mock_run.assert_called_once_with("browser_open", {"url": "https://youtube.com"})
    assert "I'll now go ahead" not in spoken
    assert result is None


def test_agent_loop_does_not_reject_genuine_direct_answers():
    """Guard against over-triggering: a plain informational answer with
    done=true/step=null (no tool ever needed) must still work exactly as
    before -- this is legitimate, not a stall."""
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "general knowledge",
            "response": "The capital of Japan is Tokyo.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool") as mock_run:
        result = loop.run("What is the capital of Japan?")

    mock_run.assert_not_called()
    assert spoken == ["The capital of Japan is Tokyo."]
    assert result is None


def test_agent_loop_returns_resumable_state_on_genuine_ask_user():
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "Ambiguous request",
            "response": "What do you mean by old?",
            "done": False,
            "ask_user": True,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool"):
        result = loop.run("delete my old videos")

    assert result == {"goal": "delete my old videos", "observations": []}


def test_agent_loop_resume_goal_keeps_original_goal_for_planner():
    """When the caller passes back resume_goal/resume_observations (as
    Agent does after a genuine ask_user pause), the NEXT call must plan
    against the ORIGINAL goal -- not treat the follow-up reply ("yes") as
    a brand new, context-free goal."""
    prior_observations = [{"step": {"tool": "browser_open_tab", "arguments": {}}, "success": True, "result": "ok"}]

    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "resuming",
            "response": "Continuing.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool"):
        result = loop.run(
            "yes",
            resume_goal="Book a flight from Mumbai to Singapore from September 10-15 under 25k.",
            resume_observations=prior_observations,
        )

    call_kwargs = brain.think.call_args_list[0].kwargs
    assert call_kwargs["goal"] == "Book a flight from Mumbai to Singapore from September 10-15 under 25k."
    assert call_kwargs["observations"] == prior_observations
    assert result is None


def test_agent_loop_does_not_flag_genuine_clarifying_question_as_a_stall():
    """'Let me clarify...' is a real question, not a narrated-but-unexecuted
    action -- the guard must not swallow it into an endless retry loop."""
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "need info",
            "response": "Let me clarify -- do you want economy or business class?",
            "done": False,
            "ask_user": True,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool") as mock_run:
        result = loop.run("book me a flight")

    mock_run.assert_not_called()
    assert spoken == ["Let me clarify -- do you want economy or business class?"]
    assert brain.think.call_count == 1
    assert result == {"goal": "book me a flight", "observations": []}


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
