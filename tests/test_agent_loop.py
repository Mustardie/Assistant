import importlib
import json
from unittest.mock import MagicMock, patch

import pytest

from brain.agent_loop import AgentLoop

browser_agent_module = importlib.import_module("tools.browser.browser_agent")
agent_loop_module = importlib.import_module("brain.agent_loop")


def _make_loop(brain_decisions, *, speak=None, record_tool=None, fallback=None):
    brain = MagicMock()
    brain.think.side_effect = brain_decisions
    brain.answer_from_page.return_value = ""
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


@pytest.fixture(autouse=True)
def _no_live_browser(monkeypatch):
    """Unit tests must never touch a live browser bridge. The loop's
    deterministic page resolution sees an unavailable tab list (raising
    is the realistic offline behavior); tests that want tabs override
    list_tabs explicitly."""

    def _unavailable():
        raise ConnectionError("no browser bridge in unit tests")

    monkeypatch.setattr(browser_agent_module.browser_agent, "list_tabs", _unavailable)


def test_agent_loop_executes_single_step_and_stops_when_done():
    """browser_open is a terminal step: a successful open returns control
    immediately (stop=True) so the planner cannot re-open the same URL on
    the next turn -- the loop ends right after the single execution."""
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
        result = loop.run("open youtube")

    assert mock_run.call_count == 1
    assert mock_run.call_args[0] == ("browser_open", {"url": "https://youtube.com"})
    assert recorded == [("browser_open", "opened")]
    assert "Opening YouTube." in spoken
    assert "YouTube is open." not in spoken  # loop ended right after the open
    assert brain.think.call_count == 1
    assert result is None


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


def test_agent_loop_rejects_ask_user_when_goal_references_browser_page():
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "Need details",
            "response": "Which tab do you mean?",
            "done": False,
            "ask_user": True,
            "step": None,
        },
        {
            "reasoning": "Find the tab",
            "response": "Checking the tabs.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_list_tabs", "arguments": {}},
        },
        {
            "reasoning": "Done",
            "response": "Here is the summary.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"tabs": ["ChatGPT"]})):
        loop.run("see my open chatgpt tab and summarise the whole page for me")

    assert "Which tab do you mean?" not in spoken
    assert "Here is the summary." in spoken
    assert brain.think.call_count == 3


def test_agent_loop_ask_guard_fires_once_then_pauses():
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "Need details",
            "response": "What does the page say?",
            "done": False,
            "ask_user": True,
            "step": None,
        },
        {
            "reasoning": "Still need details",
            "response": "Seriously, which tab?",
            "done": False,
            "ask_user": True,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool") as mock_run:
        result = loop.run("summarise the chatgpt page")

    mock_run.assert_not_called()
    assert result is not None
    assert "goal" in result and "observations" in result
    assert len(result["observations"]) == 1
    assert any("browser_list_tabs" in obs["result"] for obs in result["observations"])
    assert "What does the page say?" not in spoken
    assert spoken == ["Seriously, which tab?"]
    assert brain.think.call_count == 2


def test_ask_guard_rejects_question_even_after_page_was_read(monkeypatch):
    """The guard's old hole: it only fired with empty observations. When
    the auto-resolver already read the page, a lazy 'what does it say?'
    ask must still be rejected -- the text is in the observations."""
    tabs = [
        {"tabId": 42, "title": "ChatGPT - my conversation", "url": "https://chatgpt.com/c/abc", "active": True},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 1},
    )

    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "No content provided by the user.",
            "response": "What does the page say?",
            "done": False,
            "ask_user": True,
            "step": None,
        },
        {
            "reasoning": "The text is in the observations; summarise it.",
            "response": "Summary: the page is about PC control from phone.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    read_result = {
        "title": "ChatGPT - my conversation",
        "url": "https://chatgpt.com/c/abc",
        "text": "PC control from phone: user asks about controlling their PC remotely.",
        "truncated": False,
    }

    with patch("brain.agent_loop.run_tool", return_value=(True, read_result)):
        result = loop.run("Summarise my open ChatGPT tab in 5 points")

    assert result is None
    assert "What does the page say?" not in spoken
    assert "Summary: the page is about PC control from phone." in spoken
    assert brain.think.call_count == 2

    second_observations = brain.think.call_args_list[1].kwargs["observations"]
    assert any(
        o["step"]["tool"] == "_browser_ask_guard"
        and "already" in o["result"]
        and "read" in o["result"]
        for o in second_observations
    )


