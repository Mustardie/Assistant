"""Tests for the Connections subsystem: ConnectionManager, capability
discovery, adapter registration, OAuth state handling, and credential
storage. No live external services are touched."""

import threading
import time

import pytest

from adapters.base import BaseAdapter, LocalAppAdapter, ApiKeyAdapter
from connections.manager import ConnectionManager
from connections.storage import CredentialStorage


class FakeOAuthAdapter(BaseAdapter):
    name = "fake_oauth"
    display_name = "Fake OAuth"
    authentication = "oauth"
    capabilities = ["read_messages", "send_message", "search_messages"]

    def __init__(self):
        super().__init__()
        self._connected = False
        self._connect_calls = 0
        self._disconnect_calls = 0

    def connect(self):
        self._connect_calls += 1
        if not getattr(self, "reject_connect", False):
            self._connected = True
            return {"success": True, "message": "connected"}
        return {"success": False, "message": "rejected"}

    def disconnect(self):
        self._disconnect_calls += 1
        self._connected = False
        return {"success": True, "message": "disconnected"}

    def status(self):
        if self._connected:
            return {"status": "connected", "message": "ok"}
        return {"status": "requires_auth", "message": "not connected"}


class CrashingAdapter(BaseAdapter):
    name = "crashy"
    capabilities = ["read_messages"]

    def status(self):
        raise RuntimeError("boom")


def test_adapter_registration_and_listing():
    mgr = ConnectionManager()
    mgr.register(FakeOAuthAdapter())
    assert mgr.list_adapters() == ["fake_oauth"]
    assert mgr.get("fake_oauth") is not None
    assert mgr.get("FAKE_OAUTH") is not None
    assert mgr.unregister("fake_oauth") is True
    assert mgr.list_adapters() == []


def test_capability_discovery():
    mgr = ConnectionManager()
    mgr.register(FakeOAuthAdapter())
    assert mgr.find_adapters_with_capability("send_message") == ["fake_oauth"]
    assert mgr.is_capability_available("send_message") is False  # not connected yet
    mgr.connect("fake_oauth")
    assert mgr.is_capability_available("send_message") is True
    assert mgr.is_capability_available("play_media") is False


def test_status_reporting():
    mgr = ConnectionManager()
    mgr.register(FakeOAuthAdapter())
    status = mgr.get_status("fake_oauth")
    assert status["name"] == "fake_oauth"
    assert status["status"] == "requires_auth"
    assert status["connected"] is False
    assert "read_messages" in status["capabilities"]
    assert status["authentication"] == "oauth"


def test_adapter_status_exceptions_fail_gracefully():
    mgr = ConnectionManager()
    mgr.register(CrashingAdapter())
    status = mgr.get_status("crashy")
    assert status["status"] == "unavailable"
    assert status["connected"] is False


def test_connect_and_disconnect_lifecycle():
    mgr = ConnectionManager()
    adapter = FakeOAuthAdapter()
    mgr.register(adapter)
    result = mgr.connect("fake_oauth")
    assert result["success"] is True
    assert adapter._connect_calls == 1
    assert mgr.get_status("fake_oauth")["connected"] is True

    result = mgr.disconnect("fake_oauth")
    assert result["success"] is True
    assert adapter._disconnect_calls == 1
    assert mgr.get_status("fake_oauth")["connected"] is False


def test_unknown_integration_fails_cleanly():
    mgr = ConnectionManager()
    result = mgr.connect("nope")
    assert result["success"] is False
    assert "Unknown" in result["error"]
    status = mgr.get_status("nope")
    assert status["status"] == "unknown"


def test_change_handler_notified():
    events = []
    mgr = ConnectionManager()
    mgr.set_change_handler(lambda name, status: events.append((name, status)))
    mgr.register(FakeOAuthAdapter())
    mgr.connect("fake_oauth")
    assert len(events) >= 1
    assert events[-1][0] == "fake_oauth"


def test_all_statuses_sorted():
    mgr = ConnectionManager()
    mgr.register(FakeOAuthAdapter())
    mgr.register(CrashingAdapter())
    statuses = mgr.get_all_statuses()
    names = [s["name"] for s in statuses]
    assert names == sorted(names)


# --------------------------------------------------------------------- #
# Credential storage (encrypted, local-only)
# --------------------------------------------------------------------- #


def _store(tmp_path):
    """Isolated file-based credential store (never touches the real
    Windows Credential Manager during tests)."""
    return CredentialStorage(storage_dir=tmp_path / "creds", use_cred_manager=False)


def test_credential_storage_roundtrip(tmp_path):
    store = _store(tmp_path)
    assert store.save_credentials("test_service", {"access_token": "abc123", "refresh_token": "xyz"})
    creds = store.get_credentials("test_service")
    assert creds["access_token"] == "abc123"
    assert creds["refresh_token"] == "xyz"


def test_credential_storage_delete(tmp_path):
    store = _store(tmp_path)
    store.save_credentials("test_service", {"access_token": "abc"})
    assert store.delete_credentials("test_service") is True
    assert store.get_credentials("test_service") is None


def test_credential_storage_persists_across_instances(tmp_path):
    store1 = _store(tmp_path)
    store1.save_credentials("svc_a", {"token": "1"})
    store2 = _store(tmp_path)
    assert store2.get_credentials("svc_a") == {"token": "1"}
    assert "svc_a" in store2.list_services()


# --------------------------------------------------------------------- #
# Base adapter behaviors
# --------------------------------------------------------------------- #


class FakeLocalAppAdapter(LocalAppAdapter):
    name = "fakelocal"
    display_name = "Fake Local App"

    def __init__(self, installed=True, running=True):
        super().__init__()
        self._installed = installed
        self._running = running

    def detect_app(self):
        return {"installed": self._installed, "running": self._running, "path": "C:/x"}


def test_local_app_adapter_status_variants():
    assert FakeLocalAppAdapter(installed=False, running=False).status()["status"] == "unavailable"
    assert FakeLocalAppAdapter(installed=True, running=False).status()["status"] == "requires_auth"
    assert FakeLocalAppAdapter(installed=True, running=True).status()["status"] == "connected"


def test_api_key_adapter():
    adapter = ApiKeyAdapter()
    adapter.config_key = "tavily_api_key"  # real settings field
    assert adapter.is_configured() == bool(adapter._api_key())
    status = adapter.status()
    assert status["status"] in ("not_configured", "requires_auth")


def test_supports_capability():
    a = FakeOAuthAdapter()
    assert a.supports("read_messages")
    assert not a.supports("play_media")