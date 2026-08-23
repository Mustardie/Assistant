"""Agent loop integration tests for the Skills subsystem: command
detection, deterministic handling, playback pause/resume, improvement
offers and auto-play -- all with the skill manager mocked."""

import importlib
from unittest.mock import MagicMock

import pytest

from brain.agent_loop import AgentLoop

agent_loop_module = importlib.import_module("brain.agent_loop")
detect = agent_loop_module._detect_skill_request


# --------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------- #

def test_detect_watch_me():
    assert detect("watch me") == {"intent": "record_start", "args": {"name": None}}


def test_detect_record_with_name():
    assert detect("record a skill called Create Word Doc")["intent"] == "record_start"
    assert detect("record a skill called Create Word Doc")["args"]["name"] == "Create Word Doc"


def test_detect_learn_this():
    assert detect("learn this")["intent"] == "record_start"


def test_detect_recording_controls():
    assert detect("stop recording")["intent"] == "record_stop"
    assert detect("pause the recording")["intent"] == "record_pause"
    assert detect("resume recording")["intent"] == "record_resume"
    assert detect("cancel the recording")["intent"] == "record_cancel"
    assert detect("i'm done")["intent"] == "record_stop"


def test_detect_record_stop_extracts_name():
    parsed = detect(
        "stop recording, save the skill as school files, read the title "
        "of the document before downloading"
    )
    assert parsed["intent"] == "record_stop"
    assert parsed["args"]["name"] == "school files"
    assert detect("stop recording and save it as My Groceries")["args"]["name"] == "My Groceries"
    assert detect("stop recording")["args"]["name"] is None


def test_detect_record_stop_extracts_written_instructions():
    parsed = detect(
        "stop recording, save the skill as school files, read the title of "
        "the document before downloading, and navigate through the pages"
    )
    assert parsed["args"]["instructions"] == [
        "read the title of the document before downloading",
        "navigate through the pages",
    ]
    # No instructions -> empty list, no crash.
    assert detect("stop recording")["args"]["instructions"] == []
    # Instructions without a rename clause.
    parsed = detect("stop recording, read the title before downloading")
    assert parsed["args"]["name"] is None
    assert parsed["args"]["instructions"] == ["read the title before downloading"]


def test_detect_play():
    parsed = detect("do the powerpoint skill")
    assert parsed["intent"] == "play"
    assert parsed["args"]["name"] == "powerpoint"
    assert detect("run my Monthly Report skill")["args"]["name"] == "Monthly Report"
    assert detect("play the spotify daily skill")["args"]["name"] == "spotify daily"


def test_detect_editing():
    parsed = detect("rename the Create Word skill to Create Docs")
    assert parsed["intent"] == "rename"
    assert parsed["args"] == {"name": "Create Word", "new_name": "Create Docs"}
    assert detect("delete the old skill")["intent"] == "delete"
    assert detect("export the Create Word skill")["intent"] == "export"
    assert detect("duplicate the Create Word skill as Backup")["intent"] == "duplicate"
    assert detect("show me my skills")["intent"] == "list"
    assert detect("what skills do you have")["intent"] == "list"
    assert detect("dry run the Create Word skill") == {
        "intent": "test", "args": {"name": "Create Word"}
    }


def test_detect_returns_none_for_normal_requests():
    for request in (
        "open youtube", "generate my monthly report", "summarize the page",
        "play some music", "create a new document about physics",
        "what is the weather",
    ):
        assert detect(request) is None, request


# --------------------------------------------------------------------- #
# Handler (mocked skill manager)
# --------------------------------------------------------------------- #

