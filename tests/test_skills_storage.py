"""Storage layer tests: folder layout, CRUD, rename/duplicate/export,
timeline & metadata persistence, usage bookkeeping."""

import json
from pathlib import Path

from skills import storage


def _make_skill(name="Test Skill"):
    folder = storage.ensure_layout(name)
    record = {
        "id": storage.new_skill_id(),
        "name": name,
        "description": "demo",
        "created": storage.now_iso(),
        "tags": ["demo"],
        "version": 1,
    }
    storage.save_skill(record)
    storage.save_timeline(name, {"semantic": [{"type": "click", "target": "OK"}], "raw": []})
    storage.save_metadata(name, {"applications": ["demo.exe"]})
    (folder / "screenshots").mkdir(parents=True, exist_ok=True)
    return record


def test_layout_under_skills_root():
    _make_skill()
    folder = storage.skill_dir("Test Skill")
    assert folder.exists()
    for sub in ("skill.json", "timeline.json", "metadata.json",
                "screenshots", "vision", "playback_logs", ".nova"):
        assert (folder / sub).exists(), sub


def test_list_and_load():
    _make_skill("Alpha")
    _make_skill("Beta")
    names = sorted(s["name"] for s in storage.list_skills())
    assert names == ["Alpha", "Beta"]
    loaded = storage.load_skill("Alpha")
    assert loaded["name"] == "Alpha"
    assert storage.skill_exists("Alpha")
    assert not storage.skill_exists("Missing")


def test_rename_copies_artifacts():
    _make_skill("Old")
    (storage.skill_dir("Old") / "screenshots" / "000001.jpg").write_bytes(b"jpeg")
    record = storage.rename_skill("Old", "New")
    assert record["name"] == "New"
    assert not storage.skill_exists("Old")
    assert storage.skill_dir("New").exists()
    assert (storage.skill_dir("New") / "screenshots" / "000001.jpg").exists()
    assert storage.load_skill("New")["name"] == "New"


def test_delete():
    _make_skill("Doomed")
    assert storage.delete_skill("Doomed")
    assert not storage.skill_exists("Doomed")
    assert not storage.delete_skill("Doomed")


def test_duplicate():
    _make_skill("Original")
    storage.duplicate_skill("Original", "Copy")
    original = storage.load_skill("Original")
    copy = storage.load_skill("Copy")
    assert copy["name"] == "Copy"
    assert copy["id"] != original["id"]
    assert copy["duplicated_from"] == "Original"
    assert storage.load_timeline("Copy")["semantic"]


def test_export_roundtrip(tmp_path):
    _make_skill("Portable")
    archive = storage.export_skill("Portable", tmp_path)
    assert archive is not None and archive.exists()
    storage.delete_skill("Portable")
    imported = storage.import_skill(archive)
    assert imported == "Portable"
    assert storage.load_skill("Portable")["name"] == "Portable"


def test_sanitize_name():
    assert storage.sanitize_name('a/b\\c:d*e?f"g<h>i|') == "a_b_c_d_e_f_g_h_i_"
    assert storage.sanitize_name("  ") == "Unnamed Skill"


def test_update_last_used():
    _make_skill("Used")
    storage.update_last_used("Used", outcome="success")
    record = storage.load_skill("Used")
    assert record["usage"]["count"] == 1
    assert record["last_used"] is not None
    storage.update_last_used("Used", outcome="success")
    assert storage.load_skill("Used")["usage"]["count"] == 2


def test_write_playback_log(tmp_path):
    _make_skill("Logged")
    storage.write_playback_log("Logged", {"result": "success", "steps": 3})
    storage.write_playback_log("Logged", {"result": "success", "steps": 3})
    logs = list((storage.skill_dir("Logged") / "playback_logs").glob("*.json"))
    assert len(logs) == 2
    assert json.loads(logs[0].read_text(encoding="utf-8"))["result"] == "success"


def test_write_json_is_atomic():
    path = Path(storage.skills_root()) / "x.json"
    storage.write_json(path, {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert not path.with_suffix(".json.tmp").exists()