def test_auto_resolution_matches_inactive_tab_via_hostname(monkeypatch):
    """The ChatGPT tab's title is often generic ('PC control from phone')
    while the user's active tab is something else entirely -- the URL host
    (chatgpt.com) is the distinctive signal and must score like a title."""
    tabs = [
        {"tabId": 1, "title": "Ollama REST API", "url": "http://localhost:11434/api/tags", "active": True},
        {"tabId": 42, "title": "PC control from phone", "url": "https://chatgpt.com/c/6a69f91f", "active": False},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 2},
    )

    loop, brain, spoken, _ = _make_loop([])
    brain.answer_from_page.return_value = "Short summary of the ChatGPT tab."

    with patch("brain.agent_loop.run_tool", side_effect=(
        lambda tool, arguments: (
            (True, {"success": True, "tabId": 42})
            if tool == "browser_switch_tab"
            else (True, {"title": "PC control from phone", "url": "https://chatgpt.com/c/6a69f91f", "text": "PC control from phone: long conversation text here.", "truncated": False})
        )
    )) as mock_run:
        result = loop.run("summarise my chatgpt tab, nothing specific just the whole tab in short")

    assert result is None
    assert brain.think.call_count == 0
    assert mock_run.call_args_list[0][0] == ("browser_switch_tab", {"tab": 42})
    assert "Short summary of the ChatGPT tab." in spoken[0]


def test_ask_guard_retries_resolution_before_hinting(monkeypatch):
    """If the initial auto-resolution failed (e.g. bridge hiccup), a
    rejected ask_user retries it deterministically instead of just
    hinting -- the planner still ends up with the page text."""
    calls = {"n": 0}

    def flaky_list_tabs():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("bridge hiccup")
        return {
            "tabs": [{"tabId": 42, "title": "ChatGPT", "url": "https://chatgpt.com", "active": True}],
            "count": 1,
        }

    monkeypatch.setattr(browser_agent_module.browser_agent, "list_tabs", flaky_list_tabs)

    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "No access.",
            "response": "Which tab?",
            "done": False,
            "ask_user": True,
            "step": None,
        },
        {
            "reasoning": "The text is in the observations; answer.",
            "response": "Summary: PC control from phone.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(
        True,
        {"title": "ChatGPT", "url": "https://chatgpt.com", "text": "PC control from phone: long conversation text here.", "truncated": False},
    )) as mock_run:
        result = loop.run("summarise my chatgpt tab")

    assert result is None
    assert calls["n"] == 2
    assert "Which tab?" not in spoken
    assert "Summary: PC control from phone." in spoken
    assert mock_run.call_count == 1  # only the read -- active tab, no switch
    assert brain.think.call_count == 2


def test_direct_answer_bypasses_planner_when_page_text_available(monkeypatch):
    """Track A ultimate fix: a plain consume goal with the page text in
    hand is answered via a focused text prompt -- the planner loop (which
    the weak model kept abusing to ask questions) is never invoked."""
    tabs = [{"tabId": 42, "title": "ChatGPT", "url": "https://chatgpt.com/c/abc", "active": True}]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 1},
    )

    loop, brain, spoken, _ = _make_loop([])
    brain.answer_from_page.return_value = (
        "1) The page is about controlling a PC from a phone.\n"
        "2) The user asked how to set it up."
    )

    with patch("brain.agent_loop.run_tool", return_value=(
        True,
        {
            "title": "ChatGPT - my conversation",
            "url": "https://chatgpt.com/c/abc",
            "text": "PC control from phone: user asks about controlling their PC remotely and setting it up.",
            "truncated": False,
        },
    )):
        result = loop.run("summarise my chatgpt tab in 5 points")

    assert result is None
    assert brain.think.call_count == 0
    brain.answer_from_page.assert_called_once()
    assert "PC control from phone" in brain.answer_from_page.call_args.kwargs["page_text"]
    assert "1) The page is about controlling a PC from a phone." in spoken[0]


def test_direct_answer_failure_speaks_sanitized_message(monkeypatch):
    """If the model server dies during the direct answer, the user gets
    the clean retry message -- never a raw backend error."""
    tabs = [{"tabId": 42, "title": "ChatGPT", "url": "https://chatgpt.com/c/abc", "active": True}]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 1},
    )

    loop, brain, spoken, _ = _make_loop([])
    brain.answer_from_page.side_effect = ConnectionError(
        "Could not reach Ollama at http://localhost:11434/api/chat"
    )

    with patch("brain.agent_loop.run_tool", return_value=(
        True,
        {
            "title": "ChatGPT - my conversation",
            "url": "https://chatgpt.com/c/abc",
            "text": "PC control from phone: user asks about controlling their PC remotely.",
            "truncated": False,
        },
    )):
        result = loop.run("summarise my chatgpt tab in 5 points")

    assert result is None
    assert len(spoken) == 1
    assert "localhost" not in spoken[0]
    assert "thinking engine" in spoken[0]


def test_direct_answer_not_used_for_comparison_goals(monkeypatch):
    """'compare' goals need the planner (multiple tabs) -- the direct path
    must not swallow them even when page text is available."""
    tabs = [{"tabId": 42, "title": "ChatGPT", "url": "https://chatgpt.com/c/abc", "active": True}]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 1},
    )

    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "Compare.",
            "response": "Comparing.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(
        True,
        {
            "title": "ChatGPT - my conversation",
            "url": "https://chatgpt.com/c/abc",
            "text": "PC control from phone: user asks about controlling their PC remotely.",
            "truncated": False,
        },
    )):
        result = loop.run("compare what's on my chatgpt tab and my flights tab")

    assert result is None
    assert brain.think.call_count == 1
    brain.answer_from_page.assert_not_called()


