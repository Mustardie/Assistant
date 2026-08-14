"""Discord adapter -- messaging via the Discord REST API.

Two auth modes, picked automatically:

    * bot token  (config key `discord_bot_token`)  -- "Bot " prefix.
      The bot must be added to a server (it sees DMs with itself).

    * account/user token (config key `discord_user_token`) -- no prefix.
      "Self-bot" style: uses YOUR account, so it can read private DMs
      with anyone and post in servers you're in. Set it via the
      Connections page or the DISCORD_USER_TOKEN env var.

Implements the messaging capabilities the universal tools dispatch to.
"""

from __future__ import annotations

import logging

from adapters.api import ApiClient, ApiError, RESTAdapter

logger = logging.getLogger(__name__)


class DiscordAdapter(RESTAdapter):
    name = "discord"
    display_name = "Discord"
    description = ("Messaging through the Discord API. Use a bot token (must "
                   "be added to a server) or your own account token to read "
                   "private DMs and reply from your identity.")
    authentication = "api_key"
    config_key = "discord_bot_token"
    user_config_key = "discord_user_token"
    api_base_url = "https://discord.com/api/v10"
    token_header = "Authorization"
    token_prefix = "Bot "
    capabilities = [
        "read_messages", "search_messages", "send_message", "reply_to_message",
        "identify_sender", "inspect_attachment", "download_attachment",
    ]

    # ------------------------------------------------------------------ #
    def _user_token(self) -> str:
        from connections.secrets import get_token
        return get_token(self.user_config_key)

    def _token_kind(self) -> str:
        return "user" if self._user_token() else "bot"

    def _http(self):
        if self._client is None:
            self._client = ApiClient(
                self.api_base_url,
                token=self._user_token() or self._api_key(),
                token_header=self.token_header,
                token_prefix="" if self._token_kind() == "user" else self.token_prefix,
            )
        return self._client

    def _verify_connection(self):
        self._http().get("/users/@me")

    def status(self) -> dict:
        if not (self.is_configured() or self._user_token()):
            return {"status": "not_configured",
                    "message": "Discord needs a bot token (or your account "
                               "token) to connect."}
        kind = self._token_kind()
        label = "account token" if kind == "user" else "bot token"
        return {"status": "requires_auth",
                "message": f"Discord {label} configured. Click Connect to verify."}

    # ------------------------------------------------------------------ #
    # Messaging
    # ------------------------------------------------------------------ #
    def read_messages(self, limit=20, **kwargs):
        client = self._http()
        channel = kwargs.get("channel_id") or self._last_dm_channel(client)
        if not channel:
            return self._fail("No Discord channel available to read.")
        data = client.get(f"/channels/{channel}/messages",
                          params={"limit": min(int(limit), 100)})
        messages = [{"id": m.get("id"), "sender": (m.get("author") or {}).get("username"),
                     "content": m.get("content", ""), "timestamp": m.get("timestamp")}
                    for m in data if isinstance(m, dict)]
        return self._ok(messages=messages, count=len(messages))

    def search_messages(self, query, limit=20, **kwargs):
        return self.read_messages(limit=limit, query=query)

    def send_message(self, recipient, text, **kwargs):
        client = self._http()
        channel = self._resolve_recipient_channel(client, recipient)
        if not channel:
            return self._fail(f"No Discord channel found for '{recipient}'.")
        data = client.post(f"/channels/{channel}/messages",
                           json_body={"content": text})
        return self._ok(id=data.get("id"), recipient=recipient)

    def reply_to_message(self, message_id, text, **kwargs):
        client = self._http()
        msg = client.get(f"/channels/{kwargs.get('channel_id', '@me')}/messages/{message_id}")
        channel = kwargs.get("channel_id")
        if not channel:
            channel = (msg or {}).get("channel_id") or self._last_dm_channel(client)
        if not channel:
            return self._fail("Cannot resolve the channel for this reply.")
        data = client.post(f"/channels/{channel}/messages",
                           json_body={"content": text,
                                      "message_reference": {"message_id": message_id}})
        return self._ok(id=data.get("id"))

    def identify_sender(self, message, **kwargs):
        return self._ok(sender=(message or {}).get("sender"))

    def inspect_attachment(self, message, index=0, **kwargs):
        attachments = (message or {}).get("attachments") or []
        if not attachments or index >= len(attachments):
            return self._fail("No attachment at that index.")
        item = attachments[index]
        return self._ok(filename=item.get("filename"),
                        mime_type=item.get("content_type"),
                        size=item.get("size"),
                        url=item.get("url"))

    def download_attachment(self, message, index=0, destination=None, **kwargs):
        info = self.inspect_attachment(message, index=index)
        if not info.get("success"):
            return info
        import urllib.request
        from pathlib import Path

        url = info.get("url")
        folder = Path(destination) if destination else Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (info.get("filename") or "attachment.bin")
        with urllib.request.urlopen(url, timeout=30) as resp:
            path.write_bytes(resp.read())
        return self._ok(path=str(path), filename=info.get("filename"))

    # ------------------------------------------------------------------ #
    def _last_dm_channel(self, client) -> str | None:
        data = client.get("/users/@me/channels", params={"limit": 1})
        return data[0]["id"] if isinstance(data, list) and data else None

    def _resolve_recipient_channel(self, client, recipient: str) -> str | None:
        data = client.get("/users/@me/channels")
        if isinstance(data, list):
            for channel in data:
                recips = channel.get("recipients") or []
                if any(recipient.lower() in (r.get("username", "").lower()
                                             for r in recips)):
                    return channel.get("id")
        # Fallback: create/open a DM with the user by name lookup.
        user = self._find_user(client, recipient)
        if user:
            dm = client.post("/users/@me/channels",
                             json_body={"recipient_id": user.get("id")})
            return dm.get("id")
        return None

    def _find_user(self, client, name: str) -> dict | None:
        if not name:
            return None
        try:
            data = client.get("/users/@me/channels")
            if isinstance(data, list):
                for channel in data:
                    for r in channel.get("recipients") or []:
                        if r.get("username", "").lower() == name.lower():
                            return r
        except ApiError:
            pass
        return None