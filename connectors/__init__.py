"""Shared connector contracts for general JARVIS integrations."""

from connectors.base import Connector, ConnectorCapability, ConnectorResult, ConnectorStatus
from connectors.registry import ConnectorRegistry

__all__ = ["Connector", "ConnectorCapability", "ConnectorResult", "ConnectorStatus", "ConnectorRegistry"]