def test_resolution_detects_ambiguous_same_site_tabs(monkeypatch):
    """Several tabs of the same site ("the wikipedia tab" with three
    Wikipedia pages open) cannot be resolved deterministically -- the
    resolution signals ambiguity with the candidate list."""
    tabs = [
        {"tabId": 1, "title": "Vienna - Wikipedia", "url": "https://en.wikipedia.org/wiki/Vienna", "active": False},
        {"tabId": 2, "title": "Chess - Wikipedia", "url": "https://en.wikipedia.org/wiki/Chess", "active": True},
        {"tabId": 3, "title": "Google", "url": "https://google.com", "active": False},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 3},
    )

    result = agent_loop_module._resolve_browser_page("summarise the wikipedia tab")

    assert result is not None
    assert result.get("ambiguous") is True
    candidates = result.get("candidates") or []
    assert len(candidates) == 2
    assert {c["tabId"] for c in candidates} == {1, 2}


def test_resolution_unique_match_not_ambiguous(monkeypatch):
    """One Wikipedia tab among unrelated tabs resolves normally -- no
    confirmation question."""
    tabs = [
        {"tabId": 1, "title": "Vienna - Wikipedia", "url": "https://en.wikipedia.org/wiki/Vienna", "active": False},
        {"tabId": 2, "title": "Google", "url": "https://google.com", "active": True},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 2},
    )

    result = agent_loop_module._resolve_browser_page("summarise the wikipedia tab")

    assert result is not None
    assert not result.get("ambiguous")
    assert result["tabId"] == 1


def test_run_asks_confirmation_when_tabs_ambiguous(monkeypatch):
    """The loop pauses with a numbered question instead of guessing which
    Wikipedia page the user means -- no tool runs, the planner is not
    asked, and the task stays pending for the user's reply."""
    tabs = [
        {"tabId": 1, "title": "Vienna - Wikipedia", "url": "https://en.wikipedia.org/wiki/Vienna", "active": False},
        {"tabId": 2, "title": "Chess - Wikipedia", "url": "https://en.wikipedia.org/wiki/Chess", "active": False},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 2},
    )

    loop, brain, spoken, _ = _make_loop([])

    with patch("brain.agent_loop.run_tool") as mock_run:
        result = loop.run("summarise the wikipedia tab")

    mock_run.assert_not_called()
    brain.think.assert_not_called()
    assert result == {"goal": "summarise the wikipedia tab", "observations": []}
    assert len(spoken) == 1
    assert "Which one do you mean?" in spoken[0]
    assert "Vienna" in spoken[0] and "Chess" in spoken[0]


def test_run_picks_tab_by_number_on_resume(monkeypatch):
    """After the confirmation question, replying '2' reads the second
    candidate tab and answers from ITS text -- the original goal (not the
    reply) drives the answer."""
    tabs = [
        {"tabId": 1, "title": "Vienna - Wikipedia", "url": "https://en.wikipedia.org/wiki/Vienna", "active": False},
        {"tabId": 2, "title": "Chess - Wikipedia", "url": "https://en.wikipedia.org/wiki/Chess", "active": False},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 2},
    )

    loop, brain, spoken, _ = _make_loop([])
    brain.answer_from_page.return_value = (
        "Chess is a board game played by millions around the world."
    )

    goal = "summarise the wikipedia tab"
    with patch("brain.agent_loop.run_tool") as mock_run:
        first = loop.run(goal)
        assert first == {"goal": goal, "observations": []}
        mock_run.assert_not_called()
        mock_run.side_effect = [
            (True, {"success": True, "tabId": 2}),
            (
                True,
                {
                    "title": "Chess - Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/Chess",
                    "text": "Chess is a board game played by millions around the world.",
                    "truncated": False,
                },
            ),
        ]
        result = loop.run("2", resume_goal=goal, resume_observations=[])

    assert result is None
    assert mock_run.call_args_list[0][0] == ("browser_switch_tab", {"tab": 2})
    brain.answer_from_page.assert_called_once()
    assert brain.answer_from_page.call_args.kwargs["goal"] == goal
    assert "Chess is a board game" in brain.answer_from_page.call_args.kwargs["page_text"]
    assert "Chess is a board game played by millions" in spoken[-1]


def test_run_picks_tab_by_title_on_resume(monkeypatch):
    """'the chess one' resolves by title -- the word 'one' must not be
    treated as candidate number 1."""
    tabs = [
        {"tabId": 1, "title": "Vienna - Wikipedia", "url": "https://en.wikipedia.org/wiki/Vienna", "active": False},
        {"tabId": 2, "title": "Chess - Wikipedia", "url": "https://en.wikipedia.org/wiki/Chess", "active": False},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 2},
    )

    loop, brain, spoken, _ = _make_loop([])
    brain.answer_from_page.return_value = "Chess summary here."

    goal = "summarise the wikipedia tab"
    with patch("brain.agent_loop.run_tool") as mock_run:
        loop.run(goal)
        mock_run.side_effect = [
            (True, {"success": True, "tabId": 2}),
            (
                True,
                {
                    "title": "Chess - Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/Chess",
                    "text": "Chess is a board game played by millions around the world.",
                    "truncated": False,
                },
            ),
        ]
        result = loop.run("the chess one", resume_goal=goal, resume_observations=[])

    assert result is None
    assert mock_run.call_args_list[0][0] == ("browser_switch_tab", {"tab": 2})
    assert "Chess summary here." in spoken[-1]


