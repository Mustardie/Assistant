import zipfile

from skills import schema, storage
from skills.manager import SkillManager
from skills.player import PlaybackSession


def test_schema_contains_record_playback_contract():
    record = schema.build_definition(
        {"name": "Create Notes", "version": 1},
        {"semantic": [{"type": "click"}, {"type": "type"}]},
        {"windows": ["Notepad"]},
    )
    assert record["schema_version"] == schema.CURRENT_SCHEMA_VERSION
    assert record["trigger_phrases"]
    assert record["required_tools"] == ["left_click", "type_text"]
    assert record["preconditions"]["windows"] == ["Notepad"]
    assert record["failure_handling"]["max_retries"] == 1
    assert record["test_mode"]["executes_actions"] is False
    assert schema.validate_definition(record, {"semantic": []}) == []


def test_skill_test_mode_does_not_execute(tmp_path):
    storage.save_timeline("Dry Run", {"semantic": [{"type": "click"}]})
    storage.save_metadata("Dry Run", {})
    storage.save_skill(schema.build_definition({"name": "Dry Run"}, storage.load_timeline("Dry Run"), {}))
    result = SkillManager().test_skill("Dry Run")
    assert result["success"]
    assert result["steps"] == 1
    assert result["executed"] is False


def test_import_rejects_zip_path_traversal(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Bad/skill.json", '{"name": "Bad", "schema_version": 2, "trigger_phrases": ["bad"]}')
        zf.writestr("Bad/../escaped.txt", "should not escape")
    assert storage.import_skill(archive) is None
    assert not (storage.skills_root() / "escaped.txt").exists()


def test_playback_requires_explicit_confirmation_for_sensitive_step():
    storage.save_skill(schema.build_definition({"name": "Sensitive"}, {"semantic": []}, {}))
    storage.save_timeline("Sensitive", {
        "semantic": [{"type": "wait", "seconds": 0, "requires_confirmation": True}],
    })
    session = PlaybackSession("Sensitive")
    assert session.step_until_blocked() == "need_input"
    session.answer("yes")
    assert session.step_until_blocked() == "done"


def test_playback_cancellation_is_not_overwritten_by_running_state():
    storage.save_skill(schema.build_definition({"name": "Cancel Sensitive"}, {"semantic": []}, {}))
    storage.save_timeline("Cancel Sensitive", {
        "semantic": [{"type": "wait", "seconds": 0, "requires_confirmation": True}],
    })
    session = PlaybackSession("Cancel Sensitive")
    assert session.step_until_blocked() == "need_input"
    session.answer("no")
    assert session.status == "cancelled"
