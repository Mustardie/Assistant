"""Consistent connector interface and result normalization."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
import inspect
from typing import Any


class ConnectorStatus(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"


class ConnectorRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ConnectorCapability:
    name: str
    description: str = ""
    mutating: bool = False
    requires_confirmation: bool = False
    idempotent: bool = True
    risk_level: str = ConnectorRisk.LOW.value
    requires_auth: bool = False
    available: bool = True
    unavailable_reason: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    # For actions such as a message-send button or an explicit "send this"
    # request, the user's initiating action is already the authorization.  The
    # connector still treats the call as mutating/non-idempotent, but does not
    # add a redundant second confirmation gate.
    explicit_request_authorizes: bool = False

    @property
    def id(self) -> str:
        return self.name

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = self.name
        return value


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
    risk_level: str = ConnectorRisk.LOW.value
    expected_result: str = ""
    missing_inputs: tuple[str, ...] = ()
    fallback: str = ""


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
    display_name: str = ""

    @abstractmethod
    def status(self) -> ConnectorStatus:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> list[ConnectorCapability]:
        raise NotImplementedError

    @abstractmethod
    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        raise NotImplementedError

    def plan_action(self, request: ConnectorRequest) -> ConnectorActionPlan:
        try:
            status = self.status()
        except Exception as exc:
            return ConnectorActionPlan(
                request,
                ConnectorStatus.UNAVAILABLE,
                False,
                reason=f"Status check failed: {exc}",
                fallback="Check connector configuration and try again.",
            )
        descriptor = next((item for item in self.capabilities() if item.name == request.capability), None)
        if descriptor is None:
            return ConnectorActionPlan(
                request,
                status,
                False,
                reason=f"Capability '{request.capability}' is unsupported",
                fallback="Choose one of the connector's advertised capabilities.",
            )
        required = tuple(str(item) for item in descriptor.input_schema.get("required", ()))
        missing = tuple(item for item in required if request.arguments.get(item) in (None, "", []))
        operational = status in {ConnectorStatus.READY, ConnectorStatus.DEGRADED}
        supported = operational and descriptor.available and not missing
        confirmation = bool(
            descriptor.requires_confirmation
            or (descriptor.mutating and not descriptor.explicit_request_authorizes)
        )
        if not descriptor.available:
            reason = descriptor.unavailable_reason or "Capability is unavailable"
        elif not operational:
            reason = f"Connector status is {status.value}"
        elif missing:
            reason = f"Missing required input: {', '.join(missing)}"
        elif confirmation and not request.confirmed:
            reason = "User confirmation is required"
        else:
            reason = "Ready with limited capabilities" if status == ConnectorStatus.DEGRADED else "Ready"
        return ConnectorActionPlan(
            request,
            status,
            supported,
            requires_confirmation=confirmation,
            may_retry=bool(not descriptor.mutating and descriptor.idempotent),
            reason=reason,
            risk_level=descriptor.risk_level,
            expected_result=descriptor.description,
            missing_inputs=missing,
            fallback=descriptor.unavailable_reason or "Use a safe local/manual workflow if the connector is unavailable.",
        )

    def execute_action(self, request: ConnectorRequest) -> ConnectorResult:
        parameters = inspect.signature(self.execute).parameters
        if "confirmed" in parameters:
            return self.execute(request.capability, request.arguments, confirmed=request.confirmed)
        return self.execute(request.capability, request.arguments)