def test_content_scan_resolves_tab_when_metadata_misses(monkeypatch):
    """Titles say nothing ('New Tab', 'Document') but one page's text
    mentions the goal word -- the scan reads every tab, finds it, and
    restores the user's original active tab."""
    tabs = [
        {"tabId": 1, "title": "New Tab", "url": "about:blank", "active": True},
        {"tabId": 2, "title": "Untitled", "url": "about:blank", "active": False},
        {"tabId": 3, "title": "Document", "url": "about:blank", "active": False},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 3},
    )

    texts = {
        1: "nothing here at all",
        2: "some recipes and cooking tips",
        3: "the football world cup finals were amazing this year",
    }
    current = 1

    def fake_run_tool(tool, arguments):
        nonlocal current
        if tool == "browser_switch_tab":
            current = arguments.get("tab")
            return (True, {"success": True, "tabId": current})
        if tool == "browser_read":
            return (True, {
                "title": f"tab {current}",
                "url": "about:blank",
                "text": texts.get(current, ""),
                "truncated": False,
            })
        raise AssertionError(f"unexpected tool {tool}")

    loop, brain, spoken, _ = _make_loop([])
    brain.answer_from_page.return_value = "Football world cup summary."

    with patch("brain.agent_loop.run_tool", side_effect=fake_run_tool) as mock_run:
        result = loop.run("tell me about the football tab")

    assert result is None
    switches = [a[0][1]["tab"] for a in mock_run.call_args_list if a[0][0] == "browser_switch_tab"]
    assert 3 in switches  # scanned and resolved to the football tab
    assert 1 in switches  # original active tab was restored
    assert switches[-1] == 3  # final read happens on the resolved tab
    assert "Football world cup summary." in spoken[0]
    brain.answer_from_page.assert_called_once()
    assert "football world cup finals" in brain.answer_from_page.call_args.kwargs["page_text"]


def test_content_scan_asks_confirmation_on_tie(monkeypatch):
    """Two tabs whose texts both mention the goal word are ambiguous too --
    the user picks, the loop never guesses."""
    tabs = [
        {"tabId": 1, "title": "New Tab", "url": "about:blank", "active": True},
        {"tabId": 2, "title": "Untitled", "url": "about:blank", "active": False},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 2},
    )

    texts = {1: "all about football tactics", 2: "the football transfer window news"}
    current = 1

    def fake_run_tool(tool, arguments):
        nonlocal current
        if tool == "browser_switch_tab":
            current = arguments.get("tab")
            return (True, {"success": True, "tabId": current})
        if tool == "browser_read":
            return (True, {"title": f"tab {current}", "url": "about:blank",
                           "text": texts.get(current, ""), "truncated": False})
        raise AssertionError(f"unexpected tool {tool}")

    loop, brain, spoken, _ = _make_loop([])

    with patch("brain.agent_loop.run_tool", side_effect=fake_run_tool) as mock_run:
        result = loop.run("tell me about the football tab")

    assert result is not None
    assert result["observations"] == []
    brain.think.assert_not_called()
    brain.answer_from_page.assert_not_called()
    assert len(spoken) == 1
    assert "Which one do you mean?" in spoken[0]


def test_sanitizes_raw_backend_error_response():
    """An Ollama/API outage must never be narrated verbatim (the planner
    later hallucinated it as a browser/internet problem)."""
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "LLM unreachable",
            "response": "Could not reach Ollama at http://localhost:11434/api/chat: Max retries exceeded",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool") as mock_run:
        result = loop.run("summarise the page")

    mock_run.assert_not_called()
    assert result is None
    assert len(spoken) == 1
    assert "localhost" not in spoken[0]
    assert "11434" not in spoken[0]
    assert "ollama" not in spoken[0].lower()
    assert "thinking engine" in spoken[0]


def test_backend_error_sanitization_leaves_normal_responses_alone():
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "Done.",
            "response": "The capital of Japan is Tokyo.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool"):
        result = loop.run("What is the capital of Japan?")

    assert result is None
    assert spoken == ["The capital of Japan is Tokyo."]


