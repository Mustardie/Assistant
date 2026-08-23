"""Versioned, portable contract for recorded skills."""

from __future__ import annotations

from copy import deepcopy


CURRENT_SCHEMA_VERSION = 2
RISKY_STEP_TYPES = {"delete", "send", "submit", "purchase", "shutdown", "restart"}


def required_tools_for(steps: list[dict]) -> list[str]:
    mapping = {
        "click": "left_click",
        "double_click": "double_click",
        "right_click": "right_click",
        "type": "type_text",
        "press": "press_key",
        "hotkey": "hotkey",
        "scroll": "scroll",
        "wait": "wait",
    }
    tools = {mapping.get(str(step.get("type", "")).lower()) for step in steps}
    return sorted(tool for tool in tools if tool)


def build_definition(record: dict, timeline: dict, metadata: dict) -> dict:
    steps = timeline.get("semantic") or []
    name = str(record.get("name") or "Unnamed Skill")
    trigger_phrases = record.get("trigger_phrases") or [
        f"run the {name} skill",
        f"do the {name} skill",
    ]
    risky = any(
        str(step.get("type", "")).lower() in RISKY_STEP_TYPES
        or step.get("requires_confirmation")
        for step in steps
    )
    return {
        **record,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "version": max(1, int(record.get("version") or 1)),
        "trigger_phrases": trigger_phrases,
        "required_tools": record.get("required_tools") or required_tools_for(steps),
        "preconditions": record.get("preconditions") or {
            "windows": metadata.get("windows") or record.get("required_windows") or [],
            "permissions": metadata.get("permissions") or record.get("required_permissions") or [],
        },
        "examples": record.get("examples") or [{"request": trigger_phrases[0]}],
        "failure_handling": record.get("failure_handling") or {
            "max_retries": 1,
            "on_missing_element": "pause_and_ask",
            "on_tool_failure": "stop_and_report",
        },
        "safety": record.get("safety") or {
            "confirmation_required": risky,
            "confirm_before": sorted(RISKY_STEP_TYPES),
        },
        "test_mode": record.get("test_mode") or {
            "supported": True,
            "executes_actions": False,
        },
    }


def validate_definition(record: dict, timeline: dict | None = None) -> list[str]:
    timeline = timeline or {}
    errors = []
    if not isinstance(record, dict):
        return ["skill.json must contain an object"]
    if not str(record.get("name") or "").strip():
        errors.append("skill name is required")
    schema_version = record.get("schema_version", 1)
    if not isinstance(schema_version, int) or schema_version < 1 or schema_version > CURRENT_SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {schema_version}")
    triggers = record.get("trigger_phrases", [])
    if schema_version >= 2 and (
        not isinstance(triggers, list) or not any(str(item).strip() for item in triggers)
    ):
        errors.append("at least one trigger phrase is required")
    if not isinstance(timeline.get("semantic", []), list):
        errors.append("timeline semantic steps must be a list")
    return errors


def migrate(record: dict, timeline: dict, metadata: dict) -> dict:
    migrated = build_definition(deepcopy(record), timeline, metadata)
    errors = validate_definition(migrated, timeline)
    if errors:
        raise ValueError("Invalid skill definition: " + "; ".join(errors))
    return migrated

