"""Compatibility facade over the verification-first desktop service."""

from __future__ import annotations

from tools.app_discovery import WindowsAppDiscovery
from tools.desktop_control import get_desktop_service
from tools.desktop_models import AppLaunchRequest


def load_apps() -> dict[str, dict]:
    return {
        app.canonical_name.lower(): {
            "name": app.canonical_name,
            "target": app.executable_path,
            "source": app.source,
            "aliases": list(app.aliases),
            "confidence": app.confidence,
            "evidence": list(app.evidence),
        }
        for app in WindowsAppDiscovery().discover()
    }


def launch_app(query: str):
    """Return the legacy tuple while refusing to turn uncertainty into success."""
    result = get_desktop_service().open_app(AppLaunchRequest(str(query)))
    name = result.app.canonical_name if result.app else None
    return result.success and result.verified, name