def test_auto_reads_matching_open_tab_for_read_goal(monkeypatch):
    """The loop resolves 'my chatgpt tab' deterministically: list tabs ->
    keyword match (chatgpt in the title) -> switch -> read -> the planner
    only has to write the summary. No 'which tab?' is ever possible."""
    tabs = [
        {"tabId": 1, "title": "Google", "url": "https://google.com", "active": False},
        {"tabId": 42, "title": "ChatGPT - my conversation", "url": "https://chatgpt.com/c/abc", "active": False},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 2},
    )

    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "The page text is already in the observations; write the summary.",
            "response": "Here is the summary of your ChatGPT page: ...",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    read_result = {
        "title": "ChatGPT - my conversation",
        "url": "https://chatgpt.com/c/abc",
        "text": "User: hello, please help me plan a trip.",
        "truncated": False,
    }

    def fake_run_tool(tool, arguments):
        if tool == "browser_switch_tab":
            return (True, {"success": True, "tabId": 42})
        if tool == "browser_read":
            return (True, read_result)
        raise AssertionError(f"unexpected tool {tool}")

    with patch("brain.agent_loop.run_tool", side_effect=fake_run_tool) as mock_run:
        result = loop.run("see my open chatgpt tab and summarise the whole page for me")

    assert result is None
    assert mock_run.call_args_list[0][0] == ("browser_switch_tab", {"tab": 42})
    assert mock_run.call_args_list[1][0] == ("browser_read", {})
    assert brain.think.call_count == 1
    assert "Here is the summary of your ChatGPT page: ..." in spoken

    observations = brain.think.call_args_list[0].kwargs["observations"]
    assert len(observations) == 2
    assert observations[0]["step"]["tool"] == "auto_tab_select"
    assert observations[0]["success"] is True
    assert observations[1]["step"]["tool"] == "browser_read"
    assert observations[1]["success"] is True
    assert "plan a trip" in json.dumps(observations[1]["result"])


def test_auto_resolution_uses_active_tab_for_wordless_page_goal(monkeypatch):
    """'just summarise the page' has no discriminating words: the ACTIVE
    tab is the referent. No switch needed -- read the current tab only."""
    tabs = [
        {"tabId": 1, "title": "Google", "url": "https://google.com", "active": True},
        {"tabId": 42, "title": "ChatGPT", "url": "https://chatgpt.com", "active": False},
    ]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 2},
    )

    loop, brain, _, _ = _make_loop([
        {
            "reasoning": "Page text is in the observations; answer.",
            "response": "Summary: ...",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"title": "Google", "text": "x"})) as mock_run:
        result = loop.run("just summarise the page")

    assert result is None
    assert mock_run.call_count == 1
    assert mock_run.call_args_list[0][0] == ("browser_read", {})

    observations = brain.think.call_args_list[0].kwargs["observations"]
    assert observations[0]["step"]["tool"] == "auto_tab_select"
    assert observations[0]["result"]["url"] == "https://google.com"


