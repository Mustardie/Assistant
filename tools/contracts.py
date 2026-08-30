"""Stable tool-call contracts used by the agent runtime.

Legacy tools may return strings, lists, dictionaries, or raise exceptions.
This module gives the decision loop one truthful shape without forcing every
tool implementation to be rewritten at once.
"""

from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping


class ToolStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    EMPTY = "empty"
    STALE = "stale"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ToolResult:
    tool: str
    status: ToolStatus
    success: bool
    data: Any = None
    error: str | None = None
    retryable: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_legacy(cls, tool: str, executor_success: bool, result: Any) -> "ToolResult":
        reported_success = executor_success
        error = None
        stale = False
        retryable = False
        confirmation_required = False
        if isinstance(result, dict):
            status = str(result.get("status") or "").lower()
            confirmation_required = bool(
                result.get("requires_confirmation")
                or status == "confirmation_required"
            )
            error_shaped = bool(result.get("error") or result.get("error_message")) and result.get("success") is not True
            partial = bool(result.get("partial") or status == "partial")
            if result.get("success") is False or status in {"error", "failed", "failure"} or error_shaped or partial:
                reported_success = False
            error = (result.get("error") or result.get("error_message") or result.get("message")) if not reported_success else None
            stale = bool(result.get("stale"))
            retryable = bool(result.get("retryable"))
        if confirmation_required:
            return cls(
                tool, ToolStatus.BLOCKED, False, result,
                str(error or "Explicit confirmation is required before this action."),
                False, {"requires_confirmation": True},
            )
        if stale:
            return cls(tool, ToolStatus.STALE, False, result, error or "Tool returned stale data", True)
        if not reported_success:
            return cls(tool, ToolStatus.ERROR, False, result, str(error or result), retryable)
        if result is None or result == "" or result == []:
            return cls(tool, ToolStatus.EMPTY, True, result, metadata={"empty": True})
        return cls(tool, ToolStatus.SUCCESS, True, result)


@dataclass(frozen=True)
class ToolAssessment:
    allowed: bool
    tool: str
    arguments: dict
    reason: str = ""
    requires_confirmation: bool = False


@dataclass(frozen=True)
class Verification:
    succeeded: bool
    sufficient: bool
    retryable: bool
    reason: str
    should_continue: bool


class ToolDecisionLayer:
    """Validates necessity, capability, arguments, and risk before a call."""

    _HIGH_RISK = {
        "file_delete", "delete_file", "delete_folder", "gmail_delete",
        "gmail_send", "shutdown", "restart", "purchase", "submit",
        "app_close_confirmed", "process_kill_confirmed",
        "desktop_monitor_start", "desktop_monitor_stop",
        "desktop_startup_enable_confirmed", "desktop_startup_disable",
        "desktop_habit_delete", "desktop_activity_clear",
        "capability_delete",
        "type_text", "press_key", "hotkey", "left_click", "double_click",
        "right_click", "scroll", "move_mouse",
    }
    _CONFIRMATION_KEYS = ("confirm", "confirmed", "user_confirmed")

    def __init__(self, tools: Mapping[str, Callable]):
        self.tools = tools

    def assess(self, tool: str, arguments: Any, *, confirmed: bool = False) -> ToolAssessment:
        if not tool or tool not in self.tools:
            return ToolAssessment(False, tool or "", {}, f"Unknown tool: {tool}")
        if not isinstance(arguments, dict):
            return ToolAssessment(False, tool, {}, "Tool arguments must be an object")

        function = self.tools[tool]
        normalized = dict(arguments)
        try:
            signature = inspect.signature(function)
            parameters = signature.parameters
        except (TypeError, ValueError):
            parameters = {}

        aliases = {"tabId": "tab", "tab_id": "tab", "filepath": "path", "file_path": "path"}
        for old, new in aliases.items():
            if old in normalized and new in parameters and new not in normalized:
                normalized[new] = normalized.pop(old)

        if parameters and not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            unexpected = sorted(set(normalized) - set(parameters))
            if unexpected:
                return ToolAssessment(False, tool, normalized, f"Unexpected arguments: {', '.join(unexpected)}")
            missing = [
                name for name, parameter in parameters.items()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                and name not in normalized
            ]
            if missing:
                return ToolAssessment(False, tool, normalized, f"Missing required arguments: {', '.join(missing)}")

        explicit = confirmed or any(bool(normalized.get(key)) for key in self._CONFIRMATION_KEYS)
        if tool in {"capability_execute", "capability_validate", "learned_skill_execute"} and not explicit:
            try:
                from capabilities.service import default_capability_service

                service = default_capability_service()
                if tool == "learned_skill_execute":
                    skill = service.store.get_skill(str(normalized.get("skill_id") or ""))
                    dynamic_permissions = list(skill.permissions) if skill else []
                    dynamic_confirmation = bool(set(dynamic_permissions) & service.permission_policy.APPROVAL_REQUIRED)
                else:
                    dynamic_confirmation, dynamic_permissions = service.requires_confirmation(
                        str(normalized.get("capability_id") or "")
                    )
                if dynamic_confirmation:
                    return ToolAssessment(
                        False, tool, normalized,
                        "Synthesized capability requires approval for: " + ", ".join(dynamic_permissions),
                        requires_confirmation=True,
                    )
            except Exception:
                # The capability facade repeats the permission check. A
                # lookup error here must never turn into authorization.
                return ToolAssessment(
                    False, tool, normalized,
                    "Could not resolve synthesized capability permissions",
                    requires_confirmation=True,
                )
        if tool in {"file_move", "move_file"} and not explicit:
            source = normalized.get("source") or normalized.get("path")
            if source:
                try:
                    from tools.file_intelligence import assess_file_action

                    file_safety = assess_file_action(source, "move")
                    if file_safety.get("risk") in {"high", "critical"}:
                        return ToolAssessment(
                            False,
                            tool,
                            normalized,
                            file_safety.get("reason") or "Moving this important file requires confirmation",
                            requires_confirmation=True,
                        )
                except Exception:
                    # The tool itself repeats this guard.  Do not turn a
                    # classifier availability problem into an invented risk.
                    pass
        if tool in self._HIGH_RISK and not explicit:
            return ToolAssessment(
                False,
                tool,
                normalized,
                f"{tool} can cause an external or destructive side effect",
                requires_confirmation=True,
            )
        if confirmed and "confirm" in parameters:
            normalized["confirm"] = True
        return ToolAssessment(True, tool, normalized)


class ToolResultVerifier:
    def verify(self, result: ToolResult, *, expected_output: str = "") -> Verification:
        if result.status == ToolStatus.BLOCKED:
            return Verification(False, False, False, result.error or "Confirmation required", False)
        if result.status == ToolStatus.STALE:
            return Verification(False, False, True, "The result is stale", True)
        if result.status == ToolStatus.ERROR:
            obvious_argument_error = bool(result.error and ("missing" in result.error.lower() or "argument" in result.error.lower()))
            return Verification(False, False, result.retryable or obvious_argument_error, result.error or "Tool failed", True)
        if result.status == ToolStatus.EMPTY:
            search_like = any(word in result.tool for word in ("search", "read", "list", "find"))
            reason = "The tool succeeded but returned no data"
            return Verification(True, not search_like and not expected_output, search_like, reason, True)
        if isinstance(result.data, dict) and result.data.get("contradicts_request"):
            return Verification(True, False, False, "The result contradicts the request", True)
        return Verification(True, True, False, "Verified", False)
