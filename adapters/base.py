"""Adapter base classes for the integration layer.

Adapters are THIN. They declare capabilities and expose them to the
universal tool layer -- they never re-implement the agent/tool system.
Each integration declares:

    {
        "name": "discord",
        "capabilities": ["read_messages", "send_message", ...],
        "authentication": "oauth"
    }
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class BaseAdapter:
    """Common interface every integration adapter implements."""

    name: str = ""
    display_name: str = ""
    authentication: str = "none"  # "oauth" | "local" | "api_key" | "none"
    capabilities: list[str] = []
    description: str = ""  # shown when the user clicks an integration row

    def __init__(self):
        self._ready = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def connect(self) -> dict:
        """Establish the connection / run authorization. Returns
        {'success': bool, 'message': str, ...}."""
        raise NotImplementedError

    def disconnect(self) -> dict:
        """Revoke tokens / drop local access. Returns
        {'success': bool, 'message': str}."""
        return {"success": True, "message": f"{self.name} disconnected."}

    def status(self) -> dict:
        """Return {'status': str, 'message': str}. Statuses:
        'connected' | 'connecting' | 'requires_auth' | 'not_configured' |
        'unavailable' | 'disconnected'."""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Capabilities
    # ------------------------------------------------------------------ #

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _ok(self, **extra) -> dict:
        data = {"success": True}
        data.update(extra)
        return data

    def _fail(self, message: str, **extra) -> dict:
        data = {"success": False, "error": message}
        data.update(extra)
        return data


class LocalAppAdapter(BaseAdapter):
    """Adapter for an application running on the local machine, driven
    through Windows UI Automation / accessibility APIs rather than a web
    API. Used for WhatsApp Desktop, Teams, Spotify client, VS Code, etc.

    Subclasses define how to detect the app, and can override the default
    UI-automation-based dispatch for app-specific quirks.
    """

    authentication = "local"
    executable_names: list[str] = []
    process_aliases: list[str] = []
    launch_paths: list[str] = []  # executables/CLI names to start the app
    capability_map: dict[str, str] = {}  # capability -> uia pattern helper

    def detect_app(self) -> dict:
        """Return {'installed': bool, 'running': bool, 'path': str}."""
        raise NotImplementedError

    def status(self) -> dict:
        info = self.detect_app()
        if not info.get("installed"):
            return {"status": "unavailable",
                    "message": f"{self.display_name} isn't installed on this machine."}
        if not info.get("running"):
            return {"status": "requires_auth",
                    "message": f"{self.display_name} is installed but not running. "
                               "Click Connect to launch it."}
        return {"status": "connected",
                "message": f"{self.display_name} is available."}

    def connect(self) -> dict:
        """Connect a local app adapter: launches the app when it is
        installed but not running, then re-checks."""
        info = self.detect_app()
        if not info.get("installed"):
            return self._fail(f"{self.display_name} isn't installed.")
        if not info.get("running"):
            launched = self._launch_app()
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if self.detect_app().get("running"):
                    break
                time.sleep(0.4)
            if self.detect_app().get("running"):
                return self._ok(message=f"{self.display_name} launched and connected.")
            if launched:
                return self._fail(
                    f"{self.display_name} is starting... If it doesn't appear, "
                    "launch it manually."
                )
            return self._fail(
                f"{self.display_name} is installed but not running. Start it first."
            )
        return self._ok(message=f"{self.display_name} connected.")

    def _launch_app(self) -> bool:
        """Start the app. Subclasses may set `launch_paths` (executables
        or CLI names). Returns True when a launch attempt was made."""
        import subprocess
        for entry in self.launch_paths or []:
            try:
                subprocess.Popen([entry], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
            except Exception:
                continue
        return False


class ApiKeyAdapter(BaseAdapter):
    """Adapter for services that authenticate with a simple API key
    (Telegram bot token, Tavily, etc.). Keys are read from configuration
    (env vars or the secure local token store) and are never hardcoded."""

    authentication = "api_key"
    config_key: str = ""  # settings field name holding the API key

    def _api_key(self) -> str:
        from connections.secrets import get_token
        return get_token(self.config_key)

    def save_api_key(self, value: str) -> None:
        """Persist the API key to the secure local token store."""
        from connections.secrets import save_token
        save_token(self.config_key, str(value or ""))

    def is_configured(self) -> bool:
        return bool(self._api_key())

    def status(self) -> dict:
        if not self.is_configured():
            return {"status": "not_configured",
                    "message": f"{self.display_name} needs an API key to connect."}
        return {"status": "requires_auth",
                "message": f"{self.display_name} is configured but not connected."}


# Ensure subclasses override the abstract methods cleanly.
BaseAdapter.__abstractmethods__ = frozenset()