def test_auto_resolution_skipped_for_open_goal(monkeypatch):
    """'open the chatgpt tab' is an OPEN goal, not a READ goal: no auto
    switch/read. The planner still decides (browser_open dedupes)."""
    tabs = [{"tabId": 42, "title": "ChatGPT", "url": "https://chatgpt.com", "active": True}]
    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs",
        lambda: {"tabs": tabs, "count": 1},
    )

    loop, brain, _, _ = _make_loop([
        {
            "reasoning": "Open the tab.",
            "response": "Opening.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_open_tab", "arguments": {"url": "https://chatgpt.com"}},
        },
        {
            "reasoning": "Done.",
            "response": "Open.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, "opened")) as mock_run:
        result = loop.run("open the chatgpt tab")

    assert result is None
    obs_tools = [o["step"]["tool"] for o in brain.think.call_args_list[0].kwargs["observations"]]
    assert "auto_tab_select" not in obs_tools
    assert mock_run.call_args_list[0][0] == ("browser_open_tab", {"url": "https://chatgpt.com"})


def test_auto_resolution_skipped_when_tab_list_unavailable(monkeypatch):
    """Tab list failing must not break the task: fall back to normal
    planning (the planner can list tabs itself, which may fail visibly)."""

    def _unavailable():
        raise ConnectionError("bridge down")

    monkeypatch.setattr(
        browser_agent_module.browser_agent, "list_tabs", _unavailable
    )

    loop, brain, _, _ = _make_loop([
        {
            "reasoning": "Try listing tabs.",
            "response": "Checking tabs.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_list_tabs", "arguments": {}},
        },
        {
            "reasoning": "No tabs available; report.",
            "response": "I could not reach the browser.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"tabs": []})):
        result = loop.run("see my open chatgpt tab and summarise the whole page for me")

    assert result is None
    obs_tools = [o["step"]["tool"] for o in brain.think.call_args_list[0].kwargs["observations"]]
    assert "auto_tab_select" not in obs_tools


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

    # The deterministic flight task pre-opens the Google Flights search
    # URL, then the planner still drives the booking steps.
    assert mock_run.call_args_list[0][0] == (
        "browser_open",
        {"url": "https://www.google.com/travel/flights?q=flights%20from%20Mumbai%20to%20Singapore"},
    )
    assert mock_run.call_args_list[-1][0] == ("browser_open_tab", {"url": "https://flights.google.com"})
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


# ---------------------------------------------------------------------------
# Loop guard: repeated identical tool calls
# ---------------------------------------------------------------------------

READ_STEP = {"tool": "browser_get_state", "arguments": {}}


def test_loop_guard_blocks_infinite_identical_tool_loop():
    """Reproduces the reported bug: the planner keeps calling the same tool
    with the same arguments forever (browser_read → still a title → read
    again...). The loop must execute the call ONCE, serve the cached result
    on the second identical call, block the third, and auto-stop on the
    fourth -- the identical call can never execute more than once."""
    loop, brain, spoken, recorded = _make_loop([
        {"reasoning": "r1", "response": "Reading.", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r2", "response": "Reading again.", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r3", "response": "Reading again.", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r4", "response": "Reading again.", "done": False, "ask_user": False, "step": READ_STEP},
    ])

    result1 = {"success": True, "title": "Home", "url": "https://example.com"}

    with patch("brain.agent_loop.run_tool", return_value=(True, result1)) as mock_run:
        result = loop.run("find the price on the page")

    assert mock_run.call_count == 1, "identical tool call must execute at most once"
    assert recorded == [("browser_get_state", result1)]
    assert brain.think.call_count == 4

    observations = result["observations"]
    assert len(observations) == 4
    assert observations[1]["reused_from_cache"] is True
    assert observations[2]["blocked_by_loop_guard"] is True
    assert observations[3]["blocked_by_loop_guard"] is True
    assert observations[3]["auto_stopped"] is True
    assert "UNPRODUCTIVE" in observations[2]["result"]
    assert "UNPRODUCTIVE" in observations[3]["result"]
    assert loop._unproductive_tools == {"browser_get_state"}
    assert any("same action" in m and "browser_get_state" in m for m in spoken)


def test_loop_guard_second_identical_call_serves_cached_result():
    """Two identical calls in a row: the second must NOT re-execute the tool
    -- the planner gets the exact cached result back with a flag, and the
    loop can finish normally on the next turn."""
    loop, brain, _, recorded = _make_loop([
        {"reasoning": "r1", "response": "Reading.", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r2", "response": "Reading again.", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r3", "response": "Done.", "done": True, "ask_user": False, "step": None},
    ])

    result1 = {"success": True, "title": "Home"}

    with patch("brain.agent_loop.run_tool", return_value=(True, result1)) as mock_run:
        result = loop.run("read the page")

    assert mock_run.call_count == 1
    assert recorded == [("browser_get_state", result1)]
    assert result is None

    observations = brain.think.call_args_list[2].kwargs["observations"]
    assert observations[1]["reused_from_cache"] is True
    assert observations[1]["result"] is result1
    assert len(loop._tool_history) == 1
    assert loop._tool_history[0]["tool"] == "browser_get_state"
    assert "result_hash" in loop._tool_history[0]


def test_loop_guard_allows_same_tool_after_different_action():
    """The guard must not throttle legitimate re-uses: the same tool is fine
    when a different action happened in between (the world may have changed)."""
    note_step = {"tool": "browser_note", "arguments": {"key": "k", "value": "v"}}
    loop, brain, _, _ = _make_loop([
        {"reasoning": "r1", "response": "A", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r2", "response": "B", "done": False, "ask_user": False, "step": note_step},
        {"reasoning": "r3", "response": "C", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r4", "response": "D", "done": True, "ask_user": False, "step": None},
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})) as mock_run:
        result = loop.run("multi-step task")

    assert mock_run.call_count == 3, "non-consecutive identical calls must still execute"
    assert result is None


def test_loop_guard_different_arguments_not_throttled():
    """Different arguments for the same tool are different calls -- both
    must execute."""
    loop, brain, _, _ = _make_loop([
        {"reasoning": "r1", "response": "A", "done": False, "ask_user": False,
         "step": {"tool": "browser_record_data", "arguments": {"key": "a", "data": "1"}}},
        {"reasoning": "r2", "response": "B", "done": False, "ask_user": False,
         "step": {"tool": "browser_record_data", "arguments": {"key": "b", "data": "2"}}},
        {"reasoning": "r3", "response": "C", "done": True, "ask_user": False, "step": None},
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})) as mock_run:
        result = loop.run("record two things")

    assert mock_run.call_count == 2
    assert result is None


def test_loop_guard_block_message_marks_tool_unproductive():
    """After the block, the observation fed back to the planner names the
    tool as unproductive and orders a different approach."""
    loop, brain, _, _ = _make_loop([
        {"reasoning": "r1", "response": "A", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r2", "response": "B", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r3", "response": "C", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r4", "response": "D", "done": True, "ask_user": False, "step": None},
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})):
        result = loop.run("task")

    block_message = brain.think.call_args_list[3].kwargs["observations"][2]["result"]
    assert "browser_get_state" in block_message
    assert "UNPRODUCTIVE" in block_message
    assert "Repeating it cannot succeed" in block_message
    assert "DIFFERENT tool" in block_message
    assert loop._unproductive_tools == {"browser_get_state"}
    assert result is None


def test_loop_guard_ignores_none_argument_differences():
    """{'tab': None} and {} must count as the SAME call -- otherwise a
    model alternating between the two spellings would dodge the guard."""
    loop, brain, _, _ = _make_loop([
        {"reasoning": "r1", "response": "A", "done": False, "ask_user": False,
         "step": {"tool": "browser_get_state", "arguments": {"tab": None}}},
        {"reasoning": "r2", "response": "B", "done": False, "ask_user": False, "step": READ_STEP},
        {"reasoning": "r3", "response": "C", "done": True, "ask_user": False, "step": None},
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})) as mock_run:
        result = loop.run("task")

    assert mock_run.call_count == 1, "None-valued args must not dodge the guard"
    assert result is None


# ---------------------------------------------------------------------------
# Finish forcing: the loop must make a weak planner ANSWER instead of
# looping forever on narration and pointless re-reads
# ---------------------------------------------------------------------------


def test_agent_loop_finish_hint_after_no_step_when_data_gathered():
    """Reproduces the reported bug: the page was opened AND read (data in
    the observations), but the planner kept returning 'I will now
    summarize...' without a step, and the loop's old hint ('MUST return a
    valid step') pushed it to re-read the same page. With data already
    gathered, the loop must tell it to FINISH (done=true + answer), and
    narrated responses must never be spoken."""
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "open",
            "response": "I will now proceed by opening the Wikipedia page.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_open_tab", "arguments": {"url": "https://en.wikipedia.org/wiki/Tom_Holland"}},
        },
        {
            "reasoning": "stall",
            "response": "I will now extract key information and summarize it.",
            "done": False,
            "ask_user": False,
            "step": None,
        },
        {
            "reasoning": "finish",
            "response": "Tom Holland is a British actor born on 1 June 1996 in London. He is best known for playing Spider-Man in the MCU, starting with Captain America: Civil War (2016). His Spider-Man films grossed over $1 billion each. He also starred in Uncharted and The Odyssey, and he is married to Zendaya.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})) as mock_run:
        result = loop.run("open the Tom Holland wikipedia page and summarise it for me")

    assert mock_run.call_count == 1
    assert result is None

    # The stall turn got a _finish_hint (answer now, no more tools), not
    # the old "MUST return a valid step" hint.
    observations = brain.think.call_args_list[2].kwargs["observations"]
    assert observations[1]["step"]["tool"] == "_finish_hint"
    assert "done=true" in observations[1]["result"]
    assert "Do NOT call another tool" in observations[1]["result"]

    # Narrations were never spoken; only the real summary was.
    assert all("I will now" not in m for m in spoken)
    assert any("Tom Holland is a British actor" in m for m in spoken)


def test_agent_loop_gives_up_after_three_no_step_turns():
    """If the planner produces neither a step nor an answer for three
    consecutive turns, the loop must STOP (with a spoken fallback) instead
    of spinning until the iteration budget runs out."""
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "open",
            "response": "Opening the page.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_open_tab", "arguments": {"url": "https://en.wikipedia.org/wiki/Tom_Holland"}},
        },
        {"reasoning": "stall", "response": "I will now summarize.", "done": False, "ask_user": False, "step": None},
        {"reasoning": "stall", "response": "I will now summarize.", "done": False, "ask_user": False, "step": None},
        {"reasoning": "stall", "response": "I will now summarize.", "done": False, "ask_user": False, "step": None},
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})):
        result = loop.run("open the page and summarise it")

    assert brain.think.call_count == 4
    assert result is not None
    assert result["goal"] == "open the page and summarise it"
    assert any("having trouble" in m for m in spoken)
    assert not any("I will now" in m for m in spoken)
    # Two finish hints were fed back before the loop gave up.
    finish_hints = [o for o in result["observations"] if isinstance(o.get("step"), dict)
                    and o["step"].get("tool") == "_finish_hint"]
    assert len(finish_hints) == 2


