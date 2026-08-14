"""Blender specialist for Nova: BlenderLLM as a library, not a subprocess.

The BlenderLLM project (AI_Models/blenderllm) is a self-contained terminal
specialist: its own Ollama model (qwen2.5-coder:14b), its own system prompt
(PLAN / BLENDER PYTHON / NOTES), its own curated knowledge base (18 topics)
and its own explicit localhost bridge into a running Blender 5.2 instance.

Nova never spawns that REPL. This module loads BlenderLLM's building blocks
(knowledge, generation, system prompt, bridge client, config) into memory
under unique module names and exposes them as Nova tools:

    blender_generate(request)      -> generate Blender Python (PLAN /
                                      BLENDER PYTHON / NOTES reply + extracted
                                      script). Generation only -- nothing is
                                      ever executed.
    blender_execute(code, confirm) -> run the last generated script inside
                                      Blender 5.2. Requires explicit user
                                      approval (confirm=True); never runs
                                      automatically.
    blender_status()               -> diagnostics: model, knowledge topics,
                                      whether the Blender bridge is reachable.
    blender_session_clear()        -> reset the specialist's conversation.

The specialist keeps its own bounded conversation history so follow-ups
("make it bigger", "now give it a material") have context without polluting
Nova's planner conversation.

The LLM call uses Nova's Ollama client (llm/ollama_client.py) pinned to
BlenderLLM's model -- Nova's environment does not need the `ollama` pip
package, and the model stays the code-specialist qwen2.5-coder:14b rather
than Nova's planner model.

SAFETY: execution is never automatic. blender_execute() requires
confirm=True, and the planner prompt (brain/brain.py) tells the model to
always ask the user first -- the same contract as file_delete/delete_folder.
The bridge itself binds to 127.0.0.1 only.
"""

from __future__ import annotations

import importlib.util
import logging
import socket
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_BLENDERLLM_DIR = Path(__file__).resolve().parents[1] / "AI_Models" / "blenderllm"

# Bounded conversation of the Blender specialist (mirrors BlenderLLM's
# terminal `history` list, capped so it cannot grow forever).
_MAX_HISTORY_MESSAGES = 12

_bllm = None  # cache of the loaded BlenderLLM modules

history: list[dict] = []  # [{"role": "user"|"assistant", "content": str}, ...]
last_code: str | None = None  # most recent extracted script, like BlenderLLM's session["last_code"]


class BlenderSpecialistError(Exception):
    """The BlenderLLM project cannot be loaded or is unusable."""


