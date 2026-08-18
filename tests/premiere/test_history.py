"""Operation history, revision protection and checkpoints."""
from __future__ import annotations

import json

import pytest

from premiere.errors import RevisionError
from premiere.history import History, _trim


@pytest.fixture
def log(tmp_path):
    return History(path=str(tmp_path / "history.json"))


class TestRecording:
    def test_records_and_reads_back(self, log):
        log.record(ops=[{"op": "clip.remove", "params": {"ripple": True}}],
                   results=[{"success": True}], revision="r1", label="cleanup")
        entries = log.recent()
        assert len(entries) == 1
        assert entries[0]["label"] == "cleanup"
        assert entries[0]["ops"][0]["op"] == "clip.remove"

    def test_newest_first(self, log):
        for i in range(3):
            log.record(ops=[{"op": f"op{i}", "params": {}}], results=[])
        assert [e["ops"][0]["op"] for e in log.recent()] == ["op2", "op1", "op0"]

    def test_persists_across_instances(self, tmp_path):
        path = str(tmp_path / "h.json")
        History(path=path).record(ops=[{"op": "clip.move", "params": {}}],
                                  results=[], revision="r9")
        reloaded = History(path=path)
        assert reloaded.recent()[0]["ops"][0]["op"] == "clip.move"
        assert reloaded.revision == "r9"

    def test_capped(self, tmp_path):
        log = History(path=str(tmp_path / "h.json"), max_entries=5)
        for i in range(12):
            log.record(ops=[{"op": f"op{i}", "params": {}}], results=[])
        assert len(log.recent(limit=100)) == 5

    def test_corrupt_file_does_not_block_editing(self, tmp_path):
        path = tmp_path / "h.json"
        path.write_text("{ this is not json")
        log = History(path=str(path))
        log.record(ops=[{"op": "clip.move", "params": {}}], results=[])
        assert len(log.recent()) == 1

    def test_note_is_kept(self, log):
        log.record(ops=[{"op": "animate", "params": {}, "note": "punch in on beat"}],
                   results=[])
        assert log.recent()[0]["ops"][0]["note"] == "punch in on beat"


class TestTrimming:
    def test_large_lists_are_summarised(self):
        trimmed = _trim({"keyframes": list(range(600))})
        assert trimmed["keyframes"] == "<600 items>"

    def test_small_lists_are_kept(self):
        assert _trim({"points": [1, 2, 3]})["points"] == [1, 2, 3]

    def test_long_strings_are_truncated(self):
        assert _trim({"text": "x" * 5000})["text"].endswith("...")

    def test_a_baked_batch_stays_small(self, log):
        log.record(
            ops=[{"op": "keyframe.set",
                  "params": {"keyframes": [{"time": i, "value": i}
                                           for i in range(500)]}}],
            results=[{"success": True}],
        )
        size = len(json.dumps(log.recent()))
        assert size < 4000, "history entry ballooned"


class TestRevisions:
    def test_matching_revision_passes(self, log):
        log.check_revision("r1", "r1")

    def test_no_expectation_always_passes(self, log):
        log.check_revision(None, "r5")

    def test_mismatch_is_refused(self, log):
        with pytest.raises(RevisionError) as excinfo:
            log.check_revision("r1", "r2")
        assert "timeline.snapshot" in excinfo.value.hint

    def test_unknown_current_revision_does_not_block(self, log):
        # If the host cannot report a revision, refusing every edit would be
        # worse than proceeding.
        log.check_revision("r1", None)

    def test_recording_updates_the_revision(self, log):
        log.record(ops=[], results=[], revision="r42")
        assert log.revision == "r42"


class TestUndoBookkeeping:
    def test_only_mutating_entries_are_undoable(self, log):
        log.record(ops=[{"op": "timeline.snapshot", "params": {}}],
                   results=[], undo_steps=0)
        assert log.last_undoable() is None
        log.record(ops=[{"op": "clip.remove", "params": {}}],
                   results=[], undo_steps=1)
        assert log.last_undoable()["ops"][0]["op"] == "clip.remove"

    def test_pop_removes_from_the_log(self, log):
        log.record(ops=[{"op": "clip.remove", "params": {}}], results=[],
                   undo_steps=1)
        assert len(log.pop_undoable(1)) == 1
        assert log.last_undoable() is None

    def test_pop_skips_read_only_entries(self, log):
        log.record(ops=[{"op": "clip.move", "params": {}}], results=[], undo_steps=1)
        log.record(ops=[{"op": "timeline.snapshot", "params": {}}], results=[],
                   undo_steps=0)
        popped = log.pop_undoable(1)
        assert popped[0]["ops"][0]["op"] == "clip.move"


class TestCheckpoints:
    def test_save_and_get(self, log):
        log.save_checkpoint("before-grade", {"path": "/tmp/x.prproj", "auto": False})
        assert log.get_checkpoint("before-grade")["path"] == "/tmp/x.prproj"
        assert "at" in log.get_checkpoint("before-grade")

    def test_missing_checkpoint_is_none(self, log):
        assert log.get_checkpoint("nope") is None

    def test_listing(self, log):
        log.save_checkpoint("a", {"path": "/a"})
        log.save_checkpoint("b", {"path": "/b"})
        assert set(log.list_checkpoints()) == {"a", "b"}
