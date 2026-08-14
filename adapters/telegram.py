"""Telegram adapter -- messaging via the official Telegram Bot API."""

from __future__ import annotations

import logging

from adapters.api import RESTAdapter

logger = logging.getLogger(__name__)


class TelegramAdapter(RESTAdapter):
    name = "telegram"
    display_name = "Telegram"
    description = ("Messaging through your Telegram bot. Create a bot with "
                   "@BotFather, paste the token, and Nova can read messages, "
                   "reply and send from it.")
    authentication = "api_key"
    config_key = "telegram_bot_token"
    api_base_url = "https://api.telegram.org"
    token_header = "Authorization"  # Telegram embeds the token in the URL.
    capabilities = [
        "read_messages", "search_messages", "send_message", "reply_to_message",
        "identify_sender", "inspect_attachment", "download_attachment",
    ]

    # ------------------------------------------------------------------ #
    def _bot_token(self) -> str:
        return self._api_key()

    def _http(self):
        if self._client is None:
            from adapters.api import ApiClient
            token = self._bot_token()
            self._client = ApiClient(
                base_url=f"https://api.telegram.org/bot{token}" if token else "",
                token="",  # token is part of the URL
            )
        return self._client

    def _verify_connection(self):
        data = self._http().get("/getMe")
        if not isinstance(data, dict) or not data.get("ok"):
            raise _TelegramError()

    def status(self) -> dict:
        if not self.is_configured():
            return {"status": "not_configured",
                    "message": "Telegram needs a bot token to connect."}
        return {"status": "requires_auth",
                "message": "Telegram bot token configured. Click Connect to verify."}

    # ------------------------------------------------------------------ #
    def read_messages(self, limit=20, **kwargs):
        client = self._http()
        offset = kwargs.get("offset") or -int(limit)
        data = client.get("/getUpdates",
                          params={"limit": min(int(limit), 100), "offset": offset})
        if not data.get("ok"):
            return self._fail("Telegram getUpdates failed.")
        messages = []
        for update in data.get("result") or []:
            msg = update.get("message") or update.get("channel_post")
            if not msg:
                continue
            messages.append({"id": msg.get("message_id"),
                             "chat_id": (msg.get("chat") or {}).get("id"),
                             "sender": _sender(msg),
                             "content": msg.get("text", ""),
                             "timestamp": msg.get("date")})
        return self._ok(messages=messages, count=len(messages))

    def search_messages(self, query, limit=20, **kwargs):
        results = self.read_messages(limit=max(limit, 50))
        matches = [m for m in results.get("messages", [])
                   if query.lower() in (m.get("content") or "").lower()]
        return self._ok(messages=matches[:limit], count=len(matches[:limit]))

    def send_message(self, recipient, text, **kwargs):
        client = self._http()
        chat_id = self._resolve_chat(client, recipient)
        if not chat_id:
            return self._fail(f"No Telegram chat found for '{recipient}'.")
        data = client.post("/sendMessage", json_body={"chat_id": chat_id, "text": text})
        if not data.get("ok"):
            return self._fail(f"Telegram send failed: {data.get('description')}")
        return self._ok(id=(data.get("result") or {}).get("message_id"), recipient=recipient)

    def reply_to_message(self, message_id, text, chat_id=None, **kwargs):
        client = self._http()
        if not chat_id:
            return self._fail("reply_to_message needs a chat_id.")
        data = client.post("/sendMessage",
                           json_body={"chat_id": chat_id, "text": text,
                                      "reply_to_message_id": message_id})
        if not data.get("ok"):
            return self._fail(f"Telegram reply failed: {data.get('description')}")
        return self._ok(id=(data.get("result") or {}).get("message_id"))

    def identify_sender(self, message, **kwargs):
        return self._ok(sender=(message or {}).get("sender"))

    def inspect_attachment(self, message, index=0, **kwargs):
        doc = (message or {}).get("document")
        if doc:
            return self._ok(filename=doc.get("file_name"),
                            mime_type=doc.get("mime_type"), size=doc.get("file_size"),
                            file_id=doc.get("file_id"))
        return self._fail("No document attachment on that message.")

    def download_attachment(self, message, index=0, destination=None, **kwargs):
        client = self._http()
        doc = (message or {}).get("document")
        if not doc:
            return self._fail("No document attachment on that message.")
        info = client.get("/getFile", params={"file_id": doc["file_id"]})
        if not info.get("ok"):
            return self._fail("Telegram getFile failed.")
        from pathlib import Path
        import urllib.request

        file_path = (info.get("result") or {}).get("file_path")
        url = f"https://api.telegram.org/file/bot{self._bot_token()}/{file_path}"
        folder = Path(destination) if destination else Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        name = doc.get("file_name") or Path(file_path).name or "file.bin"
        path = folder / name
        with urllib.request.urlopen(url, timeout=30) as resp:
            path.write_bytes(resp.read())
        return self._ok(path=str(path), filename=name)

    # ------------------------------------------------------------------ #
    def _resolve_chat(self, client, recipient: str) -> str | None:
        data = client.get("/getUpdates", params={"limit": 100})
        if data.get("ok"):
            seen = {}
            for update in data.get("result") or []:
                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue
                chat = msg.get("chat") or {}
                title = chat.get("title") or chat.get("username") or _sender(msg) or ""
                seen.setdefault(str(chat.get("id")), title)
            for chat_id, title in seen.items():
                if recipient.lower() in str(title).lower():
                    return chat_id
        return None


def _sender(msg: dict) -> str:
    user = msg.get("from") or {}
    return f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or \
        user.get("username", "")


class _TelegramError(Exception):
    pass