def _load_blenderllm() -> dict:
    """Load BlenderLLM's modules under unique names.

    BlenderLLM's modules internally do `from config import ...` while Nova
    also has a `config` package. To avoid the collision, BlenderLLM's own
    config module is loaded first and temporarily registered under the name
    "config" while the other modules execute (they capture the names they
    need at import time), then Nova's config module is restored. All loaded
    modules are pure Python (stdlib only).
    """
    global _bllm
    if _bllm is not None:
        return _bllm
    if not _BLENDERLLM_DIR.is_dir():
        raise BlenderSpecialistError(
            f"BlenderLLM project not found at {_BLENDERLLM_DIR}"
        )

    def _load(name: str, filename: str):
        module_name = f"_blenderllm_{name}"
        spec = importlib.util.spec_from_file_location(
            module_name, _BLENDERLLM_DIR / filename
        )
        if spec is None or spec.loader is None:
            raise BlenderSpecialistError(
                f"cannot import {_BLENDERLLM_DIR / filename}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        return module, spec

    files = (
        ("config", "config.py"),
        ("system_prompt", "system_prompt.py"),
        ("generation", "generation.py"),
        ("knowledge", "knowledge.py"),
        ("bridge", "blender_bridge.py"),
    )
    loaded: dict = {}
    real_config = sys.modules.get("config")
    config_module = None
    try:
        config_module, config_spec = _load("config", "config.py")
        sys.modules["config"] = config_module  # shim for `from config import ...`
        config_spec.loader.exec_module(config_module)
        for name, filename in files[1:]:
            module, spec = _load(name, filename)
            spec.loader.exec_module(module)
            loaded[name] = module
    except Exception as exc:
        logger.exception("Failed to load BlenderLLM modules")
        raise BlenderSpecialistError(
            f"failed to load BlenderLLM: {exc}"
        ) from exc
    finally:
        if real_config is not None:
            sys.modules["config"] = real_config
        else:
            sys.modules.pop("config", None)
    loaded["config"] = config_module
    _bllm = loaded
    return _bllm


def _make_client():
    """Nova's Ollama client pinned to BlenderLLM's code-specialist model."""
    from llm.factory import make_llm_client

    return make_llm_client("ollama", model=_load_blenderllm()["config"].MODEL)


def _probe_bridge() -> str:
    """Short status string for the Blender bridge (localhost only)."""
    bllm = _load_blenderllm()
    host = bllm["config"].BLENDER_BRIDGE_HOST
    port = bllm["config"].BLENDER_BRIDGE_PORT
    try:
        with socket.create_connection((host, port), timeout=2):
            return f"reachable ({host}:{port})"
    except OSError:
        return (
            f"not reachable ({host}:{port}) - is Blender open with the "
            "BlenderLLM Bridge add-on started?"
        )


def _format_history() -> str:
    """Render the specialist's conversation as a transcript for the model."""
    if not history:
        return ""
    lines = ["PREVIOUS CONVERSATION:"]
    for message in history[-_MAX_HISTORY_MESSAGES:]:
        role = "User" if message.get("role") == "user" else "Assistant"
        content = str(message.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    lines.append("")
    lines.append("CURRENT REQUEST:")
    return "\n".join(lines) + "\n"


def _append_history(role: str, content: str) -> None:
    if not (content or "").strip():
        return
    history.append({"role": role, "content": content})
    del history[:-_MAX_HISTORY_MESSAGES]


def blender_generate(request: str | None = None) -> dict:
    """Generate Blender Python code with the BlenderLLM specialist.

    Generation only -- nothing is executed. Returns:
        {"success": True, "reply": <full model reply>,
         "code": <extracted script or None>, "code_ready": bool}
    """
    request = (request or "").strip()
    if not request:
        return {
            "success": False,
            "error": (
                "Missing argument 'request'. What should the script do? "
                "(e.g. 'create a cube in Blender')"
            ),
        }
    try:
        bllm = _load_blenderllm()
    except BlenderSpecialistError as exc:
        return {"success": False, "error": str(exc)}

    config_mod = bllm["config"]
    kb = bllm["knowledge"].KnowledgeBase() if config_mod.KNOWLEDGE_ENABLED else None
    code_request = bllm["generation"].is_code_request(request)
    system_content = bllm["system_prompt"].SYSTEM_PROMPT
    if kb is not None and kb.loaded:
        context = kb.select_context(request)
        if context:
            system_content += "\n\n" + context
    if code_request:
        system_content += "\n\n" + bllm["system_prompt"].CODE_FORMAT_GUIDANCE

    user_prompt = _format_history() + request

    try:
        reply = _make_client().chat_text(system_content, user_prompt)
    except Exception as exc:
        logger.warning("Blender specialist model call failed: %s", exc)
        return {
            "success": False,
            "error": (
                "The Blender specialist model isn't available right now: "
                f"{exc} (is Ollama running, and is '{config_mod.MODEL}' "
                "pulled?)"
            ),
        }

    reply = (reply or "").strip()
    _append_history("user", request)
    _append_history("assistant", reply)
    code = bllm["generation"].extract_python_code(reply)
    global last_code
    if code:
        last_code = code
    return {
        "success": True,
        "reply": reply,
        "code": code,
        "code_ready": bool(code),
    }


def blender_execute(code: str | None = None, confirm: bool = False) -> dict:
    """Execute the last generated Blender script inside Blender 5.2.

    This RUNS Python inside Blender. It must never be called with
    confirm=True automatically: the planner asks the user first and only
    passes confirm=True after an explicit 'yes'. The script goes through
    the localhost BlenderLLM bridge -- Blender must be open with the
    BlenderLLM Bridge add-on started.
    """
    try:
        bllm = _load_blenderllm()
    except BlenderSpecialistError as exc:
        return {"success": False, "error": str(exc)}

    target = (code or "").strip() or (last_code or "").strip()
    if not target:
        return {
            "success": False,
            "error": (
                "No generated Blender code to execute. Ask me to generate a "
                "script first (e.g. 'create a cube in Blender')."
            ),
        }
    if not confirm:
        return {
            "success": False,
            "confirm_required": True,
            "error": (
                "This executes Python inside Blender. Ask the user for "
                "explicit permission before calling blender_execute with "
                "confirm=True."
            ),
        }
    try:
        result = bllm["bridge"].send_script(target)
    except Exception as exc:
        return {"success": False, "error": f"Blender bridge error: {exc}"}

    if result.get("status") == "SUCCESS":
        response = {"success": True, "message": "Blender: execution successful."}
        stdout = (result.get("stdout") or "").strip()
        if stdout:
            response["stdout"] = stdout
        return response

    response = {"success": False, "error": "Blender: execution failed."}
    if result.get("error_type"):
        response["error_type"] = result["error_type"]
    if result.get("error"):
        response["error"] = f"Blender: execution failed. {result['error']}"
    traceback_text = (result.get("traceback") or "").strip()
    if traceback_text:
        response["traceback"] = traceback_text
    return response


def blender_status() -> dict:
    """Diagnostics for the Blender specialist."""
    try:
        bllm = _load_blenderllm()
    except BlenderSpecialistError as exc:
        return {"success": False, "error": str(exc)}

    config_mod = bllm["config"]
    status: dict = {
        "success": True,
        "model": config_mod.MODEL,
        "ollama_host": config_mod.OLLAMA_HOST,
        "bridge": {
            "host": config_mod.BLENDER_BRIDGE_HOST,
            "port": config_mod.BLENDER_BRIDGE_PORT,
            "status": _probe_bridge(),
        },
        "knowledge_enabled": bool(config_mod.KNOWLEDGE_ENABLED),
        "conversation_length": len(history),
        "has_pending_code": bool(last_code),
    }
    try:
        kb = bllm["knowledge"].KnowledgeBase()
        status["knowledge_topics"] = kb.topics if kb.loaded else []
    except Exception as exc:
        status["knowledge_topics"] = []
        status["knowledge_error"] = str(exc)
    return status


def blender_session_clear() -> dict:
    """Reset the Blender specialist's conversation (and pending code)."""
    history.clear()
    global last_code
    last_code = None
    return {
        "success": True,
        "message": "Blender specialist conversation cleared.",
    }
