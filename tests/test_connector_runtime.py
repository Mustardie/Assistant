from connectors.base import Connector, ConnectorCapability, ConnectorResult, ConnectorStatus
from connectors.gmail import GmailConnector
from connectors.registry import ConnectorRegistry
from tools import tool_registry


class FakeConnector(Connector):
    name = "fake"

    def __init__(self, status=ConnectorStatus.READY, mutating=False):
        self.current_status = status
        self.mutating = mutating
        self.calls = 0

    def status(self):
        return self.current_status

    def capabilities(self):
        return [ConnectorCapability("fetch", mutating=self.mutating, requires_confirmation=self.mutating)]

    def execute(self, capability, arguments):
        self.calls += 1
        if self.calls == 1:
            return ConnectorResult(False, error="temporary", retryable=True, connector=self.name, capability=capability)
        return ConnectorResult(True, data={"items": [1]}, connector=self.name, capability=capability)


def test_registry_discovers_capabilities_and_retries_safe_reads():
    registry = ConnectorRegistry()
    connector = FakeConnector()
    registry.register(connector)
    assert registry.names() == ["fake"]
    assert registry.capabilities("fake")[0]["name"] == "fetch"
    result = registry.execute("fake", "fetch", {}, retries=1)
    assert result.success
    assert connector.calls == 2


def test_registry_blocks_auth_and_confirmed_mutation_is_not_retried():
    registry = ConnectorRegistry()
    unavailable = FakeConnector(ConnectorStatus.AUTH_REQUIRED)
    registry.register(unavailable)
    result = registry.execute("fake", "fetch", {})
    assert not result.success
    assert "auth_required" in result.error
    assert unavailable.calls == 0

    mutating = FakeConnector(mutating=True)
    registry.register(mutating)
    blocked = registry.execute("fake", "fetch", {})
    assert not blocked.success
    assert "confirmation" in blocked.error
    attempted = registry.execute("fake", "fetch", {}, confirmed=True, retries=3)
    assert not attempted.success
    assert mutating.calls == 1


class FakeGmail:
    def read(self, limit=10):
        return [{"subject": "Assignment", "limit": limit}]

    def search(self, query, limit=10):
        return [{"subject": query, "limit": limit}]


def test_gmail_adapter_has_auth_status_and_normalized_results():
    connector = GmailConnector(FakeGmail(), auth_check=lambda: True)
    assert connector.status() == ConnectorStatus.READY
    names = {item.name for item in connector.capabilities()}
    assert {"read", "search", "send", "delete"} <= names
    result = connector.execute("search", {"query": "assignment", "limit": 5})
    assert result.success
    assert result.data[0]["subject"] == "assignment"


def test_connector_tools_expose_normalized_registry(monkeypatch):
    registry = ConnectorRegistry()
    registry.register(FakeConnector())
    monkeypatch.setattr("connectors.defaults.default_registry", lambda: registry)
    assert tool_registry.connector_status("fake")["status"] == "ready"
    assert tool_registry.connector_capabilities("fake")["capabilities"][0]["name"] == "fetch"
    result = tool_registry.connector_execute("fake", "fetch", {})
    assert result["success"]
    assert result["data"] == {"items": [1]}
