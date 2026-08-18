"""Shared fixtures for the Premiere layer tests.

The bridge is replaced by a recording fake. That is not a substitute for
running against Premiere -- it cannot tell us whether ExtendScript accepts a
call -- but it does verify the half of the system that is ours: that a plan
validates, expands into the right primitive operations, with the right times,
values and ordering, and that failures are classified and reported correctly.

The fake speaks the same envelope as the real panel, so a test asserting
"animate produced these keyframes" is asserting on the exact payload the panel
would have received.
"""
from __future__ import annotations

import pytest

from premiere.engine import EditEngine
from premiere.errors import HostError
from premiere.history import History


class FakeBridge:
    """Records every host call and replays canned results."""

    def __init__(self, **overrides):
        self.calls = []
        self.responses = dict(overrides)
        self.failures = {}
        self.connected = True

    # -- transport surface used by the engine --------------------------
    def health(self):
        return {"connected": self.connected, "project_open": True,
                "project": "Test.prproj", "version": "25.0.0"}

    def require_connected(self):
        if not self.connected:
            from premiere.errors import BridgeError
            raise BridgeError("Cannot reach Adobe Premiere Pro")
        return self.health()

    def call(self, op, params=None, *, timeout=None):
        self.calls.append((op, params or {}))
        if op in self.failures:
            raise self.failures[op]
        if op in self.responses:
            value = self.responses[op]
            return value(params) if callable(value) else value
        return self._default(op, params or {})

    def call_batch(self, operations, *, on_error="abort", timeout=None):
        """Emulate the panel's /batch route, including its abort semantics."""
        results = []
        for index, entry in enumerate(operations):
            try:
                value = self.call(entry["op"], entry.get("params"))
                results.append({"op": entry["op"], "index": index, "ok": True,
                                "result": value})
            except Exception as exc:  # noqa: BLE001 - mirrors the panel
                results.append({
                    "op": entry["op"], "index": index, "ok": False,
                    "error": {"code": getattr(exc, "code", "host_error"),
                              "message": str(getattr(exc, "message", exc))},
                })
                if on_error == "abort":
                    break
        return {"results": results,
                "applied": len([r for r in results if r["ok"]])}

    # -- canned host behaviour -----------------------------------------
    @staticmethod
    def _default(op, params):
        if op == "sequence.info":
            return {"name": "Main", "fps": 30.0, "width": 1920, "height": 1080,
                    "duration": 120.0, "video_tracks": 3, "audio_tracks": 3}
        if op == "timeline.revision":
            return {"revision": "rev1"}
        if op == "clip.resolve":
            return [{"id": "clip-1", "name": "shot.mp4", "start": 10.0,
                     "end": 20.0, "duration": 10.0, "in_point": 0.0,
                     "out_point": 10.0, "track": "V1"}]
        if op == "property.get":
            return {"value": 100.0, "animated": False}
        if op == "project.checkpoint":
            return {"path": "/tmp/checkpoint.prproj"}
        if op == "overlay.place":
            return {"placed": params.get("name", "overlay"), "clip_id": "clip-9"}
        if op == "adjustment.create":
            return {"created": True, "clip_id": "adj-1"}
        return {"ok": True}

    # -- helpers for assertions ----------------------------------------
    def ops(self):
        return [op for op, _ in self.calls]

    def payload(self, op):
        """The params of the first call to `op`."""
        for name, params in self.calls:
            if name == op:
                return params
        raise AssertionError(f"{op} was never called. Called: {self.ops()}")

    def payloads(self, op):
        return [params for name, params in self.calls if name == op]

    def fail(self, op, error):
        self.failures[op] = error


@pytest.fixture
def bridge():
    return FakeBridge()


@pytest.fixture
def history(tmp_path):
    return History(path=str(tmp_path / "history.json"))


@pytest.fixture
def engine(bridge, history):
    return EditEngine(bridge=bridge, history=history)


@pytest.fixture
def host_error():
    return HostError
