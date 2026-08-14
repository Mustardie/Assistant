"""Local Qwen access via Ollama.

Preferred client: the official `ollama` Python package (already present in the
JARVIS environment).  Fallback: plain HTTP against the Ollama REST API.  If
neither works, `generate()` returns None and callers fall back to
deterministic templates -- the pipeline never hard-depends on the LLM being
online (set config.LLM_REQUIRED = True to change that).
"""
from __future__ import annotations

import requests

from config import (LLM_MAX_TOKENS, LLM_REQUIRED, LLM_TEMPERATURE,
                    LLM_TIMEOUT_SECONDS, OLLAMA_BASE_URL, OLLAMA_MODEL)
from utils.logging import get_logger

log = get_logger(__name__)


def ollama_available(model: str | None = None) -> bool:
    model = model or OLLAMA_MODEL
    try:
        import ollama
        tags = ollama.list()
        names = [m.model for m in getattr(tags, "models", [])]
        if any(n.startswith(model.split(":")[0]) for n in names):
            return True
    except Exception:
        pass
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def generate(system: str, prompt: str, model: str | None = None,
             temperature: float = LLM_TEMPERATURE, max_tokens: int = LLM_MAX_TOKENS,
             timeout: int = LLM_TIMEOUT_SECONDS) -> str | None:
    """Run a chat completion against local Qwen.  Returns None when offline."""
    model = model or OLLAMA_MODEL
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    try:
        import ollama
        resp = ollama.chat(model=model, messages=messages, options={
            "temperature": temperature, "num_predict": max_tokens,
        })
        return resp["message"]["content"].strip()
    except Exception as exc:
        log.debug("ollama python package failed: %s", exc)
    try:
        resp = requests.post(f"{OLLAMA_BASE_URL}/api/chat", json={
            "model": model,
            "stream": False,
            "messages": messages,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    except Exception as exc:
        log.warning("Ollama unreachable (%s). Using deterministic report.", exc)
        if LLM_REQUIRED:
            raise RuntimeError("LLM is required (config.LLM_REQUIRED=True) but unreachable")
        return None
