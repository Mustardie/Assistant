"""Slack adapter -- messaging via the official Slack Web API (bot token)."""

from __future__ import annotations

import logging

from adapters.api import RESTAdapter

logger = logging.getLogger(__name__)


class SlackAdapter(RESTAdapter):
    name = "slack"
    display_name = "Slack"
    description = ("Team messaging through the Slack API. Needs a bot token "
                   "with chat/read scopes; Nova can read channels, search "
                   "history and post messages.")
    authentication = "api_key"
    config_key = "slack_bot_token"
    api_base_url = "https://slack.com/api"
    capabilities = [
        "read_messages", "search_messages", "send_message", "reply_to_message",
        "identify_sender", "inspect_attachment", "download_attachment",
    ]

    # ------------------------------------------------------------------ #
    def _verify_connection(self):
        data = self._http().get("/auth.test")
        if not data.get("ok"):
            raise _SlackError()

    def status(self) -> dict:
        if not self.is_configured():
            return {"status": "not_configured",
                    "message": "Slack needs a bot token to connect."}
        return {"status": "requires_auth",
                "message": "Slack bot token configured. Click Connect to verify."}

    # ------------------------------------------------------------------ #
    def read_messages(self, limit=20, **kwargs):
        client = self._http()
        channel = kwargs.get("channel") or self._first_channel(client)
        if not channel:
            return self._fail("No Slack channel available.")
        data = client.get("/conversations.history",
                          params={"channel": channel, "limit": min(int(limit), 100)})
        if not data.get("ok"):
            return self._fail(f"Slack history failed: {data.get('error')}")
        messages = [{"id": m.get("ts"), "channel": channel,
                     "sender": (m.get("user") or m.get("bot_id") or ""),
                     "content": m.get("text", ""), "timestamp": m.get("ts")}
                    for m in data.get("messages") or []
                    if not m.get("subtype")]
        return self._ok(messages=messages, count=len(messages))

    def search_messages(self, query, limit=20, **kwargs):
        client = self._http()
        data = client.get("/search.messages",
                          params={"query": query, "count": min(int(limit), 100)})
        if not data.get("ok"):
            return self._fail(f"Slack search failed: {data.get('error')}")
        messages = [{"id": m.get("ts"),
                     "channel": (m.get("channel") or {}).get("id") if isinstance(m.get("channel"), dict) else m.get("channel"),
                     "sender": (m.get("user") or m.get("username") or ""),
                     "content": m.get("text", "")}
                    for m in (data.get("messages") or {}).get("matches") or []]
        return self._ok(messages=messages, count=len(messages))

    def send_message(self, recipient, text, **kwargs):
        client = self._http()
        channel = self._resolve_channel(client, recipient)
        if not channel:
            return self._fail(f"No Slack channel/user found for '{recipient}'.")
        data = client.post("/chat.postMessage",
                           json_body={"channel": channel, "text": text})
        if not data.get("ok"):
            return self._fail(f"Slack send failed: {data.get('error')}")
        return self._ok(id=(data.get("message") or {}).get("ts"), recipient=recipient)

    def reply_to_message(self, message_id, text, channel=None, **kwargs):
        client = self._http()
        if not channel:
            return self._fail("reply_to_message needs a channel.")
        data = client.post("/chat.postMessage",
                           json_body={"channel": channel, "text": text,
                                      "thread_ts": message_id})
        if not data.get("ok"):
            return self._fail(f"Slack reply failed: {data.get('error')}")
        return self._ok(id=(data.get("message") or {}).get("ts"))

    def identify_sender(self, message, **kwargs):
        return self._ok(sender=(message or {}).get("sender"))

    def inspect_attachment(self, message, index=0, **kwargs):
        attachments = (message or {}).get("attachments") or []
        if not attachments or index >= len(attachments):
            return self._fail("No attachment at that index.")
        item = attachments[index]
        return self._ok(filename=item.get("title") or item.get("fallback"),
                        title=item.get("title"), url=item.get("title_link"))

    def download_attachment(self, message, index=0, destination=None, **kwargs):
        info = self.inspect_attachment(message, index=index)
        if not info.get("success"):
            return info
        import urllib.request
        from pathlib import Path

        url = info.get("url")
        if not url:
            return self._fail("Attachment has no downloadable URL.")
        folder = Path(destination) if destination else Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (info.get("filename") or "attachment.bin")
        with urllib.request.urlopen(url, timeout=30) as resp:
            path.write_bytes(resp.read())
        return self._ok(path=str(path), filename=info.get("filename"))

    # ------------------------------------------------------------------ #
    def _first_channel(self, client) -> str | None:
        data = client.get("/conversations.list",
                          params={"types": "public_channel,private_channel", "limit": "5"})
        channels = data.get("channels") or []
        return channels[0]["id"] if channels else None

    def _resolve_channel(self, client, recipient: str) -> str | None:
        data = client.get("/conversations.list",
                          params={"types": "public_channel,private_channel", "limit": "200"})
        for channel in data.get("channels") or []:
            if recipient.lower() in str(channel.get("name", "")).lower():
                return channel.get("id")
        users = client.get("/users.list").get("members") or []
        for user in users:
            name = user.get("name") or user.get("real_name") or ""
            if recipient.lower() == name.lower():
                dm = client.post("/conversations.open",
                                 json_body={"users": user.get("id")})
                return (dm.get("channel") or {}).get("id")
        return None


class _SlackError(Exception):
    pass