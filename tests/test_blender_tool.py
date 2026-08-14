"""Unit tests for the Blender specialist (tools/blender_tool.py).

Never touches Ollama, Blender, or the network: the LLM client and the
bridge sender are mocked. The BlenderLLM module loader itself is pure
Python (stdlib only) and runs for real where it matters (loading,
knowledge base, code extraction, history).
"""

import types
from unittest.mock import MagicMock, patch

import pytest

import tools.blender_tool as blender_tool
from brain.agent_loop import AgentLoop, _detect_complex_task
from tools.tool_registry import TOOLS, run_tool

CUBE_REPLY = (
    "PLAN\nAdd a cube at the origin.\n\n"
    "BLENDER PYTHON\n```python\nimport bpy\n"
    "bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))\n```\n\n"
    "NOTES\nModern Blender; adds a cube to the active collection."
)


@pytest.fixture(autouse=True)
def _reset_blender_tool_state():
    """Every test starts with a clean specialist: no history, no pending
    code, and the BlenderLLM module cache cleared so real module loading
    (stdlib only) happens fresh."""
    blender_tool.history.clear()
    blender_tool.last_code = None
    blender_tool._bllm = None
    yield


class _FakeClient:
    def __init__(self, reply):
        self._reply = reply
        self.system_prompt = ""
        self.user_prompt = ""

    def chat_text(self, system_prompt, user_prompt):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self._reply


def _patch_client(monkeypatch, reply):
    fake = _FakeClient(reply)
    monkeypatch.setattr(blender_tool, "_make_client", lambda: fake)
    return fake


# --------------------------------------------------------------------------- #
# Registry integration
# --------------------------------------------------------------------------- #


def test_blender_tools_registered_in_registry():
    for name in (
        "blender_generate",
        "blender_execute",
        "blender_status",
        "blender_session_clear",
    ):
        assert name in TOOLS


def test_run_tool_blender_generate_missing_request():
    success, result = run_tool("blender_generate", {})
    assert success is True  # the tool reports failure via dict, not raise
    assert result["success"] is False
    assert "Missing argument 'request'" in result["error"]


# --------------------------------------------------------------------------- #
# blender_generate
# --------------------------------------------------------------------------- #


def test_blender_generate_returns_reply_and_extracts_code(monkeypatch):
    fake = _patch_client(monkeypatch, CUBE_REPLY)
    result = blender_tool.blender_generate("create a cube in blender")

    assert result["success"] is True
    assert "PLAN" in result["reply"]
    assert result["code_ready"] is True
    assert "bpy.ops.mesh.primitive_cube_add" in result["code"]
    assert blender_tool.last_code == result["code"]
    assert len(blender_tool.history) == 2


def test_blender_generate_keeps_specialist_history(monkeypatch):
    blender_tool.history.append({"role": "user", "content": "first request"})
    blender_tool.history.append({"role": "assistant", "content": "first reply"})

    fake = _patch_client(monkeypatch, "second reply")
    blender_tool.blender_generate("make the cube bigger")

    assert "first request" in fake.user_prompt
    assert "first reply" in fake.user_prompt
    assert "make the cube bigger" in fake.user_prompt
    assert len(blender_tool.history) == 4


def test_blender_generate_applies_code_guidance_for_code_requests(monkeypatch):
    fake = _patch_client(monkeypatch, "ok")
    blender_tool.blender_generate("write a blender python script that creates a spiral")

    assert "BLENDER PYTHON" in fake.system_prompt  # CODE_FORMAT_GUIDANCE marker
    assert "You are BlenderLLM" in fake.system_prompt  # BlenderLLM system prompt


def test_blender_generate_without_code_in_reply(monkeypatch):
    _patch_client(monkeypatch, "Blender has no command line interface like that.")
    result = blender_tool.blender_generate("what is blender")

    assert result["success"] is True
    assert result["code_ready"] is False
    assert result["code"] is None
    assert blender_tool.last_code is None


def test_blender_generate_reports_model_failure(monkeypatch):
    def _broken_client():
        raise RuntimeError("Could not reach Ollama")

    monkeypatch.setattr(blender_tool, "_make_client", _broken_client)
    result = blender_tool.blender_generate("create a cube in blender")

    assert result["success"] is False
    assert "Ollama" in result["error"] or "model" in result["error"]


# --------------------------------------------------------------------------- #
# blender_execute
# --------------------------------------------------------------------------- #


def _patch_bridge(monkeypatch, send_script):
    fake_bridge = types.SimpleNamespace(send_script=send_script)
    monkeypatch.setattr(
        blender_tool,
        "_load_blenderllm",
        lambda: {"bridge": fake_bridge, "config": None},
    )


def test_blender_execute_no_generated_code():
    result = blender_tool.blender_execute()
    assert result["success"] is False
    assert "No generated Blender code" in result["error"]


