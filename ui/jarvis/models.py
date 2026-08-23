"""Pure state models for the JARVIS core and widget workspace."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class JarvisState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    EXECUTING_TOOL = "executing_tool"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    ERROR = "error"

    @classmethod
    def normalize(cls, value: str | "JarvisState") -> "JarvisState":
        if isinstance(value, cls):
            return value
        aliases = {
            "transcribing": cls.THINKING,
            "processing": cls.THINKING,
            "tool": cls.EXECUTING_TOOL,
            "confirming": cls.WAITING_FOR_CONFIRMATION,
            "failed": cls.ERROR,
        }
        key = str(value or "idle").lower()
        if key in aliases:
            return aliases[key]
        return cls(key)


@dataclass(frozen=True)
class WidgetAction:
    id: str
    label: str
    event: str
    style: str = "secondary"
    requires_confirmation: bool = False


@dataclass(frozen=True)
class WidgetSpec:
    widget_type: str
    title: str
    purpose: str
    required_backend: str | None = None
    supported_actions: tuple[str, ...] = ()
    default_size: tuple[int, int] = (360, 280)
    min_size: tuple[int, int] = (260, 170)
    implemented: bool = False
    demo_payload: dict[str, Any] = field(default_factory=dict)
    disabled_message: str = "This backend is not connected yet."
    singleton: bool = True


@dataclass
class WidgetState:
    widget_id: str
    widget_type: str
    title: str
    x: int = 32
    y: int = 32
    width: int = 360
    height: int = 280
    z_index: int = 1
    expanded: bool = False
    collapsed: bool = False
    minimized: bool = False
    pinned: bool = False
    loading: bool = False
    empty: bool = False
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        spec: WidgetSpec,
        *,
        widget_id: str | None = None,
        position: tuple[int, int] | None = None,
        size: tuple[int, int] | None = None,
        data: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> "WidgetState":
        width, height = size or spec.default_size
        x, y = position or (32, 32)
        payload = dict(spec.demo_payload)
        payload.update(data or {})
        return cls(
            widget_id=widget_id or f"{spec.widget_type}-{uuid.uuid4().hex[:8]}",
            widget_type=spec.widget_type,
            title=title or spec.title,
            x=int(x),
            y=int(y),
            width=max(int(width), spec.min_size[0]),
            height=max(int(height), spec.min_size[1]),
            data=payload,
        )

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WidgetState":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in allowed if key in value})
