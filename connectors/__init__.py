"""Shared connector contracts for general JARVIS integrations."""

from connectors.base import (
    Connector,
    ConnectorActionPlan,
    ConnectorCapability,
    ConnectorError,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
)
from connectors.registry import ConnectorRegistry

__all__ = [
    "Connector",
    "ConnectorActionPlan",
    "ConnectorCapability",
    "ConnectorError",
    "ConnectorRequest",
    "ConnectorResult",
    "ConnectorStatus",
    "ConnectorRegistry",
]
