"""Lazily constructed production connector registry."""

from __future__ import annotations

from connectors.gmail import GmailConnector
from connectors.registry import ConnectorRegistry

_registry = None


def default_registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        from skills.gmail import gmail
        registry = ConnectorRegistry()
        registry.register(GmailConnector(gmail))
        _registry = registry
    return _registry

