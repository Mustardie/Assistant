from brain.agent import Agent
from brain.intent_router import Intent, IntentRouter


def test_search_result_is_explained_and_not_opened(monkeypatch):
    agent = Agent.__new__(Agent)
    agent._pending_user_goal = None
    spoken = []
    calls = []
    agent._speak = spoken.append
    monkeypatch.setattr("brain.agent.run_tool", lambda tool, args: calls.append((tool, args)))
    agent._act_on_result(
        "find the crash log",
        "search",
        {
            "path": r"C:\Games\.minecraft\logs\latest.log",
            "summary": "Crash log from Minecraft/Fabric.",
            "confidence": 0.94,
            "risk": "low",
            "evidence": ["Fabric marker"],
        },
    )
    assert not calls
    assert "Crash log from Minecraft" in spoken[0]


def test_git_and_file_purpose_requests_route_as_file_requests():
    agent = Agent.__new__(Agent)
    assert agent._is_file_request("what should I commit?")
    assert agent._is_file_request("summarize this crash log")
    assert agent._is_file_request("find the file that stores my settings")


def test_structured_router_selects_file_intent_and_git_tools():
    router = IntentRouter()
    purpose = router.route("find the Minecraft crash log")
    git = router.route("what should I commit?")
    assert purpose.intent == Intent.LOCAL_TASK
    assert purpose.likely_required_tools == ["file_intent_search"]
    assert git.intent == Intent.LOCAL_TASK
    assert git.likely_required_tools == ["file_git_summary"]