def test_blender_execute_requires_explicit_confirmation(monkeypatch):
    blender_tool.last_code = "import bpy"
    _patch_bridge(monkeypatch, lambda script: {"status": "SUCCESS"})

    result = blender_tool.blender_execute()
    assert result["success"] is False
    assert result.get("confirm_required") is True
    assert "confirm" in result["error"]


def test_blender_execute_success_with_confirmation(monkeypatch):
    blender_tool.last_code = "import bpy"
    sent = []
    _patch_bridge(monkeypatch, lambda script: sent.append(script) or {
        "status": "SUCCESS", "stdout": "cube added", "stderr": "",
    })

    result = blender_tool.blender_execute(confirm=True)
    assert result["success"] is True
    assert sent == ["import bpy"]
    assert "cube added" in result.get("stdout", "")


def test_blender_execute_prefers_explicit_code_argument(monkeypatch):
    blender_tool.last_code = "old script"
    sent = []
    _patch_bridge(monkeypatch, lambda script: sent.append(script) or {
        "status": "SUCCESS", "stdout": "", "stderr": "",
    })

    result = blender_tool.blender_execute(code="new script", confirm=True)
    assert result["success"] is True
    assert sent == ["new script"]


def test_blender_execute_bridge_not_running(monkeypatch):
    blender_tool.last_code = "import bpy"

    def _send(script):
        raise RuntimeError("cannot reach the Blender bridge at 127.0.0.1:41987")

    _patch_bridge(monkeypatch, _send)
    result = blender_tool.blender_execute(confirm=True)

    assert result["success"] is False
    assert "Blender bridge error" in result["error"]
    assert "cannot reach" in result["error"]


def test_blender_execute_reports_script_error(monkeypatch):
    blender_tool.last_code = "import bpy"
    _patch_bridge(monkeypatch, lambda script: {
        "status": "ERROR",
        "error_type": "BlenderExecutionError",
        "error": "bpy not found",
        "traceback": "Traceback (most recent call last)...",
    })

    result = blender_tool.blender_execute(confirm=True)
    assert result["success"] is False
    assert result["error_type"] == "BlenderExecutionError"
    assert "bpy not found" in result["error"]
    assert "Traceback" in result["traceback"]


# --------------------------------------------------------------------------- #
# blender_status / blender_session_clear
# --------------------------------------------------------------------------- #


def test_blender_status_reports_diagnostics(monkeypatch):
    monkeypatch.setattr(blender_tool, "_probe_bridge", lambda: "not reachable")
    result = blender_tool.blender_status()

    assert result["success"] is True
    assert result["model"] == "qwen2.5-coder:14b"
    assert result["knowledge_enabled"] is True
    assert result["knowledge_topics"]  # real KB loads in tests (read-only)
    assert result["bridge"]["port"] == 41987
    assert result["has_pending_code"] is False


def test_blender_session_clear_resets_history_and_code():
    blender_tool.history.append({"role": "user", "content": "hi"})
    blender_tool.last_code = "import bpy"

    result = blender_tool.blender_session_clear()
    assert result["success"] is True
    assert blender_tool.history == []
    assert blender_tool.last_code is None


# --------------------------------------------------------------------------- #
# Agent-loop integration (deterministic routing)
# --------------------------------------------------------------------------- #


def test_detect_complex_task_identifies_blender():
    assert _detect_complex_task("create a cube in blender") == "blender"
    assert _detect_complex_task("write a bpy script for a spiral staircase") == "blender"
    assert _detect_complex_task("what is the weather today") is None


def test_agent_loop_runs_blender_generate_deterministically():
    brain = MagicMock()
    spoken = []
    loop = AgentLoop(
        brain,
        speak=spoken.append,
        record_tool=lambda tool, result: None,
        fallback=None,
    )
    reply = (
        "PLAN\nAdd a cube.\n\nBLENDER PYTHON\n```python\nimport bpy\n"
        "bpy.ops.mesh.primitive_cube_add()\n```\n\nNOTES\nModern Blender."
    )
    with patch(
        "brain.agent_loop.run_tool",
        return_value=(True, {
            "success": True,
            "reply": reply,
            "code": "import bpy\nbpy.ops.mesh.primitive_cube_add()",
            "code_ready": True,
        }),
    ) as mock_run:
        result = loop.run("create a cube in blender")

    assert result is None
    mock_run.assert_called_once_with(
        "blender_generate", {"request": "create a cube in blender"}
    )
    assert any("PLAN" in s for s in spoken)
    assert brain.think.call_count == 0  # planner never asked


def test_agent_loop_blender_generate_failure_is_reported():
    brain = MagicMock()
    spoken = []
    loop = AgentLoop(
        brain,
        speak=spoken.append,
        record_tool=lambda tool, result: None,
        fallback=None,
    )
    with patch(
        "brain.agent_loop.run_tool",
        return_value=(False, {"success": False, "error": "model down"}),
    ):
        result = loop.run("create a cube in blender")

    assert result is None
    assert any("couldn't generate Blender code" in s for s in spoken)
    assert brain.think.call_count == 0
