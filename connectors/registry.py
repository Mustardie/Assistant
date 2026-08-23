"""Connector discovery, auth gating, normalized errors, and safe retries."""

from __future__ import annotations

from connectors.base import Connector, ConnectorResult, ConnectorStatus


class ConnectorRegistry:
    def __init__(self):
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        if not getattr(connector, "name", None):
            raise ValueError("Connector must have a name")
        self._connectors[connector.name] = connector

    def names(self) -> list[str]:
        return sorted(self._connectors)

    def status(self, name: str) -> ConnectorStatus:
        connector = self._connectors.get(name)
        return connector.status() if connector else ConnectorStatus.UNAVAILABLE

    def capabilities(self, name: str) -> list[dict]:
        connector = self._connectors.get(name)
        if not connector:
            return []
        return [capability.__dict__.copy() for capability in connector.capabilities()]

    def execute(self, name: str, capability: str, arguments: dict, *, confirmed: bool = False, retries: int = 1) -> ConnectorResult:
        connector = self._connectors.get(name)
        if not connector:
            return ConnectorResult(False, error=f"Connector '{name}' is not installed", connector=name, capability=capability)
        status = connector.status()
        if status != ConnectorStatus.READY:
            return ConnectorResult(False, error=f"Connector '{name}' status is {status.value}", connector=name, capability=capability)
        descriptor = next((item for item in connector.capabilities() if item.name == capability), None)
        if descriptor is None:
            return ConnectorResult(False, error=f"Connector '{name}' does not support '{capability}'", connector=name, capability=capability)
        if descriptor.requires_confirmation and not confirmed:
            return ConnectorResult(False, error=f"'{capability}' requires user confirmation", connector=name, capability=capability)

        attempts = max(1, retries + 1)
        result = None
        for _ in range(attempts):
            try:
                result = ConnectorResult.normalize(name, capability, connector.execute(capability, arguments))
            except Exception as exc:
                result = ConnectorResult(False, error=str(exc), retryable=False, connector=name, capability=capability)
            if result.success or not result.retryable or descriptor.mutating:
                break
        return result

