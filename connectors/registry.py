"""Connector discovery, auth gating, normalized errors, and safe retries."""

from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
from typing import Any

from connectors.base import (
    Connector,
    ConnectorActionPlan,
    ConnectorError,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
)


def _result_error(name: str, capability: str, code: str, message: str, *, retryable: bool = False) -> ConnectorResult:
    detail = ConnectorError(code, message, retryable)
    return ConnectorResult(
        False,
        error=message,
        retryable=retryable,
        connector=name,
        capability=capability,
        error_detail=detail,
    )


def _attachment_candidates(value: Any, *, depth: int = 0) -> list[dict]:
    if depth > 4:
        return []
    if isinstance(value, list):
        found: list[dict] = []
        for item in value[:100]:
            found.extend(_attachment_candidates(item, depth=depth + 1))
        return found
    if not isinstance(value, dict):
        return []
    filename = value.get("filename") or value.get("file_name") or value.get("name")
    mime = value.get("mime_type") or value.get("media_type") or value.get("content_type")
    path = value.get("path") or value.get("download_path")
    looks_like_file = bool(mime or path or (filename and Path(str(filename)).suffix))
    found = [value] if looks_like_file else []
    for key in ("attachments", "files", "items", "results", "data"):
        nested = value.get(key)
        if nested is not value:
            found.extend(_attachment_candidates(nested, depth=depth + 1))
    return found


def _file_profiles(name: str, value: Any) -> tuple[dict, ...]:
    candidates = _attachment_candidates(value)
    if not candidates:
        return ()
    from tools.file_intelligence import FileSource, profile_connector_item

    source = {
        "gmail": FileSource.EMAIL_ATTACHMENT,
        "google_drive": FileSource.GOOGLE_DRIVE,
        "drive": FileSource.GOOGLE_DRIVE,
        "browser": FileSource.BROWSER_DOWNLOAD,
        "browser_downloads": FileSource.BROWSER_DOWNLOAD,
        "calendar": FileSource.CALENDAR_ATTACHMENT,
        "discord": FileSource.MESSAGING_MEDIA,
        "whatsapp": FileSource.MESSAGING_MEDIA,
    }.get(name.lower(), FileSource.CONNECTOR)
    profiles = []
    seen = set()
    for candidate in candidates[:50]:
        profile = profile_connector_item(candidate, source=source).to_dict()
        identity = (profile["path"], profile["filename"])
        if identity not in seen:
            profiles.append(profile)
            seen.add(identity)
    return tuple(profiles)


class ConnectorRegistry:
    def __init__(self):
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        if not getattr(connector, "name", None):
            raise ValueError("Connector must have a name")
        self._connectors[connector.name] = connector

    def names(self) -> list[str]:
        return sorted(self._connectors)

    def describe(self, name: str) -> dict:
        connector = self._connectors.get(name)
        if not connector:
            return {"name": name, "display_name": name, "status": ConnectorStatus.UNAVAILABLE.value, "capabilities": [], "available": False, "reason": "Connector is not installed"}
        status = self.status(name)
        capabilities = self.capabilities(name)
        available_count = sum(1 for item in capabilities if item.get("available"))
        unavailable_reasons = list(dict.fromkeys(item.get("unavailable_reason") for item in capabilities if not item.get("available") and item.get("unavailable_reason")))
        return {
            "name": name,
            "display_name": getattr(connector, "display_name", "") or name,
            "status": status.value,
            "available": status in {ConnectorStatus.READY, ConnectorStatus.DEGRADED} and available_count > 0,
            "capabilities": capabilities,
            "available_capabilities": available_count,
            "reason": unavailable_reasons[0] if status != ConnectorStatus.READY and unavailable_reasons else "",
            "limitations": unavailable_reasons,
        }

    def status(self, name: str) -> ConnectorStatus:
        connector = self._connectors.get(name)
        if not connector:
            return ConnectorStatus.UNAVAILABLE
        try:
            value = connector.status()
            return value if isinstance(value, ConnectorStatus) else ConnectorStatus(str(value))
        except Exception:
            return ConnectorStatus.UNAVAILABLE

    def capabilities(self, name: str) -> list[dict]:
        connector = self._connectors.get(name)
        if not connector:
            return []
        try:
            return [capability.to_dict() for capability in connector.capabilities()]
        except Exception:
            return []

    def plan(self, request: ConnectorRequest) -> ConnectorActionPlan:
        connector = self._connectors.get(request.connector)
        if not connector:
            return ConnectorActionPlan(request, ConnectorStatus.UNAVAILABLE, False, reason="Connector is not installed")
        try:
            return connector.plan_action(request)
        except Exception as exc:
            return ConnectorActionPlan(request, ConnectorStatus.DEGRADED, False, reason=f"Capability discovery failed: {exc}")

    def execute(self, name: str, capability: str, arguments: dict, *, confirmed: bool = False, retries: int = 1) -> ConnectorResult:
        connector = self._connectors.get(name)
        if not connector:
            return _result_error(name, capability, "not_installed", f"Connector '{name}' is not installed")
        try:
            raw_status = connector.status()
            status = raw_status if isinstance(raw_status, ConnectorStatus) else ConnectorStatus(str(raw_status))
        except Exception as exc:
            return _result_error(name, capability, "status_check_failed", f"Connector '{name}' status check failed: {exc}")
        if status in {ConnectorStatus.AUTH_REQUIRED, ConnectorStatus.UNAVAILABLE}:
            return _result_error(name, capability, "not_ready", f"Connector '{name}' status is {status.value}")
        request = ConnectorRequest(name, capability, dict(arguments or {}), confirmed)
        plan = self.plan(request)
        if not plan.supported:
            code = "missing_input" if plan.missing_inputs else "unsupported_capability"
            return _result_error(name, capability, code, plan.reason)
        if plan.requires_confirmation and not confirmed:
            return _result_error(name, capability, "confirmation_required", f"'{capability}' requires user confirmation")

        descriptor = next(item for item in connector.capabilities() if item.name == capability)

        safe_to_retry = bool(not descriptor.mutating and descriptor.idempotent)
        attempts = max(1, retries + 1) if safe_to_retry else 1
        result = None
        actual_attempts = 0
        for _ in range(attempts):
            actual_attempts += 1
            try:
                parameters = inspect.signature(connector.execute_action).parameters
                if "request" in parameters:
                    raw = connector.execute_action(request)
                else:
                    raw = connector.execute(capability, arguments, confirmed=confirmed)
                result = ConnectorResult.normalize(name, capability, raw)
            except Exception as exc:
                result = _result_error(name, capability, "execution_failed", str(exc), retryable=False)
            if result.success or not result.retryable or not safe_to_retry:
                break
        assert result is not None
        profiles = _file_profiles(name, result.data) if result.success or result.partial else ()
        metadata = {**result.metadata, "attempts": actual_attempts, "normalized": True}
        return replace(result, file_profiles=profiles, metadata=metadata)

    def test(self, name: str) -> dict:
        """Read-only connector diagnostic; never invokes a mutating capability."""
        status = self.status(name)
        capabilities = self.capabilities(name)
        description = self.describe(name)
        return {
            "connector": name,
            "display_name": description["display_name"],
            "status": status.value,
            "capabilities": capabilities,
            "ready": status == ConnectorStatus.READY,
            "error": None if status == ConnectorStatus.READY else f"Connector status is {status.value}",
            "performed_external_action": False,
            "limitations": description["limitations"],
        }