def test_agent_loop_narrated_response_with_step_is_not_spoken():
    """Even when a narrated 'I will now...' response accompanies a real
    step, it must not be spoken aloud -- only real answers reach the user."""
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "open",
            "response": "I will now proceed by opening the page.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_open_tab", "arguments": {"url": "https://example.com"}},
        },
        {
            "reasoning": "done",
            "response": "The page is open.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})):
        result = loop.run("open example.com")

    assert result is None
    assert "I will now proceed by opening the page." not in spoken
    assert "The page is open." in spoken
    assert spoken == ["The page is open."]

def test_document_to_folder_task_creates_folder_and_saves_file(monkeypatch, tmp_path):
    """'write a document on X and save it in a new folder named research of
    X' runs deterministically: create the folder, draft the content, save
    the file -- no planner, no questions."""
    monkeypatch.chdir(tmp_path)
    loop, brain, spoken, _ = _make_loop([])
    brain.draft_document.return_value = "Black holes are regions of spacetime.\n\nThey are fascinating."

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True, "path": str(tmp_path)})) as mock_run:
        result = loop.run('write a document about black holes and save it in a new folder named research of black holes')

    assert result is None
    brain.think.assert_not_called()
    assert mock_run.call_args_list[0][0] == ("create_folder", {"path": "research of black holes"})
    brain.draft_document.assert_called_once_with("black holes")
    saved = tmp_path / "research of black holes" / "black holes.md"
    assert saved.exists()
    assert "Black holes are regions of spacetime." in saved.read_text(encoding="utf-8")
    assert "saved the document" in spoken[-1]


