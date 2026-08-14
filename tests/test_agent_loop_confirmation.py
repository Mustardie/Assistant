"""Tests for the agent loop's risk-confirmation integration.

The confirmation flow uses a minimal fake Brain whose `think()` returns
scripted decisions, and a real AgentLoop wired with a no-op speak. No LLM
or network is involved."""

import logging
import os
from dataclasses import dataclass

import pytest

from brain.agent_loop import AgentLoop, normalize_user_input

logging.basicConfig(level=logging.CRITICAL)


@dataclass
class Noop:
    def __call__(self, *a, **k):
        pass


class ScriptedBrain:
    """think() returns decisions from a script; recover() is a passthrough."""

    def __init__(self, decisions):
        self.decisions = list(decisions)

    def think(self, user, goal=None, observations=None, intent_hint=None, recipe=None):
        if self.decisions:
            return self.decisions.pop(0)
        return {"reasoning": "", "response": "", "done": True, "step": None}

    def recover(self, *a, **k):
        return {"reasoning": "", "done": True, "response": "", "step": None}

    def draft_document(self, topic):
        return ""


def _make_loop(brain) -> AgentLoop:
    loop = AgentLoop(
        brain,
        speak=Noop(),
        record_tool=Noop(),
        on_file_search_result=None,
        track_file_action=None,
        fallback=None,
    )
    return loop


def test_confirmation_pauses_and_reruns_on_yes():
    """A requires_confirmation tool result pauses the loop; the next turn
    ('yes') re-runs the same tool with confirm=True added to the args."""
    calls = []

    def fake_run_tool(tool, arguments):
        calls.append((tool, dict(arguments)))
        if arguments.get("confirm"):
            return True, {"success": True, "id": "t1"}
        return True, {"success": False, "requires_confirmation": True,
                      "message": "Creating the task 'Buy milk' needs your confirmation."}

    import brain.agent_loop as agent_loop_mod

    original = agent_loop_mod.run_tool
    agent_loop_mod.run_tool = fake_run_tool
    try:
        brain = ScriptedBrain([
            {"reasoning": "", "response": "", "done": False,
             "step": {"tool": "create_task", "arguments": {"title": "Buy milk"}}},
        ])
        loop = _make_loop(brain)
        first = loop.run("create a task to buy milk")
        # The loop must pause, not treat it as a terminal failure.
        assert first is not None
        assert first["goal"]
        assert loop._pending_confirmation is not None

        # Second turn: user says yes.
        brain.decisions = []  # confirmed step runs; then done
        loop.run("yes", resume_goal=first["goal"],
                 resume_observations=first["observations"])
    finally:
        agent_loop_mod.run_tool = original

    # The tool was called twice: once blocked, once with confirm=True.
    assert len(calls) == 2
    assert calls[0][0] == "create_task"
    assert calls[0][1].get("confirm") is None
    assert calls[1][0] == "create_task"
    assert calls[1][1].get("confirm") is True


def test_confirmation_declines_with_no():
    calls = []

    def fake_run_tool(tool, arguments):
        calls.append((tool, dict(arguments)))
        return True, {"success": False, "requires_confirmation": True,
                      "message": "Sending a message needs your confirmation."}

    import brain.agent_loop as agent_loop_mod

    original = agent_loop_mod.run_tool
    agent_loop_mod.run_tool = fake_run_tool
    try:
        brain = ScriptedBrain([
            {"reasoning": "", "response": "", "done": False,
             "step": {"tool": "send_message", "arguments": {"recipient": "bob", "text": "hi"}}},
        ])
        loop = _make_loop(brain)
        first = loop.run("send bob a message saying hi")
        assert loop._pending_confirmation is not None

        # Second turn: user says no -> the step is marked declined, never
        # re-run, and the loop continues (never calls the tool again).
        brain.decisions = [{"reasoning": "", "response": "ok", "done": True, "step": None}]
        loop.run("no", resume_goal=first["goal"],
                 resume_observations=first["observations"])
    finally:
        agent_loop_mod.run_tool = original

    # Only the original (blocked) call happened; no confirm=True re-run.
    assert len(calls) == 1
    assert "confirm" not in calls[0][1]


def test_non_confirm_failure_still_recovery():
    """A normal tool failure (not requires_confirmation) keeps the existing
    recovery path and never sets _pending_confirmation."""
    calls = []

    def fake_run_tool(tool, arguments):
        calls.append((tool, dict(arguments)))
        return True, {"success": False, "error": "boom"}

    from tools import tool_registry

    original = tool_registry.run_tool
    tool_registry.run_tool = fake_run_tool
    try:
        brain = ScriptedBrain([
            {"reasoning": "", "response": "", "done": False,
             "step": {"tool": "browser_open", "arguments": {"url": "https://x.io"}}},
        ])
        loop = _make_loop(brain)
        loop.run("open x.io")
    finally:
        tool_registry.run_tool = original

    assert loop._pending_confirmation is None


def test_normalize_user_input_still_works():
    assert normalize_user_input("reserch quantum computin dawg").strip() == \
        "research quantum computing"
    assert normalize_user_input("pls create a taskk").strip() == \
        "please create a task"


def test_confirmation_prompt_mentions_universal_tools():
    import brain.brain as brain_mod
    assert "create_task" in brain_mod.TOOLS
    assert "send_message" in brain_mod.TOOLS
    assert "requires_confirmation" in brain_mod.TOOLS
    assert "OBSERVE" in brain_mod.AGENT_SYSTEM_PROMPT
    assert "VERIFY" in brain_mod.AGENT_SYSTEM_PROMPT