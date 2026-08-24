"""Google Calendar contract adapter; active only with an injected official backend."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from connectors.base import Connector, ConnectorCapability, ConnectorResult, ConnectorStatus


def local_time_zone() -> str:
    zone = datetime.now().astimezone().tzinfo
    return str(getattr(zone, "key", None) or zone or "UTC")


def _normalize_event(value: dict, default_zone: str) -> dict:
    event = dict(value or {})
    start = event.get("start")
    end = event.get("end")
    if isinstance(start, dict):
        event["start"] = start.get("dateTime") or start.get("date")
        event.setdefault("time_zone", start.get("timeZone"))
    if isinstance(end, dict):
        event["end"] = end.get("dateTime") or end.get("date")
        event.setdefault("time_zone", end.get("timeZone"))
    event["time_zone"] = event.get("time_zone") or default_zone
    event["title"] = event.get("title") or event.get("summary") or "Untitled event"
    return event


class GoogleCalendarConnector(Connector):
    name = "google_calendar"
    display_name = "Google Calendar"

    def __init__(self, backend=None, *, auth_check: Callable[[], bool] | None = None, time_zone: str | None = None):
        self.backend = backend
        self._auth_check = auth_check or (lambda: backend is not None)
        self.time_zone = time_zone or local_time_zone()

    def status(self) -> ConnectorStatus:
        if self.backend is None:
            return ConnectorStatus.UNAVAILABLE
        return ConnectorStatus.READY if self._auth_check() else ConnectorStatus.AUTH_REQUIRED

    def capabilities(self) -> list[ConnectorCapability]:
        ready = self.backend is not None and bool(self._auth_check())
        reason = "Google Calendar API backend and OAuth scopes are not configured" if self.backend is None else "Google Calendar authentication is required"
        return [
            ConnectorCapability("list_events", "List upcoming calendar events with explicit time zones", requires_auth=True, available=ready, unavailable_reason=reason if not ready else "", input_schema={"properties": {"start": {"type": "string"}, "end": {"type": "string"}, "limit": {"type": "integer"}, "time_zone": {"type": "string"}}}),
            ConnectorCapability("search_events", "Search calendar events", requires_auth=True, available=ready, unavailable_reason=reason if not ready else "", input_schema={"required": ["query"], "properties": {"query": {"type": "string"}}}),
            ConnectorCapability("create_event", "Create a calendar event", mutating=True, requires_confirmation=True, idempotent=False, risk_level="high", requires_auth=True, available=ready, unavailable_reason=reason if not ready else "", input_schema={"required": ["title", "start"], "properties": {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "time_zone": {"type": "string"}}}),
            ConnectorCapability("update_event", "Update an existing calendar event", mutating=True, requires_confirmation=True, idempotent=False, risk_level="high", requires_auth=True, available=ready, unavailable_reason=reason if not ready else "", input_schema={"required": ["event_id", "changes"]}),
            ConnectorCapability("delete_event", "Delete an existing calendar event", mutating=True, requires_confirmation=True, idempotent=False, risk_level="critical", requires_auth=True, available=ready, unavailable_reason=reason if not ready else "", input_schema={"required": ["event_id"]}),
        ]

    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        if self.backend is None or not self._auth_check():
            return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "Google Calendar API is not authenticated/configured"})
        method = getattr(self.backend, capability, None)
        if method is None:
            return ConnectorResult.normalize(self.name, capability, {"success": False, "error": f"Calendar backend does not implement '{capability}'"})
        values = dict(arguments or {})
        values.setdefault("time_zone", self.time_zone)
        raw = method(**values)
        if capability in {"list_events", "search_events"}:
            items = raw.get("items", []) if isinstance(raw, dict) else raw
            if items is None:
                items = []
            normalized = [_normalize_event(item, values["time_zone"]) for item in items]
            return ConnectorResult(True, {"events": normalized, "count": len(normalized), "time_zone": values["time_zone"]}, connector=self.name, capability=capability)
        result = ConnectorResult.normalize(self.name, capability, raw)
        if result.success and isinstance(result.data, dict):
            return ConnectorResult(True, _normalize_event(result.data, values["time_zone"]), connector=self.name, capability=capability, metadata=result.metadata)
        return result