def test_document_to_folder_defaults_folder_to_research_of_topic(monkeypatch, tmp_path):
    """No folder named in the goal -> 'research of <topic>'."""
    monkeypatch.chdir(tmp_path)
    loop, brain, spoken, _ = _make_loop([])
    brain.draft_document.return_value = "Dogs are loyal."

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True, "path": str(tmp_path)})):
        result = loop.run("write a document on dogs and save it")

    assert result is None
    assert (tmp_path / "research of dogs" / "dogs.md").exists()


def test_document_to_folder_save_verb_first_phrasing(monkeypatch, tmp_path):
    """'save a document about X to a folder called Y' -- the verb-first
    phrasing that used to fall through to the planner (and made qwen open
    Google Docs instead) -- must hit the deterministic path with the
    explicit folder name."""
    monkeypatch.chdir(tmp_path)
    loop, brain, spoken, _ = _make_loop([])
    brain.draft_document.return_value = "The war began in 1914."

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True, "path": str(tmp_path)})) as mock_run:
        result = loop.run("save a document about the causes of World War One to a folder called History Notes")

    assert result is None
    brain.think.assert_not_called()
    assert mock_run.call_args_list[0][0] == ("create_folder", {"path": "History Notes"})
    brain.draft_document.assert_called_once_with("the causes of World War One")
    saved = tmp_path / "History Notes" / "the causes of World War One.md"
    assert saved.exists()
    assert "The war began in 1914." in saved.read_text(encoding="utf-8")
    assert "History Notes" in spoken[-1]


def test_document_to_folder_plural_nouns_and_implied_save(monkeypatch, tmp_path):
    """'save my notes about chess in a folder called Board Games' -- plural
    noun + folder-only tail -- is still detected."""
    monkeypatch.chdir(tmp_path)
    loop, brain, spoken, _ = _make_loop([])
    brain.draft_document.return_value = "Chess strategy."

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True, "path": str(tmp_path)})) as mock_run:
        result = loop.run("save my notes about chess in a folder called Board Games")

    assert result is None
    brain.think.assert_not_called()
    assert mock_run.call_args_list[0][0] == ("create_folder", {"path": "Board Games"})
    assert (tmp_path / "Board Games" / "chess.md").exists()


def test_gdocs_task_opens_docs_and_types_drafted_content(monkeypatch):
    """'open google docs and write about chess' chains: open docs.new ->
    wait for the editor -> rename the title -> type the drafted document."""
    loop, brain, spoken, _ = _make_loop([])
    brain.draft_document.return_value = "Chess is a two-player strategy game. " * 5

    calls = []
    with patch("brain.agent_loop.run_tool", side_effect=lambda tool, args: (
        calls.append((tool, args)) or (True, {"success": True})
    )):
        result = loop.run("open google docs and write about chess")

    assert result is None
    brain.think.assert_not_called()
    tools = [t for t, _ in calls]
    assert tools[0] == "browser_open"
    assert calls[0][1]["url"] == "https://docs.new"
    assert "browser_wait_for_load" in tools
    assert "browser_wait_for_element" in tools
    assert ("browser_click", {"description": "Untitled document"}) in calls
    assert ("browser_type", {"description": "Untitled document", "text": "chess", "clear_first": True}) in calls
    typed = [(t, a) for t, a in calls if t == "browser_type" and a["description"] == "[contenteditable]"]
    assert typed, "must type the content into the editor"
    assert "".join(a["text"] for _, a in typed).count("Chess is a two-player") == 5
    assert "chess" in spoken[-1]


def test_hotel_task_preopens_google_hotels_search(monkeypatch):
    """'search hotels in Paris' opens the Google Hotels query URL; the
    planner then takes over the interactive part."""
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "Results are loading; read the page.",
            "response": "Opening hotel search results.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_read", "arguments": {}},
        },
        {
            "reasoning": "Done",
            "response": "Here are hotels in Paris.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})) as mock_run:
        result = loop.run("search hotels in Paris for next weekend")

    assert result is None
    assert mock_run.call_args_list[0][0] == (
        "browser_open",
        {"url": "https://www.google.com/travel/hotels?q=hotels%20in%20Paris"},
    )
    assert "Here are hotels in Paris." in spoken[-1]


def test_plain_open_goal_not_hijacked_by_task_detection(monkeypatch):
    """'open google docs' (no write intent) stays on the planner path."""
    loop, brain, spoken, _ = _make_loop([
        {
            "reasoning": "Just open it.",
            "response": "Opening Google Docs.",
            "done": False,
            "ask_user": False,
            "step": {"tool": "browser_open", "arguments": {"url": "https://docs.google.com"}},
        },
        {
            "reasoning": "Done",
            "response": "Google Docs is open.",
            "done": True,
            "ask_user": False,
            "step": None,
        },
    ])

    with patch("brain.agent_loop.run_tool", return_value=(True, {"success": True})) as mock_run:
        result = loop.run("open google docs")

    assert result is None
    # browser_open is terminal -- the loop ends right after the open.
    assert mock_run.call_args_list[0][0] == ("browser_open", {"url": "https://docs.google.com"})
    assert brain.think.call_count == 1
