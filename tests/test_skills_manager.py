"""SkillManager tests: record/pause/stop flow (mocked recorder), editing
operations, playback sessions (mocked player), improvement application."""

from pathlib import Path

from skills import storage
from skills.manager import SkillManager

EVENTS = [
    {"t": 1.0, "type": "window", "title": "Word", "app": "WINWORD.EXE"},
    {"t": 2.0, "type": "click", "button": "left", "x": 100, "y": 50, "group": 5},
    {"t": 2.1, "type": "key", "action": "down", "name": "h"},
    {"t": 2.2, "type": "key", "action": "down", "name": "i"},
    {"t": 2.3, "type": "key", "action": "down", "name": "enter"},
]


class FakeRecorder:
    def __init__(self, session_dir):
        self.session_dir = Path(session_dir)
        self._running = False
        self._paused = False
        self._extra_events = []

    def start(self):
        self._running = True

    def stop(self):
        self._running = False
        return {
            "events": list(EVENTS) + list(self._extra_events),
            "screenshots": [],
            "vision": {},
            "screen": {"width": 1920, "height": 1080, "scale": 1.0},
        }

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def cancel(self):
        self._running = False

    def add_narration(self, text):
        self._extra_events.append({"t": 1.5, "type": "narration", "text": text})

    @property
    def is_recording(self):
        return self._running

    @property
    def is_paused(self):
        return self._paused


class FakeSession:
    def __init__(self, name, status="done", improvement=False, pending=None):
        self.name = name
        self.status = status
        self.pending_question = pending or "What should the title be?"
        self.improvement_offer = improvement
        self.step_index = 3
        self.total_steps = 4
        self.failure_reason = None
        self.timeline = {
            "semantic": [{"type": "click", "t": i, "target": "x"} for i in range(5)],
            "raw": [], "variables": [], "screen": {},
        }
        self._actual_steps = [
            {"type": "click", "t": i, "target": "x"} for i in range(2)
        ]
        self._answer_log = []

    def step_until_blocked(self):
        return self.status

    def answer(self, text):
        self._answer_log.append(text)
        self.status = "done"

    def cancel(self):
        self.status = "cancelled"


def _manager_with_fake_recorder(monkeypatch):
    monkeypatch.setattr("skills.recorder.Recorder", FakeRecorder)
    return SkillManager()


def test_record_and_stop_saves_skill(tmp_path, monkeypatch):
    manager = _manager_with_fake_recorder(monkeypatch)
    started = manager.start_recording()
    assert started["success"] and manager.is_recording

    # No name given inline -> the manager holds the recording and asks,
    # instead of silently saving under a guessed name.
    stopped = manager.stop_recording()
    assert stopped["success"]
    assert stopped.get("need_name")
    assert not manager.is_recording
    assert manager.awaiting_skill_name

    result = manager.finish_recording("My Skill")
    assert result["success"]
    assert "Saved skill" in result["speak"]
    assert result["name"] == "My Skill"
    assert not manager.awaiting_skill_name

    folder = storage.skill_dir(result["name"])
    assert (folder / "skill.json").exists()
    assert (folder / "timeline.json").exists()
    assert (folder / "metadata.json").exists()
    record = storage.load_skill(result["name"])
    assert record["applications"] == ["WINWORD.EXE"]
    timeline = storage.load_timeline(result["name"])
    assert timeline["semantic"]
    assert not manager.is_recording


def test_stop_without_name_then_finish_with_instructions(monkeypatch):
    """finish_recording() splits the reply into a name plus any extra
    instructions tacked on after a comma/'and'."""
    manager = _manager_with_fake_recorder(monkeypatch)
    manager.start_recording()
    stopped = manager.stop_recording()
    assert stopped.get("need_name")

    result = manager.finish_recording(
        "school notes, read the title before downloading"
    )
    assert result["success"]
    assert result["name"] == "school notes"
    timeline = storage.load_timeline("school notes")
    assert "read the title before downloading" in timeline["instructions"]


def test_stop_with_explicit_name(monkeypatch):
    manager = _manager_with_fake_recorder(monkeypatch)
    manager.start_recording()
    result = manager.stop_recording("Create Word Doc")
    assert result["success"]
    assert result["name"] == "Create Word Doc"
    assert storage.skill_exists("Create Word Doc")


def test_pause_resume_cancel(monkeypatch):
    manager = _manager_with_fake_recorder(monkeypatch)
    manager.start_recording()
    assert manager.pause_recording()["success"] and manager.is_paused
    assert manager.resume_recording()["success"] and not manager.is_paused
    cancelled = manager.cancel_recording()
    assert cancelled["success"]
    assert not manager.is_recording
    # Cancel removed the draft folder.
    assert storage.list_skills() == []


