"""ConnectionManager -- central integration/system registry.

Tracks every integration (adapter), its authentication state, the
capabilities it exposes, and connection lifecycle (connect / disconnect /
reconnect). Adapters are thin; they declare capabilities and the agent
queries them instead of assuming every app supports every action.

Connection statuses:
    connected       -- adapter reports it is connected and usable
    connecting      -- auth flow in progress
    requires_auth   -- adapter is registered but needs user authorization
    not_configured  -- adapter needs configuration before it can connect
    unavailable     -- app/service not installed or unusable right now
    disconnected    -- user disconnected or token revoked
"""

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._adapters: dict[str, object] = {}
        self._lock = threading.RLock()
        self._status_cache: dict[str, dict] = {}
        self._on_change: Callable[[str, dict], None] | None = None

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register(self, adapter) -> bool:
        """Register an adapter. Returns True on success."""
        name = self._adapter_name(adapter)
        if not name:
            return False
        with self._lock:
            self._adapters[name] = adapter
            self._status_cache.pop(name, None)
        logger.info("[Connections] Registered adapter '%s'", name)
        return True

    def unregister(self, name: str) -> bool:
        with self._lock:
            removed = self._adapters.pop(name, None)
            self._status_cache.pop(name, None)
        if removed:
            logger.info("[Connections] Unregistered adapter '%s'", name)
            return True
        return False

    def get(self, name: str):
        return self._adapters.get(name.lower())

    def list_adapters(self) -> list[str]:
        with self._lock:
            return list(self._adapters.keys())

    def set_change_handler(self, handler: Callable[[str, dict], None] | None):
        self._on_change = handler

    def clear(self) -> None:
        """Unregister every adapter (used by tests and teardown)."""
        with self._lock:
            self._adapters.clear()
            self._status_cache.clear()

    # ------------------------------------------------------------------ #
    # Status / capabilities
    # ------------------------------------------------------------------ #

    def get_status(self, name: str) -> dict:
        """Resolve a single integration's status."""
        adapter = self.get(name)
        if adapter is None:
            return {"name": name, "status": "unknown", "capabilities": [], "authentication": "none"}
        status = self._resolve_status(adapter)
        self._status_cache[name.lower()] = status
        return status

    def get_all_statuses(self) -> list[dict]:
        """Status for every registered adapter, sorted for display."""
        statuses = []
        for name in self.list_adapters():
            statuses.append(self.get_status(name))
        statuses.sort(key=lambda s: s["name"].lower())
        return statuses

    def _resolve_status(self, adapter) -> dict:
        name = self._adapter_name(adapter)
        try:
            raw = adapter.status()
        except Exception as exc:
            logger.warning("[Connections] status() failed for '%s': %s", name, exc)
            raw = {"status": "unavailable", "error": str(exc)}
        if not isinstance(raw, dict):
            raw = {"status": raw}
        caps = self._adapter_capabilities(adapter)
        return {
            "name": name,
            "status": raw.get("status", "unknown"),
            "connected": raw.get("status") == "connected",
            "detail": raw.get("message") or raw.get("error") or "",
            "capabilities": caps,
            "authentication": getattr(adapter, "authentication", "none"),
            "description": str(getattr(adapter, "description", "") or ""),
        }

    def _adapter_capabilities(self, adapter) -> list[str]:
        declared = getattr(adapter, "capabilities", None)
        if isinstance(declared, list):
            return [str(c) for c in declared]
        if isinstance(declared, dict):
            return [str(c) for c in declared.keys()]
        return []

    def _adapter_name(self, adapter) -> str:
        return str(getattr(adapter, "name", "")).lower() or adapter.__class__.__name__.lower()

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    def connect(self, name: str) -> dict:
        """Connect an integration. For OAuth adapters this triggers the
        authorization flow; for local adapters it checks/establishes local
        access."""
        adapter = self.get(name)
        if adapter is None:
            return {"success": False, "name": name, "error": f"Unknown integration: {name}"}
        connect = getattr(adapter, "connect", None)
        if not callable(connect):
            return {"success": False, "name": name, "error": f"'{name}' cannot be connected."}
        try:
            result = connect()
        except Exception as exc:
            logger.exception("[Connections] connect() failed for '%s'", name)
            result = {"success": False, "error": str(exc)}
        result.setdefault("name", name)
        self._notify(name, self.get_status(name))
        return result

    def disconnect(self, name: str) -> dict:
        adapter = self.get(name)
        if adapter is None:
            return {"success": False, "name": name, "error": f"Unknown integration: {name}"}
        disconnect = getattr(adapter, "disconnect", None)
        try:
            if callable(disconnect):
                result = disconnect()
            else:
                result = {"success": True}
        except Exception as exc:
            logger.exception("[Connections] disconnect() failed for '%s'", name)
            result = {"success": False, "error": str(exc)}
        result.setdefault("name", name)
        self._notify(name, self.get_status(name))
        return result

    def reconnect(self, name: str) -> dict:
        self.disconnect(name)
        return self.connect(name)

    # ------------------------------------------------------------------ #
    # Capability-driven dispatch
    # ------------------------------------------------------------------ #

    def find_adapters_with_capability(self, capability: str) -> list[str]:
        """Return adapter names that declare the given capability."""
        result = []
        for name in self.list_adapters():
            if capability in self.get_status(name).get("capabilities", []):
                result.append(name)
        return result

    def is_capability_available(self, capability: str) -> bool:
        """True when at least one connected adapter supports a capability."""
        for name in self.list_adapters():
            status = self.get_status(name)
            if status.get("connected") and capability in status.get("capabilities", []):
                return True
        return False

    def _notify(self, name: str, status: dict):
        if self._on_change:
            try:
                self._on_change(name, status)
            except Exception as exc:
                logger.warning("[Connections] change handler failed: %s", exc)


connection_manager = ConnectionManager()
