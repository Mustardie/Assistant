"""Tests for the Connections UI page (Phase 9).

Runs headless via the Qt offscreen platform so it works on CI machines
without a display. The page talks to the ConnectionManager through a
fake so no real network or OAuth flow ever starts.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    _app = QApplication.instance() or QApplication([])
    yield _app


class _FakeManager:
    def __init__(self):
        self.connected = {"filesystem": True, "google": False}
        self.calls = []

    def get_all_statuses(self):
        rows = [
            {
                "name": "filesystem",
                "display_name": "File System",
                "status": "connected" if self.connected["filesystem"] else "requires_auth",
                "connected": self.connected["filesystem"],
                "capabilities": ["read_document", "search"],
                "authentication": "none",
                "detail": "",
            },
            {
                "name": "google",
                "display_name": "Google",
                "status": "connected" if self.connected["google"] else "requires_auth",
                "connected": self.connected["google"],
                "capabilities": ["send_message", "create_event"],
                "authentication": "oauth",
                "detail": "",
            },
        ]
        return rows

    def connect(self, name):
        self.calls.append(("connect", name))
        self.connected[name] = True
        return {"success": True, "name": name, "message": f"{name} connected."}

    def disconnect(self, name):
        self.calls.append(("disconnect", name))
        self.connected[name] = False
        return {"success": True, "name": name, "message": f"{name} disconnected."}


def _spin_until(predicate, timeout_ms=2000):
    loop = QEventLoop()
    QTimer.singleShot(timeout_ms, loop.quit)

    def poll():
        if predicate():
            loop.quit()
            return
        QTimer.singleShot(20, poll)

    QTimer.singleShot(20, poll)
    loop.exec()


def test_page_lists_adapters_with_status(app):
    from ui.widgets.connections_page import ConnectionsPage

    page = ConnectionsPage(manager=_FakeManager())
    assert set(page._rows.keys()) == {"filesystem", "google"}
    assert "2 services" in page._summary.text()
    assert "1 connected" in page._summary.text()
    assert page._rows["filesystem"]._action.text() == "Disconnect"
    assert page._rows["google"]._action.text() == "Connect"


def test_connect_runs_off_thread_and_refreshes(app):
    from ui.widgets.connections_page import ConnectionsPage

    mgr = _FakeManager()
    page = ConnectionsPage(manager=mgr)
    google_row = page._rows["google"]
    google_row._action.click()
    _spin_until(lambda: mgr.connected["google"])
    assert ("connect", "google") in mgr.calls
    assert google_row._action.text() == "Disconnect"
    assert "2 connected" in page._summary.text()


def test_disconnect_runs_off_thread_and_refreshes(app):
    from ui.widgets.connections_page import ConnectionsPage

    mgr = _FakeManager()
    mgr.connected["google"] = True
    page = ConnectionsPage(manager=mgr)
    fs_row = page._rows["filesystem"]
    fs_row._action.click()
    _spin_until(lambda: not mgr.connected["filesystem"])
    assert ("disconnect", "filesystem") in mgr.calls
    assert fs_row._action.text() == "Connect"
    assert "1 connected" in page._summary.text()


def test_page_survives_theme_reload(app):
    from ui.widgets.connections_page import ConnectionsPage

    page = ConnectionsPage(manager=_FakeManager())
    page.apply_theme()
    assert page._title.text() == "Connections"