def test_narration_captured_and_persisted(tmp_path, monkeypatch):
    manager = _manager_with_fake_recorder(monkeypatch)
    manager.start_recording()
    note = manager.add_narration("read the title of the document before downloading")
    assert note["captured"]
    assert note["success"]
    result = manager.stop_recording("School Files")
    assert result["success"]
    timeline = storage.load_timeline("School Files")
    assert "read the title of the document before downloading" in timeline.get("instructions", [])
    meta = storage.load_metadata("School Files")
    assert "read the title" in meta.get("instructions", [])[0]


def test_narration_ignored_when_not_recording():
    manager = SkillManager()
    note = manager.add_narration("some instruction")
    assert not note["captured"]


def test_recording_commands_require_active_session():
    manager = SkillManager()
    assert not manager.pause_recording()["success"]
    assert not manager.stop_recording()["success"]


def test_editing_operations(tmp_path, monkeypatch):
    manager = _manager_with_fake_recorder(monkeypatch)
    manager.start_recording()
    manager.stop_recording("Rename Me")
    manager.start_recording()
    manager.stop_recording("Delete Me")

    renamed = manager.rename_skill("Rename Me", "New Name")
    assert renamed["success"] and storage.skill_exists("New Name")
    assert not storage.skill_exists("Rename Me")

    duplicated = manager.duplicate_skill("New Name", "Clone")
    assert duplicated["success"] and storage.skill_exists("Clone")

    exported = manager.export_skill("Clone", tmp_path)
    assert exported["success"] and Path(exported["path"]).exists()

    deleted = manager.delete_skill("Delete Me")
    assert deleted["success"] and not storage.skill_exists("Delete Me")

    assert manager.rename_skill("Missing", "X")["success"] is False
    assert manager.delete_skill("Missing")["success"] is False


def test_playback_done_flow(monkeypatch):
    manager = SkillManager()
    storage.save_skill({"id": "x", "name": "Monthly Report", "tags": [],
                        "created": storage.now_iso(), "version": 1})
    storage.save_timeline("Monthly Report", {"semantic": [], "raw": [], "screen": {}})
    monkeypatch.setattr("skills.player.PlaybackSession",
                        lambda name: FakeSession(name, status="done"))

    result = manager.play("Monthly Report")
    assert result["status"] == "done"
    assert "Done!" in result["message"]
    assert manager.active_session is None


def test_playback_need_input_then_continue(monkeypatch):
    manager = SkillManager()
    storage.save_skill({"id": "x", "name": "Word Doc", "tags": [],
                        "created": storage.now_iso(), "version": 1})
    storage.save_timeline("Word Doc", {"semantic": [], "raw": [], "screen": {}})
    monkeypatch.setattr("skills.player.PlaybackSession",
                        lambda name: FakeSession(name, status="need_input"))

    result = manager.play("Word Doc")
    assert result["status"] == "need_input"
    assert manager.active_session is not None
    session = manager.active_session

    result = manager.continue_playback(session, "Physics Notes")
    assert result["status"] == "done"
    assert session._answer_log == ["Physics Notes"]
    assert manager.active_session is None


def test_playback_unknown_skill_suggests_close(monkeypatch):
    manager = SkillManager()
    storage.save_skill({"id": "x", "name": "Create Word Doc", "tags": [],
                        "created": storage.now_iso(), "version": 1})
    result = manager.play("Word Documents")
    assert result["status"] == "failed"
    assert "Create Word Doc" in result["message"]


def test_abort_playback(monkeypatch):
    manager = SkillManager()
    storage.save_skill({"id": "x", "name": "Skill A", "tags": [],
                        "created": storage.now_iso(), "version": 1})
    storage.save_timeline("Skill A", {"semantic": [], "raw": [], "screen": {}})
    monkeypatch.setattr("skills.player.PlaybackSession",
                        lambda name: FakeSession(name, status="need_input"))
    manager.play("Skill A")
    result = manager.abort_playback()
    assert result["status"] == "cancelled"


def test_apply_improvement_rewrites_timeline(monkeypatch):
    manager = SkillManager()
    storage.save_skill({"id": "x", "name": "Optimize Me", "tags": [],
                        "created": storage.now_iso(), "version": 1})
    storage.save_timeline("Optimize Me", {
        "semantic": [{"type": "click", "t": i} for i in range(5)],
        "raw": [], "screen": {},
    })
    session = FakeSession("Optimize Me", status="done", improvement=True)
    session.timeline = storage.load_timeline("Optimize Me")

    result = manager.apply_improvement(session, accept=True)
    assert result["success"]
    timeline = storage.load_timeline("Optimize Me")
    assert len(timeline["semantic"]) == 2
    record = storage.load_skill("Optimize Me")
    assert record["version"] == 2

    declined = manager.apply_improvement(session, accept=False)
    assert "original" in declined["speak"].lower()


def test_observe_request_delegates(monkeypatch):
    manager = SkillManager()
    assert manager.observe_request("open spotify daily") is None  # rare -> none
    assert manager.find_skill("something random") is None
