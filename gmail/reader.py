from typing import Any

from .parser import extract_body, parse_headers


class GmailReader:
    def __init__(self, service):
        self.service = service

    def _parse_message(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = message.get("payload", {})
        headers = parse_headers(payload.get("headers", []))
        body, quoted_history = extract_body(payload)
        if not body:
            body = message.get("snippet", "")

        sender_header = headers.get("From", "Unknown sender")
        sender_name = sender_header
        sender_email = sender_header
        if "<" in sender_header and ">" in sender_header:
            sender_name = sender_header.split("<", 1)[0].strip().strip('"')
            sender_email = sender_header.split("<", 1)[1].split(">", 1)[0].strip()

        return {
            "sender": sender_header,
            "sender_name": sender_name,
            "sender_email": sender_email,
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
            "body": body,
            "quoted_history": quoted_history,
            "message_id": message.get("id"),
            "thread_id": message.get("threadId"),
            "snippet": message.get("snippet", ""),
            "unread": "UNREAD" in (message.get("labelIds") or []),
            "labels": message.get("labelIds", []),
            # RFC headers needed to thread a reply correctly (distinct from Gmail's
            # internal "id"/"threadId" above).
            "rfc_message_id": headers.get("Message-ID", ""),
            "rfc_references": headers.get("References", ""),
        }

    def _fetch_messages(self, message_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        emails = []
        for item in message_refs:
            message_id = item.get("id")
            if not message_id:
                continue
            raw_message = self.service.get_message(message_id)
            emails.append(self._parse_message(raw_message))
        return emails

    def get_unread_emails(self, limit: int = 10) -> list[dict[str, Any]]:
        # -from:me keeps self-sent messages (including stray test replies that
        # ended up back in the inbox) out of the pool entirely.
        result = self.service.list_messages(query="is:unread in:inbox -from:me", max_results=limit)
        message_refs = result.get("messages", [])
        return self._fetch_messages(message_refs)

    def get_recent_emails(self, limit: int = 20) -> list[dict[str, Any]]:
        result = self.service.list_messages(query="in:inbox -from:me", max_results=limit)
        message_refs = result.get("messages", [])
        return self._fetch_messages(message_refs)

    def get_email_by_id(self, message_id: str) -> dict[str, Any]:
        raw_message = self.service.get_message(message_id)
        return self._parse_message(raw_message)

    def search_emails(self, query: str, limit: int = 10, exclude_self: bool = True) -> list[dict[str, Any]]:
        full_query = f"{query} -from:me" if exclude_self and query.strip() else query
        result = self.service.list_messages(query=full_query, max_results=limit)
        message_refs = result.get("messages", [])
        return self._fetch_messages(message_refs)
