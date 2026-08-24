"""Official/local messaging connectors for WhatsApp and Discord.

The live paths deliberately use supported credentials: a Discord bot token or
Meta's WhatsApp Business Cloud API. Personal-account tokens are never scraped.
WhatsApp personal chat history can still be read locally after the user exports
the chat, and Cloud API webhook payloads can be persisted for later retrieval.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Callable

from connectors.base import Connector, ConnectorCapability, ConnectorResult, ConnectorStatus


_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_WHATSAPP_STORE = _ROOT / "data" / "jarvis" / "connectors" / "whatsapp_messages.jsonl"


def _default_launcher(query: str) -> bool:
    try:
        from tools.app_launcher import launch_app

        success, _name = launch_app(query)
        return bool(success)
    except Exception:
        return False


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    transport: Callable[..., Any] | None = None,
) -> tuple[int, Any]:
    """Small injectable JSON transport so connector tests never need a network."""
    if transport is not None:
        value = transport(method, url, headers or {}, payload)
        if isinstance(value, tuple) and len(value) == 2:
            return int(value[0]), value[1]
        return 200, value
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return int(response.status), json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            value = {"error": raw or str(exc)}
        return int(exc.code), value


def _api_error(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("error_user_msg") or fallback)
        if error:
            return str(error)
        if value.get("message"):
            return str(value["message"])
    return fallback


class _MessagingConnector(Connector):
    app_query: str = ""
    web_url: str = ""

    def __init__(
        self,
        *,
        launcher: Callable[[str], bool] | None = None,
        url_opener: Callable[[str], object] | None = None,
    ):
        self._launcher = launcher or _default_launcher
        self._url_opener = url_opener or webbrowser.open

    def status(self) -> ConnectorStatus:
        # Opening the official client is a real capability even before API auth;
        # API capabilities advertise their own configuration state.
        return ConnectorStatus.READY if self._url_opener is not None else ConnectorStatus.UNAVAILABLE

    def _open_app(self) -> ConnectorResult:
        launched = False
        try:
            launched = bool(self._launcher(self.app_query))
        except Exception:
            launched = False
        if launched:
            return ConnectorResult(True, {"launch_requested": True, "target": self.app_query}, connector=self.name, capability="open_app")
        opened = bool(self._url_opener(self.web_url))
        if not opened:
            return ConnectorResult.normalize(self.name, "open_app", {"success": False, "error": f"Unable to open {self.display_name} app or web client"})
        return ConnectorResult(True, {"launch_requested": True, "target": self.web_url, "web_fallback": True}, connector=self.name, capability="open_app")


class WhatsAppConnector(_MessagingConnector):
    """WhatsApp Business Cloud API plus local exported-chat intelligence."""

    name = "whatsapp"
    display_name = "WhatsApp"
    app_query = "whatsapp"
    web_url = "https://web.whatsapp.com/"

    def __init__(
        self,
        *,
        access_token: str | None = None,
        phone_number_id: str | None = None,
        api_version: str | None = None,
        webhook_store: Path | str | None = None,
        transport: Callable[..., Any] | None = None,
        launcher: Callable[[str], bool] | None = None,
        url_opener: Callable[[str], object] | None = None,
    ):
        super().__init__(launcher=launcher, url_opener=url_opener)
        self.access_token = str(access_token or os.getenv("JARVIS_WHATSAPP_ACCESS_TOKEN") or "").strip()
        self.phone_number_id = str(phone_number_id or os.getenv("JARVIS_WHATSAPP_PHONE_NUMBER_ID") or "").strip()
        self.api_version = str(api_version or os.getenv("JARVIS_WHATSAPP_API_VERSION") or "v23.0").strip()
        self.webhook_store = Path(webhook_store) if webhook_store else _DEFAULT_WHATSAPP_STORE
        self._transport = transport

    @property
    def api_configured(self) -> bool:
        return bool(self.access_token and self.phone_number_id)

    def status(self) -> ConnectorStatus:
        if self._url_opener is None:
            return ConnectorStatus.UNAVAILABLE
        return ConnectorStatus.READY if self.api_configured else ConnectorStatus.DEGRADED

    def capabilities(self) -> list[ConnectorCapability]:
        reason = "Add a WhatsApp Cloud API access token and phone-number ID in JARVIS Settings → Connectors"
        return [
            ConnectorCapability("open_app", "Open WhatsApp Desktop or WhatsApp Web"),
            ConnectorCapability("open_chat", "Open an official WhatsApp chat deep link", risk_level="medium", input_schema={"required": ["phone"], "properties": {"phone": {"type": "string"}}}),
            ConnectorCapability("prepare_message", "Prefill a message in the official WhatsApp UI without sending", risk_level="medium", input_schema={"required": ["phone", "text"]}),
            ConnectorCapability("connection_info", "Verify the configured WhatsApp Business Cloud API number", requires_auth=True, available=self.api_configured, unavailable_reason="" if self.api_configured else reason),
            ConnectorCapability("read_messages", "Read locally imported chats and received Cloud API webhook messages", input_schema={"properties": {"limit": {"type": "integer"}, "phone": {"type": "string"}}}),
            ConnectorCapability("import_chat", "Import an exported personal WhatsApp chat into JARVIS local storage", mutating=True, idempotent=False, risk_level="medium", input_schema={"required": ["path"]}, explicit_request_authorizes=True),
            ConnectorCapability("ingest_webhook", "Store an official WhatsApp Cloud API webhook payload locally", mutating=True, idempotent=False, input_schema={"required": ["payload"]}, explicit_request_authorizes=True),
            ConnectorCapability(
                "send_message",
                "Send a WhatsApp message through the configured Business Cloud API",
                mutating=True,
                idempotent=False,
                risk_level="high",
                requires_auth=True,
                available=self.api_configured,
                unavailable_reason="" if self.api_configured else reason,
                input_schema={"required": ["phone", "text"]},
                explicit_request_authorizes=True,
            ),
        ]

    @staticmethod
    def _phone(value: str) -> str:
        return re.sub(r"[^0-9]", "", str(value or ""))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _graph(self, suffix: str = "") -> str:
        return f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}{suffix}"

    def _append_messages(self, messages: list[dict[str, Any]]) -> int:
        if not messages:
            return 0
        self.webhook_store.parent.mkdir(parents=True, exist_ok=True)
        with self.webhook_store.open("a", encoding="utf-8") as handle:
            for item in messages:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return len(messages)

    def _stored_messages(self, *, limit: int = 50, phone: str = "") -> list[dict[str, Any]]:
        if not self.webhook_store.is_file():
            return []
        values = []
        for line in self.webhook_store.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not phone or self._phone(item.get("from") or item.get("phone") or "") == self._phone(phone):
                values.append(item)
        return values[-max(1, min(int(limit or 50), 200)):]

    @staticmethod
    def _webhook_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                contacts = {str(item.get("wa_id")): item.get("profile", {}).get("name") for item in value.get("contacts") or []}
                for message in value.get("messages") or []:
                    found.append({
                        "id": message.get("id"),
                        "from": message.get("from"),
                        "sender": contacts.get(str(message.get("from"))) or "",
                        "timestamp": message.get("timestamp"),
                        "type": message.get("type"),
                        "text": (message.get("text") or {}).get("body") or "",
                        "raw": message,
                        "source": "whatsapp_cloud_webhook",
                    })
        return found

    @staticmethod
    def _exported_messages(path: Path) -> list[dict[str, Any]]:
        pattern = re.compile(r"^(?:\[)?(?P<date>\d{1,4}[/-]\d{1,2}[/-]\d{1,4}),?\s+(?P<time>\d{1,2}:\d{2}(?:\s?[AP]M)?)(?:\])?\s*[-–]\s*(?P<sender>[^:]+):\s*(?P<text>.*)$", re.I)
        messages: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            match = pattern.match(line.strip())
            if match:
                messages.append({
                    "timestamp": f"{match.group('date')} {match.group('time')}",
                    "sender": match.group("sender").strip(),
                    "text": match.group("text").strip(),
                    "source": "whatsapp_export",
                    "import_path": str(path.resolve()),
                })
            elif messages and line.strip():
                messages[-1]["text"] = f"{messages[-1]['text']}\n{line.strip()}".strip()
        return messages

    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        if capability == "open_app":
            return self._open_app()
        if capability in {"open_chat", "prepare_message"}:
            phone = self._phone(arguments.get("phone", ""))
            if not phone:
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "An explicit phone number is required"})
            text = str(arguments.get("text") or "")
            url = f"https://wa.me/{phone}"
            if capability == "prepare_message":
                if not text.strip():
                    return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "Message text is required"})
                url += "?text=" + urllib.parse.quote(text)
            if not self._url_opener(url):
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "WhatsApp deep link was not accepted by the system"})
            return ConnectorResult(True, {"launch_requested": True, "url": url, "message_prefilled": capability == "prepare_message", "message_sent": False}, connector=self.name, capability=capability)
        if capability == "read_messages":
            messages = self._stored_messages(limit=arguments.get("limit", 50), phone=str(arguments.get("phone") or ""))
            return ConnectorResult(True, {"messages": messages, "count": len(messages), "live_api": self.api_configured, "source": "local_whatsapp_store"}, connector=self.name, capability=capability)
        if capability == "import_chat":
            path = Path(str(arguments.get("path") or "")).expanduser()
            if not path.is_file():
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "Exported WhatsApp chat file was not found"})
            messages = self._exported_messages(path)
            count = self._append_messages(messages)
            return ConnectorResult(True, {"imported": count, "path": str(path.resolve()), "messages_read": count > 0}, connector=self.name, capability=capability)
        if capability == "ingest_webhook":
            payload = arguments.get("payload")
            if not isinstance(payload, dict):
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "Webhook payload must be an object"})
            messages = self._webhook_messages(payload)
            return ConnectorResult(True, {"ingested": self._append_messages(messages), "messages": messages}, connector=self.name, capability=capability)
        if capability == "connection_info":
            status, value = _http_json("GET", self._graph("?fields=display_phone_number,verified_name"), headers=self._headers(), transport=self._transport)
            if status >= 400:
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": _api_error(value, f"WhatsApp API returned HTTP {status}")})
            return ConnectorResult(True, {"connected": True, "account": value}, connector=self.name, capability=capability)
        if capability == "send_message":
            phone = self._phone(arguments.get("phone", ""))
            text = str(arguments.get("text") or "").strip()
            status, value = _http_json(
                "POST",
                self._graph("/messages"),
                headers=self._headers(),
                payload={"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone, "type": "text", "text": {"preview_url": False, "body": text}},
                transport=self._transport,
            )
            if status >= 400 or not isinstance(value, dict) or not value.get("messages"):
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": _api_error(value, f"WhatsApp API returned HTTP {status}")})
            message_id = (value.get("messages") or [{}])[0].get("id")
            return ConnectorResult(True, {"message_sent": True, "message_id": message_id, "to": phone, "provider_response": value}, connector=self.name, capability=capability)
        return ConnectorResult.normalize(self.name, capability, {"success": False, "error": f"Capability '{capability}' is unavailable"})


class DiscordConnector(_MessagingConnector):
    """Official Discord bot API adapter; never automates a normal user token."""

    name = "discord"
    display_name = "Discord"
    app_query = "discord"
    web_url = "https://discord.com/app"

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        default_channel: str | None = None,
        transport: Callable[..., Any] | None = None,
        launcher: Callable[[str], bool] | None = None,
        url_opener: Callable[[str], object] | None = None,
    ):
        super().__init__(launcher=launcher, url_opener=url_opener)
        self.bot_token = str(bot_token or os.getenv("JARVIS_DISCORD_BOT_TOKEN") or "").strip()
        self.default_channel = str(default_channel or os.getenv("JARVIS_DISCORD_DEFAULT_CHANNEL") or "").strip()
        self._transport = transport

    @property
    def api_configured(self) -> bool:
        return bool(self.bot_token)

    def status(self) -> ConnectorStatus:
        if self._url_opener is None:
            return ConnectorStatus.UNAVAILABLE
        return ConnectorStatus.READY if self.api_configured else ConnectorStatus.DEGRADED

    def capabilities(self) -> list[ConnectorCapability]:
        reason = "Add a Discord bot token in JARVIS Settings → Connectors; normal user tokens are not supported"
        channel_required = [] if self.default_channel else ["channel"]
        return [
            ConnectorCapability("open_app", "Open Discord Desktop or the official web app"),
            ConnectorCapability("open_channel", "Open a known official Discord channel URL", risk_level="medium", input_schema={"required": ["url"]}),
            ConnectorCapability("connection_info", "Verify the configured Discord bot", requires_auth=True, available=self.api_configured, unavailable_reason="" if self.api_configured else reason),
            ConnectorCapability("list_guild_channels", "List server channels visible to the configured bot", requires_auth=True, available=self.api_configured, unavailable_reason="" if self.api_configured else reason, input_schema={"required": ["guild_id"]}),
            ConnectorCapability("read_messages", "Read messages in a server/DM channel visible to the configured bot", requires_auth=True, available=self.api_configured, unavailable_reason="" if self.api_configured else reason, input_schema={"required": channel_required}),
            ConnectorCapability(
                "send_message",
                "Send a message as the configured Discord bot",
                mutating=True,
                idempotent=False,
                risk_level="high",
                requires_auth=True,
                available=self.api_configured,
                unavailable_reason="" if self.api_configured else reason,
                input_schema={"required": [*channel_required, "text"]},
                explicit_request_authorizes=True,
            ),
        ]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.bot_token}", "User-Agent": "JARVIS-Local-Assistant/1.0"}

    def _request(self, method: str, suffix: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        return _http_json(method, f"https://discord.com/api/v10{suffix}", headers=self._headers(), payload=payload, transport=self._transport)

    @staticmethod
    def _channel(value: str) -> str:
        raw = str(value or "").strip()
        if raw.startswith("https://discord.com/channels/"):
            raw = raw.rstrip("/").split("/")[-1]
        return raw if re.fullmatch(r"\d{16,22}", raw) else ""

    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        if capability == "open_app":
            return self._open_app()
        if capability == "open_channel":
            url = str(arguments.get("url") or "").strip()
            if not (url.startswith("https://discord.com/channels/") or url.startswith("discord://")):
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "Only explicit official Discord channel/deep-link URLs are allowed"})
            if not self._url_opener(url):
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "Discord channel link was not accepted by the system"})
            return ConnectorResult(True, {"launch_requested": True, "url": url}, connector=self.name, capability=capability)
        if capability == "connection_info":
            status, value = self._request("GET", "/users/@me")
            if status >= 400:
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": _api_error(value, f"Discord API returned HTTP {status}")})
            return ConnectorResult(True, {"connected": True, "bot": value}, connector=self.name, capability=capability)
        if capability == "list_guild_channels":
            guild_id = str(arguments.get("guild_id") or "").strip()
            status, value = self._request("GET", f"/guilds/{urllib.parse.quote(guild_id)}/channels")
            if status >= 400:
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": _api_error(value, f"Discord API returned HTTP {status}")})
            return ConnectorResult(True, {"channels": value if isinstance(value, list) else [], "guild_id": guild_id}, connector=self.name, capability=capability)
        if capability in {"read_messages", "send_message"}:
            channel = self._channel(arguments.get("channel") or self.default_channel)
            if not channel:
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "A Discord channel ID or official channel URL is required"})
            if capability == "read_messages":
                limit = max(1, min(int(arguments.get("limit") or 50), 100))
                status, value = self._request("GET", f"/channels/{channel}/messages?limit={limit}")
                if status >= 400:
                    return ConnectorResult.normalize(self.name, capability, {"success": False, "error": _api_error(value, f"Discord API returned HTTP {status}")})
                messages = value if isinstance(value, list) else []
                return ConnectorResult(True, {"messages": messages, "count": len(messages), "channel": channel}, connector=self.name, capability=capability)
            text = str(arguments.get("text") or "").strip()
            status, value = self._request("POST", f"/channels/{channel}/messages", {"content": text})
            if status >= 400 or not isinstance(value, dict) or not value.get("id"):
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": _api_error(value, f"Discord API returned HTTP {status}")})
            return ConnectorResult(True, {"message_sent": True, "message_id": value.get("id"), "channel": channel, "message": value}, connector=self.name, capability=capability)
        return ConnectorResult.normalize(self.name, capability, {"success": False, "error": f"Capability '{capability}' is unavailable"})
