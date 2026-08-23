"""Gmail adapter for the shared connector contract.

The adapter is intentionally dependency-injected so tests never need an
account and the existing Gmail implementation remains the single API client.
"""

from __future__ import annotations

from collections.abc import Callable
import json

from connectors.base import Connector, ConnectorCapability, ConnectorResult, ConnectorStatus


class GmailConnector(Connector):
    name = "gmail"

    def __init__(self, backend, auth_check: Callable[[], bool] | None = None):
        self.backend = backend
        self._auth_check = auth_check or self._default_auth_check

    @staticmethod
    def _default_auth_check() -> bool:
        try:
            from youtube_auth import TOKEN_PATH

            if not TOKEN_PATH.exists():
                return False
            payload = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
            scopes = set(payload.get("scopes") or [])
            has_gmail_scope = any("gmail" in str(scope).lower() for scope in scopes)
            has_renewable_credentials = bool(payload.get("refresh_token") or payload.get("token"))
            return has_gmail_scope and has_renewable_credentials
        except Exception:
            return False

    def status(self) -> ConnectorStatus:
        return ConnectorStatus.READY if self._auth_check() else ConnectorStatus.AUTH_REQUIRED

    def capabilities(self) -> list[ConnectorCapability]:
        return [
            ConnectorCapability("read", "Read recent email"),
            ConnectorCapability("search", "Search email"),
            ConnectorCapability("summary", "Summarize recent email"),
            ConnectorCapability("reply", "Draft or send a reply", mutating=True, requires_confirmation=True),
            ConnectorCapability("send", "Send a new email", mutating=True, requires_confirmation=True),
            ConnectorCapability("archive", "Archive a message", mutating=True, requires_confirmation=True),
            ConnectorCapability("delete", "Delete a message", mutating=True, requires_confirmation=True),
        ]

    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        try:
            arguments = dict(arguments or {})
            if confirmed and capability in {"send", "reply"}:
                arguments["confirm"] = True
            if capability == "summary":
                limit = arguments.get("limit", 10)
                if arguments.get("unread"):
                    value = self.backend.summarize_unread(limit=limit)
                else:
                    value = self.backend.summarize(self.backend.read(limit=limit))
            else:
                function = getattr(self.backend, capability)
                value = function(**arguments)
            return ConnectorResult.normalize(self.name, capability, value)
        except RuntimeError as exc:
            message = str(exc)
            auth_error = any(word in message.lower() for word in ("credential", "oauth", "auth"))
            return ConnectorResult(
                False,
                error=message,
                retryable=not auth_error,
                connector=self.name,
                capability=capability,
            )
