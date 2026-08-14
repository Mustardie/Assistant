"""Confirmation / safety policy engine.

Classifies tool actions into risk levels and decides whether user
confirmation is required before execution:

    LOW      -- read, search, summarize, create local draft
    MEDIUM   -- create reminder/calendar event, move/download files
    HIGH     -- send message/email, delete files, system settings,
                destructive commands

Rules:
- HIGH actions require confirmation unless the user has explicitly
  enabled automation for that action (an "automation policy").
- MEDIUM actions require confirmation unless enabled.
- The agent must never claim an action succeeded unless verification
  confirms it -- enforcement lives in the agent loop (VERIFY step).

Automation policy sources (highest priority first):
  1. Explicit per-action override passed to requires_confirmation()
  2. Per-capability automation flags stored in memory/config
  3. A global "auto mode" flag
"""

import logging

logger = logging.getLogger(__name__)

# Action -> risk level. Anything not listed defaults to LOW.
RISK_LEVELS = {
    # HIGH: sends or destroys things / changes the environment.
    "send_message": "high",
    "send_email": "high",
    "reply_to_message": "high",
    "delete_event": "high",
    "delete_file": "high",
    "delete_folder": "high",
    "delete_messages": "high",
    "modify_system_settings": "high",
    "shutdown": "high",
    "terminate_process": "high",
    "execute_code": "high",
    "install_software": "high",
    "create_email": "high",
    # MEDIUM: creates/structures things or moves data.
    "create_event": "medium",
    "create_task": "medium",
    "create_reminder": "medium",
    "update_event": "medium",
    "update_task": "medium",
    "move_file": "medium",
    "download_attachment": "medium",
    "download_file": "medium",
    "compress_archive": "medium",
    "extract_archive": "medium",
    "create_folder": "medium",
    "copy_file": "medium",
    "rename_file": "medium",
    # LOW: read/search/summarize/draft.
    "read_message": "low",
    "read_document": "low",
    "search": "low",
    "summarize": "low",
    "create_draft": "low",
}


def risk_level(action: str) -> str:
    """Return 'high' | 'medium' | 'low' for an action name."""
    key = (action or "").strip().lower()
    return RISK_LEVELS.get(key, "low")


# --------------------------------------------------------------------- #
# Automation policy persistence (per action + global flag)
# --------------------------------------------------------------------- #

_AUTO_ACTIONS: set[str] = set()
_GLOBAL_AUTO = False


def set_global_auto_mode(enabled: bool):
    """Enable/disable global automation (bypass all confirmations)."""
    global _GLOBAL_AUTO
    _GLOBAL_AUTO = bool(enabled)
    logger.info("[Policy] Global auto mode set to %s", _GLOBAL_AUTO)


def is_global_auto_mode() -> bool:
    return _GLOBAL_AUTO


def enable_action(action: str):
    """Whitelist a specific action so it no longer needs confirmation."""
    _AUTO_ACTIONS.add((action or "").strip().lower())


def disable_action(action: str):
    _AUTO_ACTIONS.discard((action or "").strip().lower())


def list_auto_actions() -> list[str]:
    return sorted(_AUTO_ACTIONS)


def requires_confirmation(action: str, *, confirm: bool | None = None) -> bool:
    """Decide whether a given action call needs confirmation first.

    Returns False (no confirmation) when:
      - an explicit caller decision was provided (confirm param), OR
      - the action is whitelisted in automation policy, OR
      - global auto mode is on, OR
      - the action's risk is LOW.

    Returns True (must confirm) otherwise.
    """
    action = (action or "").strip().lower()
    if confirm is not None:
        return not bool(confirm)
    if _GLOBAL_AUTO or action in _AUTO_ACTIONS:
        return False
    return risk_level(action) in ("high", "medium")


def describe_policy() -> dict:
    return {
        "global_auto_mode": _GLOBAL_AUTO,
        "auto_actions": sorted(_AUTO_ACTIONS),
        "risk_levels": RISK_LEVELS,
    }