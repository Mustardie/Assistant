"""Deterministic connector selection and preflight planning."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from connectors.base import ConnectorRequest
from connectors.defaults import default_registry


@dataclass(frozen=True)
class ConnectorIntentPlan:
    request_text: str
    connector: str
    capability: str
    arguments: dict[str, Any] = field(default_factory=dict)
    required_inputs: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    confirmation_required: bool = False
    risk_level: str = "low"
    supported: bool = False
    connector_status: str = "unavailable"
    expected_result: str = ""
    fallback: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _phone(text: str) -> str:
    match = re.search(r"\+?\d[\d ()-]{7,}\d", text)
    return re.sub(r"\D", "", match.group(0)) if match else ""


def _after(text: str, marker: str) -> str:
    lowered = text.lower()
    index = lowered.find(marker)
    return text[index + len(marker):].strip(" .,:;!?\"") if index >= 0 else ""


def _message_text(text: str) -> str:
    quoted = re.search(r"[\"“](.+?)[\"”]", text)
    if quoted:
        return quoted.group(1).strip()
    for marker in ("saying", "with the message", "message:", "text:"):
        value = _after(text, marker)
        if value:
            return value
    return ""


class ConnectorActionPlanner:
    def __init__(self, registry=None):
        self.registry = registry or default_registry()

    def choose(self, request_text: str) -> ConnectorIntentPlan:
        text = str(request_text or "").strip()
        lower = text.lower()
        connector = ""
        capability = ""
        arguments: dict[str, Any] = {}
        fallback = ""

        if "whatsapp" in lower:
            connector = "whatsapp"
            if re.search(r"\b(open|launch)\b", lower) and not re.search(r"\b(chat|message|send)\b", lower):
                capability = "open_app"
            elif re.search(r"\bsend\b", lower):
                capability = "send_message"
            else:
                capability = "prepare_message"
            arguments = {"phone": _phone(text), "text": _message_text(text)}
            arguments = {key: value for key, value in arguments.items() if value}
            fallback = "Configure WhatsApp Cloud API in JARVIS Settings, or import an exported personal chat/file locally."
        elif "discord" in lower:
            connector = "discord"
            if re.search(r"\b(read|check|find.*message)\b", lower):
                capability = "read_messages"
            elif re.search(r"\b(send|message)\b", lower):
                capability = "send_message"
            elif "channel" in lower:
                capability = "open_channel"
                url = re.search(r"(?:https://discord\.com/channels/\S+|discord://\S+)", text)
                if url:
                    arguments["url"] = url.group(0)
            else:
                capability = "open_app"
            channel_url = re.search(r"https://discord\.com/channels/\d+/\d+", text)
            channel_id = re.search(r"(?<!\d)\d{16,22}(?!\d)", text)
            if capability in {"read_messages", "send_message"}:
                if channel_url:
                    arguments["channel"] = channel_url.group(0)
                elif channel_id:
                    arguments["channel"] = channel_id.group(0)
            if capability == "send_message":
                message = _message_text(text)
                if message:
                    arguments["text"] = message
            fallback = "Configure a Discord bot and channel ID, or download/export the attachment for local inbox ingestion."
        elif re.search(r"\b(calendar|event|schedule|appointment|meeting|exam)\b", lower):
            connector = "google_calendar"
            if re.search(r"\b(create|make|add|schedule)\b", lower):
                capability = "create_event"
                title = re.sub(r"\b(create|make|add|schedule|a|an|calendar|event|for|my)\b", " ", text, flags=re.I)
                arguments["title"] = re.sub(r"\s+", " ", title).strip(" .") or "New event"
                temporal = re.search(r"\b(today|tomorrow|next \w+|\d{4}-\d{2}-\d{2})(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?", lower)
                if temporal:
                    arguments["start"] = temporal.group(0)
            elif "search" in lower or "find" in lower:
                capability = "search_events"
                arguments["query"] = text
            else:
                capability = "list_events"
                arguments["start"] = "tomorrow" if "tomorrow" in lower else "now"
            fallback = "Use the local schedule view or configure official Google Calendar OAuth scopes."
        elif re.search(r"\b(google drive|drive file|my drive)\b", lower):
            connector = "google_drive"
            capability = "search_files"
            arguments["query"] = text
            fallback = "Search a local synced Google Drive folder or manually import the file."
        elif re.search(r"\b(downloaded|downloads?|download folder)\b", lower):
            connector = "browser_downloads"
            capability = "search_intent" if re.search(r"\b(find|pdf|worksheet|assignment|today|yesterday|recent)\b", lower) else "list_recent"
            if capability == "search_intent":
                arguments["query"] = text
            arguments["days"] = 1 if "today" in lower else 2 if "yesterday" in lower else 7
            fallback = "Provide the local file path or choose a configured inbox folder."
        elif re.search(r"\b(email|gmail|mail|inbox)\b", lower):
            connector = "gmail"
            if re.search(r"\b(send|reply)\b", lower):
                capability = "send_reply" if "reply" in lower else "send"
                if capability == "send_reply":
                    arguments["command"] = text
            elif re.search(r"\b(find|search|assignment|attachment)\b", lower):
                capability = "search"
                arguments["query"] = text
            else:
                capability = "read"
                arguments["limit"] = 15
            fallback = "Authenticate Gmail or manually import the downloaded attachment and its message context."
        elif re.search(r"\b(open|launch|start)\b", lower):
            connector = "app_launcher"
            capability = "open_app"
            query = re.sub(r"\b(open|launch|start|the|app|application)\b", " ", text, flags=re.I)
            arguments["query"] = re.sub(r"\s+", " ", query).strip()
            fallback = "Open the application manually or refresh the local app index."
        else:
            return ConnectorIntentPlan(text, "", "", reason="No connector-specific intent was detected", fallback="Handle as normal conversation or a local file task.")

        registry_plan = self.registry.plan(ConnectorRequest(connector, capability, arguments))
        required = ()
        capabilities = self.registry.capabilities(connector)
        descriptor = next((item for item in capabilities if item.get("name") == capability), None)
        if descriptor:
            required = tuple(descriptor.get("input_schema", {}).get("required", ()))
        missing = tuple(dict.fromkeys(tuple(registry_plan.missing_inputs) + tuple(item for item in required if arguments.get(item) in (None, "", []))))
        return ConnectorIntentPlan(
            text,
            connector,
            capability,
            arguments,
            required_inputs=required,
            missing_inputs=missing,
            confirmation_required=registry_plan.requires_confirmation,
            risk_level=registry_plan.risk_level,
            supported=registry_plan.supported and not missing,
            connector_status=registry_plan.status.value,
            expected_result=registry_plan.expected_result,
            fallback=fallback or registry_plan.fallback,
            reason=registry_plan.reason,
        )
