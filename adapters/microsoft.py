"""Microsoft adapter -- Outlook mail, calendar, and To Do via the
Microsoft Graph REST API (OAuth)."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from adapters.api import OAuthRESTAdapter

logger = logging.getLogger(__name__)


class MicrosoftAdapter(OAuthRESTAdapter):
    name = "microsoft"
    display_name = "Microsoft"
    description = ("Outlook mail, calendar and To Do through Microsoft Graph. "
                   "Requires a Microsoft Entra app registration "
                   "(MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET in Settings). "
                   "Nova can then read and send email, schedule events and "
                   "manage your task list.")
    capabilities = [
        "read_messages", "search_messages", "send_message", "reply_to_message",
        "identify_sender", "inspect_attachment", "download_attachment",
        "create_event", "update_event", "delete_event", "list_events",
        "create_task", "list_tasks", "create_reminder",
    ]

    # ------------------------------------------------------------------ #
    def _build_oauth(self):
        from connections.oauth import OAuthConfig, OAuthHelper

        config = OAuthConfig(
            service_name="microsoft",
            client_id=_env("MICROSOFT_CLIENT_ID"),
            client_secret=_env("MICROSOFT_CLIENT_SECRET"),
            auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            scopes=[
                "Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite",
                "Tasks.ReadWrite", "User.Read", "offline_access",
            ],
            redirect_port=8766,
        )
        return OAuthHelper(config)

    def _http(self):
        client = super()._http()
        client.base_url = "https://graph.microsoft.com/v1.0"
        return client

    # ------------------------------------------------------------------ #
    # Mail
    # ------------------------------------------------------------------ #
    def read_messages(self, limit=20, **kwargs):
        client = self._http()
        params = {"$top": min(int(limit), 50)}
        if kwargs.get("query"):
            params["$search"] = kwargs["query"]
        data = client.get("/me/messages", params=params)
        messages = [{"id": m.get("id"), "sender": _from(m),
                     "subject": m.get("subject"), "preview": m.get("bodyPreview", ""),
                     "timestamp": m.get("receivedDateTime")}
                    for m in data.get("value") or []]
        return self._ok(messages=messages, count=len(messages))

    def search_messages(self, query, limit=20, **kwargs):
        return self.read_messages(limit=limit, query=query)

    def send_message(self, recipient, text, **kwargs):
        client = self._http()
        body = {
            "message": {
                "subject": kwargs.get("subject") or "Message from Jarvis",
                "body": {"contentType": "Text", "content": text},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
            },
            "saveToSentItems": True,
        }
        client.post("/me/sendMail", json_body=body)
        return self._ok(recipient=recipient)

    def reply_to_message(self, message_id, text, **kwargs):
        client = self._http()
        body = {"comment": text}
        client.post(f"/me/messages/{message_id}/reply", json_body=body)
        return self._ok(message_id=message_id)

    def identify_sender(self, message, **kwargs):
        return self._ok(sender=(message or {}).get("sender"))

    def inspect_attachment(self, message, index=0, **kwargs):
        client = self._http()
        mid = (message or {}).get("id")
        if not mid:
            return self._fail("Message has no id.")
        data = client.get(f"/me/messages/{mid}/attachments",
                          params={"$top": "10"})
        items = data.get("value") or []
        if not items or index >= len(items):
            return self._fail("No attachment at that index.")
        item = items[index]
        return self._ok(filename=item.get("name"),
                        mime_type=item.get("contentType"),
                        size=item.get("size"),
                        attachment_id=item.get("id"))

    def download_attachment(self, message, index=0, destination=None, **kwargs):
        client = self._http()
        mid = (message or {}).get("id")
        if not mid:
            return self._fail("Message has no id.")
        data = client.get(f"/me/messages/{mid}/attachments",
                          params={"$top": "10"})
        items = data.get("value") or []
        if not items or index >= len(items):
            return self._fail("No attachment at that index.")
        item = items[index]
        raw = base64.b64decode(item.get("contentBytes") or "")
        folder = Path(destination) if destination else Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (item.get("name") or "attachment.bin")
        path.write_bytes(raw)
        return self._ok(path=str(path), filename=item.get("name"), bytes=len(raw))

    # ------------------------------------------------------------------ #
    # Calendar
    # ------------------------------------------------------------------ #
    def create_event(self, summary, start=None, end=None, **kwargs):
        client = self._http()
        body = {"subject": summary,
                "start": {"dateTime": start, "timeZone": "UTC"},
                "end": {"dateTime": end or start, "timeZone": "UTC"}}
        ev = client.post("/me/events", json_body=body)
        return self._ok(id=ev.get("id"), title=summary, start=start)

    def update_event(self, event_id, **kwargs):
        client = self._http()
        body = {}
        if kwargs.get("summary"):
            body["subject"] = kwargs["summary"]
        if kwargs.get("start"):
            body["start"] = {"dateTime": kwargs["start"], "timeZone": "UTC"}
        if kwargs.get("end"):
            body["end"] = {"dateTime": kwargs["end"], "timeZone": "UTC"}
        ev = client.patch(f"/me/events/{event_id}", json_body=body)
        return self._ok(id=ev.get("id"), updated=True)

    def delete_event(self, event_id, **kwargs):
        client = self._http()
        client.delete(f"/me/events/{event_id}")
        return self._ok(id=event_id, deleted=True)

    def list_events(self, start=None, end=None, limit=20, **kwargs):
        client = self._http()
        params = {"$top": min(int(limit), 50), "$orderby": "start/dateTime"}
        if start and end:
            params["$filter"] = (f"start/dateTime ge '{start}' and "
                                 f"end/dateTime le '{end}'")
        data = client.get("/me/events", params=params)
        events = [{"id": e.get("id"), "title": e.get("subject"),
                   "start": (e.get("start") or {}).get("dateTime"),
                   "end": (e.get("end") or {}).get("dateTime")}
                  for e in data.get("value") or []]
        return self._ok(events=events, count=len(events))

    # ------------------------------------------------------------------ #
    # To Do
    # ------------------------------------------------------------------ #
    def create_task(self, title, due=None, **kwargs):
        client = self._http()
        lists = client.get("/me/todo/lists").get("value") or []
        default_id = lists[0]["id"] if lists else None
        if not default_id:
            created = client.post("/me/todo/lists",
                                  json_body={"displayName": "Jarvis"})
            default_id = created.get("id")
        body = {"title": title}
        if due:
            body["dueDateTime"] = {"dateTime": due, "timeZone": "UTC"}
        task = client.post(f"/me/todo/lists/{default_id}/tasks", json_body=body)
        return self._ok(id=task.get("id"), title=title, due=due)

    def list_tasks(self, limit=50, **kwargs):
        client = self._http()
        lists = client.get("/me/todo/lists").get("value") or []
        tasks = []
        for folder in lists[:3]:
            data = client.get(f"/me/todo/lists/{folder['id']}/tasks",
                              params={"$top": str(min(int(limit), 50))})
            tasks.extend({"id": t.get("id"), "title": t.get("title"),
                          "completed": bool(t.get("completedDateTime"))}
                         for t in data.get("value") or [])
        return self._ok(tasks=tasks, count=len(tasks))

    def create_reminder(self, text, when=None, **kwargs):
        return self.create_task(title=text, due=when)


def _from(msg: dict) -> str:
    sender = msg.get("from") or {}
    return (sender.get("emailAddress") or {}).get("address", "")


def _env(key: str) -> str:
    import os
    return os.getenv(key, "")