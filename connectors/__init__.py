"""Shared connector contracts for general JARVIS integrations."""

from connectors.base import (
    Connector,
    ConnectorActionPlan,
    ConnectorCapability,
    ConnectorError,
    ConnectorRequest,
    ConnectorResult,
    ConnectorRisk,
    ConnectorStatus,
)
from connectors.registry import ConnectorRegistry
from connectors.apps import AppLauncherConnector
from connectors.browser_downloads import BrowserDownloadsConnector
from connectors.calendar import GoogleCalendarConnector
from connectors.drive import GoogleDriveConnector
from connectors.messaging import DiscordConnector, WhatsAppConnector

__all__ = [
    "Connector",
    "ConnectorActionPlan",
    "ConnectorCapability",
    "ConnectorError",
    "ConnectorRequest",
    "ConnectorResult",
    "ConnectorRisk",
    "ConnectorStatus",
    "ConnectorRegistry",
    "AppLauncherConnector",
    "BrowserDownloadsConnector",
    "GoogleCalendarConnector",
    "GoogleDriveConnector",
    "DiscordConnector",
    "WhatsAppConnector",
]
