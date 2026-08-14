"""Events package -- the event-driven backbone of Jarvis.

AppEvent is a lightweight, structured abstraction over anything that
happens in the user's digital environment:

    source        which integration/app produced it (discord, gmail, fs)
    type          NEW_MESSAGE, NEW_EMAIL, NEW_FILE, NEW_NOTIFICATION, ...
    timestamp     when it happened (epoch seconds)
    sender        who sent it
    content       the message/body text
    attachment    attachment metadata/path
    metadata      any extra structured context

Adapters and watchers emit AppEvents; workflows (Phase 5) and the agent
loop subscribe to them via the EventDispatcher, so Jarvis reacts to the
world instead of constantly screenshotting/polling everything.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Canonical event types.
NEW_MESSAGE = "NEW_MESSAGE"
NEW_EMAIL = "NEW_EMAIL"
NEW_FILE = "NEW_FILE"
NEW_NOTIFICATION = "NEW_NOTIFICATION"
CALENDAR_EVENT = "CALENDAR_EVENT"
APP_OPENED = "APP_OPENED"
APP_CLOSED = "APP_CLOSED"
DOWNLOAD_COMPLETED = "DOWNLOAD_COMPLETED"
SYSTEM = "SYSTEM"

EVENT_TYPES = (
    NEW_MESSAGE, NEW_EMAIL, NEW_FILE, NEW_NOTIFICATION, CALENDAR_EVENT,
    APP_OPENED, APP_CLOSED, DOWNLOAD_COMPLETED, SYSTEM,
)


@dataclass
class AppEvent:
    """Structured event emitted by a source and routed to handlers."""

    source: str
    type: str
    timestamp: float = field(default_factory=time.time)
    sender: str | None = None
    content: str | None = None
    attachment: Any = None
    metadata: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "type": self.type,
            "timestamp": self.timestamp,
            "sender": self.sender,
            "content": self.content,
            "attachment": self.attachment,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppEvent":
        return cls(
            source=data.get("source", ""),
            type=data.get("type", SYSTEM),
            timestamp=float(data.get("timestamp", time.time())),
            sender=data.get("sender"),
            content=data.get("content"),
            attachment=data.get("attachment"),
            metadata=data.get("metadata") or {},
            event_id=data.get("event_id"),
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"<AppEvent {self.type} from={self.source} "
                f"by={self.sender or '?'}>")


def make_event(
    source: str,
    event_type: str,
    *,
    sender: str | None = None,
    content: str | None = None,
    attachment: Any = None,
    metadata: dict | None = None,
    timestamp: float | None = None,
) -> AppEvent:
    return AppEvent(
        source=source,
        type=event_type,
        timestamp=timestamp if timestamp is not None else time.time(),
        sender=sender,
        content=content,
        attachment=attachment,
        metadata=metadata or {},
    )