"""Consistent connector interface and result normalization."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ConnectorStatus(str, Enum):
    READY = "ready"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ConnectorCapability:
    name: str
    description: str = ""
    mutating: bool = False
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ConnectorResult:
    success: bool
    data: Any = None
    error: str | None = None
    retryable: bool = False
    connector: str = ""
    capability: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def normalize(cls, connector: str, capability: str, value: Any) -> "ConnectorResult":
        if isinstance(value, ConnectorResult):
            return value
        if isinstance(value, dict) and value.get("success") is False:
            return cls(False, value, str(value.get("error") or value.get("message") or "Connector failed"), bool(value.get("retryable")), connector, capability)
        return cls(True, value, connector=connector, capability=capability)


class Connector(ABC):
    name: str

    @abstractmethod
    def status(self) -> ConnectorStatus:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> list[ConnectorCapability]:
        raise NotImplementedError

    @abstractmethod
    def execute(self, capability: str, arguments: dict) -> ConnectorResult:
        raise NotImplementedError

