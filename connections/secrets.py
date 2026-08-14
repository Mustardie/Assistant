"""Secure local token store for integration API keys.

Tokens entered through the Connections UI are saved here (the Nova data
folder, which is outside the repo and never committed) instead of the
repo's tracked .env file. Environment variables always take precedence,
so existing .env setups keep working:

    get_token("discord_bot_token")
        -> os.getenv("DISCORD_BOT_TOKEN") or stored value
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from config.paths import get_nova_data_dir

logger = logging.getLogger(__name__)

_TOKEN_FILE = get_nova_data_dir() / "integration_tokens.json"


def load_tokens() -> dict:
    try:
        if _TOKEN_FILE.exists():
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        logger.exception("Failed to load integration tokens")
    return {}


def get_token(key: str) -> str:
    """Return the token for a config key. Env vars win over the store."""
    env_value = os.getenv((key or "").upper(), "")
    if env_value:
        return str(env_value).strip()
    return str(load_tokens().get(key, "") or "").strip()


def save_token(key: str, value: str) -> None:
    """Persist a token so it survives restarts (never touches .env)."""
    tokens = load_tokens()
    tokens[key] = str(value or "").strip()
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(
            json.dumps(tokens, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        logger.info("[Secrets] Saved integration token for '%s'", key)
    except Exception:
        logger.exception("[Secrets] Failed to persist token for '%s'", key)


def clear_token(key: str) -> None:
    tokens = load_tokens()
    if key in tokens:
        del tokens[key]
        try:
            _TOKEN_FILE.write_text(
                json.dumps(tokens, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception:
            logger.exception("[Secrets] Failed to remove token for '%s'", key)


def token_file_path() -> Path:
    return _TOKEN_FILE