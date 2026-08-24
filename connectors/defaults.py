"""Lazily constructed production connector registry."""

from __future__ import annotations

from connectors.gmail import GmailConnector
from connectors.apps import AppLauncherConnector
from connectors.browser_downloads import BrowserDownloadsConnector
from connectors.calendar import GoogleCalendarConnector
from connectors.drive import GoogleDriveConnector
from connectors.messaging import DiscordConnector, WhatsAppConnector
from connectors.registry import ConnectorRegistry

_registry = None


def default_registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        from skills.gmail import gmail
        registry = ConnectorRegistry()
        registry.register(GmailConnector(gmail))
        registry.register(GoogleDriveConnector())
        registry.register(GoogleCalendarConnector())
        registry.register(DiscordConnector())
        registry.register(WhatsAppConnector())
        registry.register(BrowserDownloadsConnector())
        registry.register(AppLauncherConnector())
        _registry = registry
    return _registry


def reset_default_registry() -> None:
    """Rebuild connectors after local settings or credentials change."""
    global _registry
    _registry = None