class FakeSkillManager:
    def __init__(self):
        self.calls = []
        self.is_recording = False

    def _log(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return {"success": True, "speak": f"{method} ok"}

    def add_narration(self, text):
        self.calls.append(("add_narration", {"text": text}))
        return {"success": True, "captured": True, "speak": "Got it, I'll remember that instruction."}

    def start_recording(self, name=None):
        self.calls.append(("start_recording", {"name": name}))
        return {"success": True,
                "speak": "I'm watching. Go ahead and perform the task. "
                         "Say 'stop recording' when you're done."}

    def stop_recording(self, name=None):
        return self._log("stop_recording", name=name)

    def pause_recording(self):
        return self._log("pause_recording")

    def resume_recording(self):
        return self._log("resume_recording")

    def cancel_recording(self):
        return self._log("cancel_recording")

    def list_skills(self):
        return [{"name": "Alpha", "description": "demo"}]

    def rename_skill(self, name, new_name):
        return self._log("rename_skill", name=name, new_name=new_name)

    def delete_skill(self, name):
        return self._log("delete_skill", name=name)

    def export_skill(self, name):
        return self._log("export_skill", name=name)

    def duplicate_skill(self, name, new_name=None):
        return self._log("duplicate_skill", name=name, new_name=new_name)

    def test_skill(self, name):
        self.calls.append(("test_skill", {"name": name}))
        return {"success": True, "name": name, "steps": 3, "errors": [], "executed": False}

    def play(self, name):
        self.calls.append(("play", {"name": name}))
        return {"status": "done", "message": f"Done {name}!", "session": None}

    def continue_playback(self, session, answer):
        self.calls.append(("continue_playback", {"answer": answer}))
        return {"status": "done", "message": "Done!", "session": None}

    def apply_improvement(self, session, accept):
        self.calls.append(("apply_improvement", {"accept": accept}))
        return {"success": True, "speak": "updated the skill"}

    def observe_request(self, request):
        self.calls.append(("observe_request", {"request": request}))
        return None

    def auto_play_candidate(self, request):
        return None


@pytest.fixture
def loop_and_fake(monkeypatch):
    brain = MagicMock()
    spoken = []
    loop = AgentLoop(
        brain,
        speak=spoken.append,
        record_tool=lambda tool, result: None,
    )
    fake = FakeSkillManager()
    monkeypatch.setattr(agent_loop_module, "skill_manager", fake)
    return loop, fake, spoken


def test_watch_me_starts_recording(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    assert loop.run("watch me") is None
    assert fake.calls[0][0] == "start_recording"
    assert any("watching" in line for line in spoken)


def test_stop_recording(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    assert loop.run("stop recording") is None
    assert fake.calls[0][0] == "stop_recording"


def test_narration_captured_while_recording(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    fake.is_recording = True
    # Non-command speech while watching -> captured as narration, NOT
    # dispatched to the planner.
    assert loop.run("read the title of the document before downloading") is None
    assert fake.calls[0][0] == "add_narration"
    assert any("remember that instruction" in line for line in spoken)


def test_stop_recording_extracts_name(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    fake.is_recording = True
    assert loop.run("stop recording, save the skill as school files") is None
    stop_call = [c for c in fake.calls if c[0] == "stop_recording"]
    assert stop_call and stop_call[0][1]["name"] == "school files"


def test_stop_recording_narrates_written_instructions(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    fake.is_recording = True
    assert loop.run(
        "stop recording, save the skill as school files, read the title "
        "of the document before downloading, and navigate through the pages"
    ) is None
    narrated = [c for c in fake.calls if c[0] == "add_narration"]
    assert [c[1]["text"] for c in narrated] == [
        "read the title of the document before downloading",
        "navigate through the pages",
    ]
    stop_call = [c for c in fake.calls if c[0] == "stop_recording"]
    assert stop_call and stop_call[0][1]["name"] == "school files"


def test_play_skill_command(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    assert loop.run("do the monthly report skill") is None
    assert ("play", {"name": "monthly report"}) in fake.calls
    assert any("Done monthly report!" in line for line in spoken)


def test_list_skills(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    assert loop.run("show me my skills") is None
    assert any("Alpha" in line for line in spoken)


def test_playback_pauses_for_input_and_resumes(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    fake.play = lambda name: {
        "status": "need_input",
        "message": "What should the document title be?",
        "session": "session-1",
    }

    pending = loop.run("do the create word skill")
    assert pending is not None  # paused mid-task
    assert loop._pending_skill_session == "session-1"
    assert any("document title" in line for line in spoken)

    fake.continue_playback = lambda session, answer: (
        fake.calls.append(("continue_playback", {"answer": answer})) or
        {"status": "done", "message": "Done!", "session": None}
    )
    result = loop.run("Physics Notes", resume_goal="do the create word skill",
                      resume_observations=[])
    assert result is None
    assert ("continue_playback", {"answer": "Physics Notes"}) in fake.calls
    assert loop._pending_skill_session is None


def test_improvement_offer_accepted_on_resume(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    fake.play = lambda name: {
        "status": "done",
        "message": "Done! Also I found a faster path. Improve this skill?",
        "improvement_offer": True,
        "session": "session-2",
    }
    assert loop.run("do the report skill") is None
    assert loop._pending_improvement_session == "session-2"

    assert loop.run("yes please", resume_goal="do the report skill",
                    resume_observations=[]) is None
    assert ("apply_improvement", {"accept": True}) in fake.calls
    assert loop._pending_improvement_session is None


def test_improvement_offer_declined_on_resume(loop_and_fake):
    loop, fake, spoken = loop_and_fake
    fake.play = lambda name: {
        "status": "done", "message": "done", "improvement_offer": True,
        "session": "session-3",
    }
    loop.run("do the report skill")
    loop.run("no thanks", resume_goal="do the report skill", resume_observations=[])
    assert ("apply_improvement", {"accept": False}) in fake.calls


# --------------------------------------------------------------------- #
# Auto-play + suggestions
# --------------------------------------------------------------------- #

def test_auto_play_runs_matching_skill(loop_and_fake, monkeypatch):
    loop, fake, spoken = loop_and_fake
    monkeypatch.setattr(agent_loop_module, "_skills_runtime_active", lambda: True)
    fake.auto_play_candidate = lambda request: {"name": "Monthly Report",
                                                "match_score": 0.9}
    assert loop.run("generate my monthly report") is None
    assert ("play", {"name": "Monthly Report"}) in fake.calls
    assert any("Monthly Report" in line for line in spoken)


def test_auto_play_skips_file_like_goals(loop_and_fake, monkeypatch):
    loop, fake, spoken = loop_and_fake
    monkeypatch.setattr(agent_loop_module, "_skills_runtime_active", lambda: True)
    fake.auto_play_candidate = lambda request: {"name": "Monthly Report",
                                                "match_score": 0.9}
    brain = MagicMock()
    brain.think.return_value = {
        "reasoning": "r", "response": "ok", "done": True, "ask_user": False,
        "step": None,
    }
    loop.brain = brain
    assert loop.run("open my monthly report file") is None
    assert not any(call[0] == "play" for call in fake.calls)


def test_suggestion_spoken_once(loop_and_fake, monkeypatch):
    loop, fake, spoken = loop_and_fake
    monkeypatch.setattr(agent_loop_module, "_skills_runtime_active", lambda: True)
    suggestion = "I noticed you've performed this task 3 times. Learn it as a skill?"
    fake.observe_request = lambda request: suggestion

    brain = MagicMock()
    brain.think.return_value = {
        "reasoning": "r", "response": "ok", "done": True, "ask_user": False,
        "step": None,
    }
    loop.brain = brain
    assert loop.run("archive old emails") is None
    assert sum("noticed" in line for line in spoken) == 1
    # Second run of a different phrase should not repeat the same suggestion.
    spoken.clear()
    assert loop.run("archive old emails") is None
    assert sum("noticed" in line for line in spoken) == 0
