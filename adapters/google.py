"""Google adapter -- Gmail, Calendar, Tasks via the official REST APIs.

Two auth paths, chosen automatically:

    * Account flow (default): reuses the existing Google OAuth store used
      by the legacy skills (client_secret.json + token.json in the Nova
      data folder, also overridable via GOOGLE_OAUTH_CLIENT_SECRET_PATH /
      GOOGLE_OAUTH_TOKEN_PATH). Connecting opens Google's consent screen
      once; the resulting token covers youtube + gmail + calendar + tasks.

    * Env OAuth (power users): GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET
      drive the built-in OAuth helper (redirect port 8765).

Implements the capability methods the universal tool layer calls:
    read_messages / search_messages / send_message / reply_to_message /
    identify_sender / inspect_attachment / download_attachment
    create_event / update_event / delete_event / list_events
    create_task / update_task / list_tasks / create_reminder
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from adapters.api import ApiError, OAuthRESTAdapter

logger = logging.getLogger(__name__)

# Scopes shared with the legacy youtube_auth flow so ONE authorization
# covers the old skills and the new adapter. Tokens whose stored scopes
# don't cover all of these are treated as not-authorized (re-auth).
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]


class GoogleAdapter(OAuthRESTAdapter):
    name = "google"
    display_name = "Google"
    description = ("Gmail, Calendar and Tasks through your Google account. "
                   "Connecting opens a one-time consent screen; Nova can then "
                   "read and send email, manage calendar events and keep your "
                   "task list in sync.")
    capabilities = [
        "read_messages", "search_messages", "send_message", "reply_to_message",
        "identify_sender", "inspect_attachment", "download_attachment",
        "create_event", "update_event", "delete_event", "list_events",
        "create_task", "update_task", "list_tasks", "create_reminder",
    ]

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def _build_oauth(self):
        from connections.oauth import OAuthConfig, OAuthHelper

        config = OAuthConfig(
            service_name="google",
            client_id=_env("GOOGLE_CLIENT_ID"),
            client_secret=_env("GOOGLE_CLIENT_SECRET"),
            auth_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            token_refresh_url="https://oauth2.googleapis.com/token",
            scopes=[
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/tasks",
            ],
            redirect_port=8765,
        )
        return OAuthHelper(config)

    def _client_secret_path(self) -> Path:
        env = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET_PATH", "")
        if env:
            return Path(env)
        from config.paths import get_nova_data_dir
        return get_nova_data_dir() / "client_secret.json"

    def _token_path(self) -> Path:
        env = os.getenv("GOOGLE_OAUTH_TOKEN_PATH", "")
        if env:
            return Path(env)
        from config.paths import get_nova_data_dir
        return get_nova_data_dir() / "token.json"

    def _has_env_creds(self) -> bool:
        return bool(os.getenv("GOOGLE_CLIENT_ID")
                    and os.getenv("GOOGLE_CLIENT_SECRET"))

    def _google_creds(self):
        """Load the shared token.json as google-auth Credentials. Returns
        None when the file is missing, expired without a refresh token, or
        its stored scopes don't cover everything we need."""
        path = self._token_path()
        if not path.exists():
            return None
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(str(path), SCOPES)
            stored = set(creds.scopes or [])
            if not stored.issuperset(set(SCOPES)):
                logger.info("Google token lacks required scopes; re-auth needed.")
                return None
            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    path.write_text(creds.to_json(), encoding="utf-8")
            return creds if creds.valid else None
        except Exception as exc:
            logger.warning("Google token load failed: %s", exc)
            return None

    def status(self) -> dict:
        if self._has_env_creds():
            return super().status()
        if self._google_creds() is not None:
            return {"status": "connected",
                    "message": "Google is connected with your account."}
        if self._client_secret_path().exists():
            return {"status": "requires_auth",
                    "message": "Click Connect to authorize Nova with your "
                               "Google account (one-time browser consent)."}
        return {"status": "not_configured",
                "message": "Google needs OAuth client credentials. Put "
                           "client_secret.json in the Nova data folder or set "
                           "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET."}

    def connect(self) -> dict:
        if self._has_env_creds():
            return super().connect()
        if self._google_creds() is not None:
            return self._ok(message="Google is already connected.")
        secret = self._client_secret_path()
        if not secret.exists():
            return self._fail(
                "No Google OAuth client_secret.json found. Set "
                "GOOGLE_OAUTH_CLIENT_SECRET_PATH or place client_secret.json "
                "in the Nova data folder, then click Connect again."
            )
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(
                str(secret), SCOPES)
            creds = flow.run_local_server(port=0)
        except Exception as exc:
            logger.warning("Google consent flow failed: %s", exc)
            return self._fail(f"Google authorization failed: {exc}")
        token_path = self._token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        self._client = None
        return self._ok(message="Google connected with your account.")

    def disconnect(self) -> dict:
        if self._has_env_creds():
            return super().disconnect()
        path = self._token_path()
        if path.exists():
            path.unlink()
        self._client = None
        return {"success": True, "message": "Google disconnected."}

    def _tokens(self) -> dict | None:
        if self._has_env_creds():
            return super()._tokens()
        creds = self._google_creds()
        if creds is None:
            return None
        return {"access_token": creds.token,
                "refresh_token": creds.refresh_token}

    # ------------------------------------------------------------------ #
    def _http(self):
        client = super()._http()
        client.base_url = "https://www.googleapis.com"
        return client

    # ------------------------------------------------------------------ #
    # Gmail: messaging
    # ------------------------------------------------------------------ #
    def read_messages(self, limit=20, **kwargs):
        client = self._http()
        query = kwargs.get("query") or ""
        data = client.get("/gmail/v1/users/me/messages",
                          params={"maxResults": min(int(limit), 50), "q": query})
        messages = data.get("messages") or []
        results = []
        for meta in messages:
            msg = client.get(f"/gmail/v1/users/me/messages/{meta['id']}")
            results.append(_gmail_to_message(msg))
        return self._ok(messages=results, count=len(results))

    def search_messages(self, query, limit=20, **kwargs):
        return self.read_messages(limit=limit, query=query)

    def send_message(self, recipient, text, **kwargs):
        client = self._http()
        subject = kwargs.get("subject") or "Message from Jarvis"
        payload = {
            "raw": base64.urlsafe_b64encode(
                f"To: {recipient}\nSubject: {subject}\n\n{text}".encode("utf-8")
            ).decode("utf-8"),
        }
        sent = client.post("/gmail/v1/users/me/messages/send", json_body=payload)
        return self._ok(id=sent.get("id"), recipient=recipient)

    def reply_to_message(self, message_id, text, **kwargs):
        client = self._http()
        msg = client.get(f"/gmail/v1/users/me/messages/{message_id}")
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "Re: message")
        thread_id = msg.get("threadId")
        raw = (f"To: {headers.get('from', '')}\nSubject: Re: {subject}\n"
               f"References: {headers.get('message-id', '')}\n\n{text}")
        sent = client.post("/gmail/v1/users/me/messages/send",
                           json_body={"raw": base64.urlsafe_b64encode(
                               raw.encode("utf-8")).decode("utf-8")})
        return self._ok(id=sent.get("id"), thread_id=thread_id)

    def identify_sender(self, message, **kwargs):
        sender = (message or {}).get("sender")
        return self._ok(sender=sender)

    def inspect_attachment(self, message, index=0, **kwargs):
        client = self._http()
        mid = (message or {}).get("id")
        if not mid:
            return self._fail("Message has no id.")
        msg = client.get(f"/gmail/v1/users/me/messages/{mid}")
        parts = msg.get("payload", {}).get("parts") or []
        if not parts or index >= len(parts):
            return self._fail("No attachment at that index.")
        part = parts[index]
        return self._ok(
            filename=part.get("filename"),
            mime_type=part.get("mimeType"),
            size=(part.get("body") or {}).get("size", 0),
            attachment_id=(part.get("body") or {}).get("attachmentId"),
        )

    def download_attachment(self, message, index=0, destination=None, **kwargs):
        client = self._http()
        mid = (message or {}).get("id")
        if not mid:
            return self._fail("Message has no id.")
        msg = client.get(f"/gmail/v1/users/me/messages/{mid}")
        parts = msg.get("payload", {}).get("parts") or []
        if not parts or index >= len(parts):
            return self._fail("No attachment at that index.")
        part = parts[index]
        attachment_id = (part.get("body") or {}).get("attachmentId")
        filename = part.get("filename") or "attachment.bin"
        if not attachment_id:
            return self._fail("Attachment has no id to download.")
        data = client.get(
            f"/gmail/v1/users/me/messages/{mid}/attachments/{attachment_id}")
        raw = base64.urlsafe_b64decode((data.get("data") or "").encode("ascii"))
        folder = Path(destination) if destination else Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / filename
        path.write_bytes(raw)
        return self._ok(path=str(path), filename=filename, bytes=len(raw))

    # ------------------------------------------------------------------ #
    # Calendar
    # ------------------------------------------------------------------ #
    def create_event(self, summary, start=None, end=None, **kwargs):
        client = self._http()
        body = {"summary": summary, "start": {"dateTime": start},
                "end": {"dateTime": end or start}}
        ev = client.post("/calendar/v3/calendars/primary/events", json_body=body)
        return self._ok(id=ev.get("id"), title=summary, start=start)

    def update_event(self, event_id, **kwargs):
        client = self._http()
        body = {k: v for k, v in kwargs.items() if k in ("summary", "start", "end")}
        ev = client.patch(f"/calendar/v3/calendars/primary/events/{event_id}", json_body=body)
        return self._ok(id=ev.get("id"), updated=True)

    def delete_event(self, event_id, **kwargs):
        client = self._http()
        client.delete(f"/calendar/v3/calendars/primary/events/{event_id}")
        return self._ok(id=event_id, deleted=True)

    def list_events(self, start=None, end=None, limit=20, **kwargs):
        client = self._http()
        params = {"maxResults": min(int(limit), 250), "orderBy": "startTime",
                  "singleEvents": "true"}
        if start:
            params["timeMin"] = start
        if end:
            params["timeMax"] = end
        data = client.get("/calendar/v3/calendars/primary/events", params=params)
        events = [{"id": e.get("id"), "title": e.get("summary"),
                   "start": _start_of(e), "end": (e.get("end") or {}).get("dateTime")}
                  for e in data.get("items") or []]
        return self._ok(events=events, count=len(events))

    # ------------------------------------------------------------------ #
    # Tasks
    # ------------------------------------------------------------------ #
    def create_task(self, title, due=None, **kwargs):
        client = self._http()
        tasklist = self._default_tasklist(client)
        body = {"title": title}
        if due:
            body["due"] = due
        task = client.post(f"/tasks/v1/lists/{tasklist}/tasks", json_body=body)
        return self._ok(id=task.get("id"), title=title, due=due)

    def update_task(self, task_id, **kwargs):
        client = self._http()
        tasklist = self._default_tasklist(client)
        body = {k: v for k, v in kwargs.items() if k in ("title", "due", "completed")}
        task = client.patch(f"/tasks/v1/lists/{tasklist}/tasks/{task_id}", json_body=body)
        return self._ok(id=task.get("id"), updated=True)

    def list_tasks(self, limit=50, **kwargs):
        client = self._http()
        tasklist = self._default_tasklist(client)
        data = client.get(f"/tasks/v1/lists/{tasklist}/tasks",
                          params={"maxResults": min(int(limit), 100)})
        tasks = [{"id": t.get("id"), "title": t.get("title"),
                  "completed": t.get("status") == "completed", "due": t.get("due")}
                 for t in data.get("items") or []]
        return self._ok(tasks=tasks, count=len(tasks))

    def create_reminder(self, text, when=None, **kwargs):
        return self.create_task(title=text, due=when)

    # ------------------------------------------------------------------ #
    def _default_tasklist(self, client) -> str:
        data = client.get("/tasks/v1/users/@me/lists", params={"maxResults": 1})
        lists = data.get("items") or []
        return lists[0]["id"] if lists else "@default"


def _gmail_to_message(msg: dict) -> dict:
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "timestamp": int(msg.get("internalDate") or 0) / 1000.0,
        "preview": _body_text(msg.get("payload", {})),
    }


def _body_text(payload: dict) -> str:
    body = payload.get("body") or {}
    if body.get("data"):
        return base64.urlsafe_b64decode(body["data"].encode("ascii")).decode("utf-8", errors="replace")
    parts = payload.get("parts") or []
    for part in parts:
        text = _body_text(part)
        if text and part.get("mimeType", "").startswith("text/plain"):
            return text
    return ""


def _start_of(event: dict) -> str:
    start = event.get("start") or {}
    return start.get("dateTime") or start.get("date") or ""


def _env(key: str) -> str:
    import os
    return os.getenv(key, "")