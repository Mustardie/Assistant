from connectors.base import (
    Connector,
    ConnectorCapability,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
)
from connectors.registry import ConnectorRegistry
from connectors.gmail import GmailConnector
from tools.contracts import ToolResult, ToolStatus


class DictConnector(Connector):
    name = "gmail"

    def __init__(self, value, *, mutating=False):
        self.value = value
        self.mutating = mutating
        self.calls = 0

    def status(self):
        return ConnectorStatus.READY

    def capabilities(self):
        return [ConnectorCapability("fetch", mutating=self.mutating, requires_confirmation=self.mutating)]

    def execute(self, capability, arguments):
        self.calls += 1
        return self.value


def test_error_shaped_none_and_partial_results_are_not_success():
    assert not ConnectorResult.normalize("x", "read", {"error": "token expired"}).success
    assert not ConnectorResult.normalize("x", "read", None).success
    partial = ConnectorResult.normalize("x", "read", {"status": "partial", "items": [1], "message": "page two failed"})
    assert not partial.success
    assert partial.partial
    assert partial.error_detail.code == "partial_failure"
    assert ToolResult.from_legacy("connector", True, {"error": "silent failure"}).status == ToolStatus.ERROR


def test_connector_plan_discovers_capability_and_requires_mutation_confirmation():
    registry = ConnectorRegistry()
    registry.register(DictConnector({"success": True}, mutating=True))
    plan = registry.plan(ConnectorRequest("gmail", "fetch", {}))
    assert plan.supported
    assert plan.requires_confirmation
    assert not plan.may_retry


def test_connector_result_normalizes_attachment_profiles():
    registry = ConnectorRegistry()
    registry.register(
        DictConnector(
            {
                "success": True,
                "data": {"attachments": [{"filename": "assignment.pdf", "mime_type": "application/pdf", "id": "a-1"}]},
            }
        )
    )
    result = registry.execute("gmail", "fetch", {}, retries=0)
    assert result.success
    assert result.file_profiles
    assert result.file_profiles[0]["source"] == "email_attachment"
    assert result.file_profiles[0]["filename"] == "assignment.pdf"


def test_connector_exceptions_and_auth_failures_are_clear():
    class BrokenStatus(DictConnector):
        def status(self):
            raise RuntimeError("auth backend unavailable")

    registry = ConnectorRegistry()
    registry.register(BrokenStatus({}))
    result = registry.execute("gmail", "fetch", {})
    assert not result.success
    assert result.error_detail.code == "status_check_failed"
    assert "status check failed" in result.error


def test_confirmed_gmail_send_reaches_backend_as_confirmed():
    class Backend:
        def __init__(self):
            self.confirmed = None

        def send(self, to, subject, body, confirm=False):
            self.confirmed = confirm
            return "sent"

    backend = Backend()
    registry = ConnectorRegistry()
    registry.register(GmailConnector(backend, auth_check=lambda: True))
    blocked = registry.execute("gmail", "send", {"to": "a@example.com", "subject": "Hi", "body": "Hello"})
    sent = registry.execute(
        "gmail",
        "send",
        {"to": "a@example.com", "subject": "Hi", "body": "Hello"},
        confirmed=True,
    )
    assert not blocked.success
    assert sent.success
    assert backend.confirmed is True
