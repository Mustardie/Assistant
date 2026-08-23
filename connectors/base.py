"""Consistent connector interface and result normalization."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any


class ConnectorStatus(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ConnectorCapability:
    name: str
    description: str = ""
    mutating: bool = False
    requires_confirmation: bool = False
    idempotent: bool = True


@dataclass(frozen=True)
class ConnectorRequest:
    connector: str
    capability: str
    arguments: dict[str, Any] = field(default_factory=dict)
    confirmed: bool = False
    request_id: str = ""


@dataclass(frozen=True)
class ConnectorError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorActionPlan:
    request: ConnectorRequest
    status: ConnectorStatus
    supported: bool
    requires_confirmation: bool = False
    may_retry: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ConnectorResult:
    success: bool
    data: Any = None
    error: str | None = None
    retryable: bool = False
    connector: str = ""
    capability: str = ""
    metadata: dict = field(default_factory=dict)
    error_detail: ConnectorError | None = None
    partial: bool = False
    file_profiles: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def normalize(cls, connector: str, capability: str, value: Any) -> "ConnectorResult":
        if isinstance(value, ConnectorResult):
            updates = {}
            if not value.connector:
                updates["connector"] = connector
            if not value.capability:
                updates["capability"] = capability
            return replace(value, **updates) if updates else value
        if value is None:
            error = ConnectorError("empty_result", "Connector returned no result", False)
            return cls(False, error=error.message, connector=connector, capability=capability, error_detail=error)
        if isinstance(value, dict):
            status = str(value.get("status") or "").lower()
            message = value.get("error") or value.get("error_message")
            explicit_failure = value.get("success") is False or status in {"error", "failed", "failure"}
            contradictory_error = bool(message) and value.get("success") is not False
            partial = bool(value.get("partial") or status == "partial")
            if explicit_failure or contradictory_error or partial:
                text = str(message or value.get("message") or ("Connector returned partial data" if partial else "Connector failed"))
                retryable = bool(value.get("retryable"))
                detail = ConnectorError(
                    str(value.get("error_code") or ("partial_failure" if partial else "connector_error")),
                    text,
                    retryable,
                    {key: item for key, item in value.items() if key not in {"data", "items"}},
                )
                return cls(
                    False,
                    value.get("data", value),
                    text,
                    retryable,
                    connector,
                    capability,
                    error_detail=detail,
                    partial=partial,
                )
            if value.get("success") is True:
                data = value.get("data", value.get("result", value))
                metadata = {key: item for key, item in value.items() if key not in {"success", "data", "result"}}
                return cls(True, data, connector=connector, capability=capability, metadata=metadata)
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
    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        raise NotImplementedError
