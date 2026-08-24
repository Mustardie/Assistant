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
    display_name = "Gmail"

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
            ConnectorCapability("read", "List recent email metadata and body", requires_auth=True, input_schema={"properties": {"limit": {"type": "integer"}}}),
            ConnectorCapability("search", "Search email metadata and body", requires_auth=True, input_schema={"required": ["query"], "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}}),
            ConnectorCapability("read_message", "Read one email by message id", requires_auth=True, input_schema={"required": ["message_id"]}),
            ConnectorCapability("list_attachments", "List attachment metadata without downloading content", requires_auth=True, input_schema={"required": ["message_id"]}),
            ConnectorCapability("summary", "Summarize recent email", requires_auth=True),
            ConnectorCapability("draft_reply", "Prepare a reply preview without sending", requires_auth=True, risk_level="medium", input_schema={"required": ["command"]}),
            ConnectorCapability("send_reply", "Send a confirmed reply", mutating=True, requires_confirmation=True, idempotent=False, risk_level="high", requires_auth=True, input_schema={"required": ["command"]}),
            ConnectorCapability("reply", "Send a confirmed reply (legacy capability)", mutating=True, requires_confirmation=True, idempotent=False, risk_level="high", requires_auth=True, input_schema={"required": ["command"]}),
            ConnectorCapability("send", "Send a new email", mutating=True, requires_confirmation=True, idempotent=False, risk_level="high", requires_auth=True, input_schema={"required": ["to", "subject", "body"]}),
            ConnectorCapability("archive", "Archive a message", mutating=True, requires_confirmation=True, idempotent=False, risk_level="high", requires_auth=True, input_schema={"required": ["message_id"]}),
            ConnectorCapability("delete", "Delete a message", mutating=True, requires_confirmation=True, idempotent=False, risk_level="critical", requires_auth=True, input_schema={"required": ["message_id"]}),
        ]

    @staticmethod
    def _attachment_metadata(raw_message: dict, message_id: str) -> list[dict]:
        attachments = []
        stack = list((raw_message.get("payload") or {}).get("parts") or [])
        while stack:
            part = stack.pop()
            stack.extend(part.get("parts") or [])
            filename = str(part.get("filename") or "").strip()
            body = part.get("body") or {}
            attachment_id = body.get("attachmentId")
            if filename or attachment_id:
                attachments.append({
                    "id": attachment_id or f"{message_id}:{part.get('partId') or len(attachments)}",
                    "attachment_id": attachment_id,
                    "message_id": message_id,
                    "filename": filename or "unnamed-attachment",
                    "mime_type": part.get("mimeType") or "application/octet-stream",
                    "size": body.get("size"),
                    "downloaded": False,
                    "content_inspected": False,
                })
        return attachments

    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        try:
            arguments = dict(arguments or {})
            if confirmed and capability in {"send", "reply"}:
                arguments["confirm"] = True
            if capability == "read_message":
                value = self.backend.get_email_by_id(arguments["message_id"])
            elif capability == "list_attachments":
                message_id = str(arguments["message_id"])
                service = getattr(self.backend, "service", None)
                if service is None or not hasattr(service, "get_message"):
                    value = {"success": False, "error": "Gmail backend cannot expose raw attachment metadata"}
                else:
                    value = {"success": True, "data": {"attachments": self._attachment_metadata(service.get_message(message_id), message_id)}}
            elif capability == "draft_reply":
                value = self.backend.reply(arguments["command"], draft_mode=True, confirm=False)
            elif capability == "send_reply":
                value = self.backend.reply(arguments["command"], draft_mode=False, confirm=confirmed)
            elif capability == "summary":
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
