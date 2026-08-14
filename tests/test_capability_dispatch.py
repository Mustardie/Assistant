"""End-to-end tests for the capability dispatch chain:

    universal tool -> ConnectionManager -> adapter capability method

Uses a fake connected messaging adapter to avoid any network access."""

import pytest

from connections.manager import ConnectionManager
from tools.communication_tool import read_messages, search_messages, send_message
from tools.calendar_task_tool import create_task, list_tasks


class FakeMessenger:
    name = "fakechat"
    display_name = "FakeChat"
    authentication = "api_key"
    capabilities = ["read_messages", "search_messages", "send_message"]

    def __init__(self):
        self.messages = [
            {"id": "1", "sender": "Teacher", "content": "Complete Q1-10 by Friday"},
            {"id": "2", "sender": "Friend", "content": "Lunch tomorrow?"},
        ]

    def connect(self):
        return {"success": True}

    def disconnect(self):
        return {"success": True}

    def status(self):
        return {"status": "connected", "message": "connected"}

    def read_messages(self, limit=20, **kwargs):
        return {"success": True, "messages": self.messages[:limit], "count": len(self.messages[:limit])}

    def search_messages(self, query, limit=20, **kwargs):
        matches = [m for m in self.messages if query.lower() in m["content"].lower()]
        return {"success": True, "messages": matches, "count": len(matches)}

    def send_message(self, recipient, text, **kwargs):
        self.sent = (recipient, text)
        return {"success": True, "id": "9"}


@pytest.fixture
def manager():
    mgr = ConnectionManager()
    yield mgr
    mgr.clear()


@pytest.fixture
def fake_messenger(monkeypatch):
    # Point the universal tools at this manager.
    messenger = FakeMessenger()
    yield messenger
    messenger.sent = None


def test_dispatch_read_messages(manager, fake_messenger, monkeypatch):
    monkeypatch.setattr("connections.manager.connection_manager", manager)
    manager.register(fake_messenger)

    result = read_messages(limit=2)
    assert result["success"] is True
    assert result["count"] == 2
    assert result["messages"][0]["content"].startswith("Complete")


def test_dispatch_search_messages(manager, fake_messenger, monkeypatch):
    monkeypatch.setattr("connections.manager.connection_manager", manager)
    manager.register(fake_messenger)

    result = search_messages(query="Friday")
    assert result["count"] == 1
    assert result["messages"][0]["sender"] == "Teacher"


def test_dispatch_no_adapters_graceful(manager, monkeypatch):
    monkeypatch.setattr("connections.manager.connection_manager", manager)
    result = read_messages()
    assert result["success"] is False
    assert "No connected service supports" in result["error"]


def test_dispatch_skips_not_connected(manager, fake_messenger, monkeypatch):
    monkeypatch.setattr("connections.manager.connection_manager", manager)

    class OfflineMessenger(FakeMessenger):
        def status(self):
            return {"status": "requires_auth", "message": "not connected"}

    manager.register(OfflineMessenger())
    result = read_messages()
    assert result["success"] is False  # adapter declared cap but isn't connected


def test_full_assignment_scenario(manager, fake_messenger, monkeypatch):
    """The flagship workflow routed via a real dispatch from the tool layer."""
    monkeypatch.setattr("connections.manager.connection_manager", manager)
    from workflows import HandleAssignmentWorkflow, register_all

    register_all()
    manager.register(fake_messenger)

    # simulate the workflow receiving the event and calling the tool layer
    workflow = HandleAssignmentWorkflow()
    event = fake_messenger.messages[0]
    # The workflow needs an AppEvent; emulate its message body.
    from events import NEW_MESSAGE, make_event
    app_event = make_event("fakechat", NEW_MESSAGE, sender="Teacher",
                           content=event["content"])
    # Patch the workflow's tool calls to the real dispatch.
    from tools import tool_registry
    real_run_tool = tool_registry.run_tool
    monkeypatch.setattr(tool_registry, "run_tool", real_run_tool)

    # create_task will dispatch to... no task adapter connected -> the
    # policy layer blocks medium-risk creation without confirmation.
    result = workflow.run(app_event)
    assert result.success is False
    assert "confirmation" in result.final_result.lower()