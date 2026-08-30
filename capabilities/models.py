from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Permission(str, Enum):
    READ_LOCAL = "read_local"
    MODIFY_LOCAL_FILES = "modify_local_files"
    PROCESS_EXECUTION = "process_execution"
    EXTERNAL_NETWORK = "external_network"
    ACCOUNT_DATA = "account_data"
    SEND_MESSAGES = "send_messages"
    MODIFY_REMOTE_DATA = "modify_remote_data"
    DELETE = "delete"
    FINANCIAL = "financial"
    CREDENTIAL_ACCESS = "credential_access"
    SYSTEM_CHANGE = "system_change"


class CapabilityState(str, Enum):
    TEMPORARY = "temporary"
    VALIDATED = "validated"
    ACTIVE = "active"
    STALE = "stale"
    DISABLED = "disabled"


@dataclass
class Reliability:
    successful_runs: int = 0
    failed_runs: int = 0
    validation_successes: int = 0
    validation_failures: int = 0
    consecutive_failures: int = 0
    last_success: str | None = None
    last_failure: str | None = None

    @property
    def score(self) -> float:
        # Transparent beta prior: a new capability starts at 0.5 and must
        # earn trust without ever becoming mathematically infallible.
        return round((self.successful_runs + 1) / (self.successful_runs + self.failed_runs + 2), 3)

    def record(self, success: bool) -> None:
        if success:
            self.successful_runs += 1
            self.consecutive_failures = 0
            self.last_success = utc_now()
        else:
            self.failed_runs += 1
            self.consecutive_failures += 1
            self.last_failure = utc_now()


@dataclass
class Capability:
    id: str
    name: str
    description: str
    semantic_text: str
    inputs: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    strategy: dict[str, Any] = field(default_factory=dict)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    auth: dict[str, Any] = field(default_factory=dict)
    discovery_source: dict[str, Any] = field(default_factory=dict)
    state: str = CapabilityState.TEMPORARY.value
    risk_level: str = "low"
    auto_execute: bool = False
    reliability: Reliability = field(default_factory=Reliability)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_validated_at: str | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    version: int = 1

    @classmethod
    def temporary(cls, name: str, description: str, *, strategy: dict, **kwargs) -> "Capability":
        return cls(
            id="cap_" + uuid.uuid4().hex[:16],
            name=name,
            description=description,
            semantic_text=kwargs.pop("semantic_text", f"{name} {description}"),
            strategy=strategy,
            **kwargs,
        )

    @classmethod
    def from_dict(cls, value: dict) -> "Capability":
        data = dict(value)
        reliability = dict(data.get("reliability") or {})
        reliability.pop("score", None)
        data["reliability"] = Reliability(**reliability)
        return cls(**data)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["reliability"]["score"] = self.reliability.score
        return data

    def touch(self) -> None:
        self.updated_at = utc_now()


@dataclass
class LearnedSkill:
    id: str
    name: str
    description: str
    semantic_text: str
    inputs: dict[str, Any]
    steps: list[dict[str, Any]]
    verification_steps: list[dict[str, Any]] = field(default_factory=list)
    failure_handling: dict[str, Any] = field(default_factory=lambda: {"on_failure": "stop"})
    permissions: list[str] = field(default_factory=list)
    state: str = CapabilityState.ACTIVE.value
    reliability: Reliability = field(default_factory=Reliability)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(cls, name: str, description: str, inputs: dict, steps: list[dict], **kwargs) -> "LearnedSkill":
        return cls(
            id="skill_" + uuid.uuid4().hex[:16],
            name=name,
            description=description,
            semantic_text=kwargs.pop("semantic_text", f"{name} {description}"),
            inputs=inputs,
            steps=steps,
            **kwargs,
        )

    @classmethod
    def from_dict(cls, value: dict) -> "LearnedSkill":
        data = dict(value)
        reliability = dict(data.get("reliability") or {})
        reliability.pop("score", None)
        data["reliability"] = Reliability(**reliability)
        return cls(**data)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["reliability"]["score"] = self.reliability.score
        return data
