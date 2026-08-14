"""Tests for the API client + adapter layer."""

import json

import pytest

from adapters.api import ApiClient, ApiError, RESTAdapter
from adapters.discord import DiscordAdapter
from adapters.google import GoogleAdapter
from adapters.microsoft import MicrosoftAdapter
from adapters.notion import NotionAdapter
from adapters.spotify import SpotifyAdapter
from adapters.todoist import TodoistAdapter
from adapters.filesystem import FileSystemAdapter
from adapters.vscode import VSCodeAdapter
from adapters.whatsapp import WhatsAppAdapter
from adapters.windows import WindowsAdapter


# --------------------------------------------------------------------------- #
# ApiClient
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, payload: dict | list, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_api_client_get(monkeypatch):
    calls = {}

    def fake_urlopen(req, body, timeout):
        calls["url"] = req.full_url
        calls["method"] = req.get_method()
        return FakeResponse({"ok": True, "items": [1, 2]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ApiClient("https://example.com/api", token="abc")
    result = client.get("/things", params={"limit": "5"})
    assert result == {"ok": True, "items": [1, 2]}
    assert "limit=5" in calls["url"]
    assert "https://example.com/api/things" in calls["url"]
    assert calls["method"] == "GET"


def test_api_client_post_json(monkeypatch):
    calls = {}

    def fake_urlopen(req, body, timeout):
        calls["url"] = req.full_url
        calls["method"] = req.get_method()
        calls["body"] = body.decode() if body else ""
        calls["token"] = req.get_header("Authorization")
        return FakeResponse({"created": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ApiClient("https://x.io/v2", token="tok123")
    result = client.post("/tasks", json_body={"title": "Hi"})
    assert result == {"created": True}
    assert calls["method"] == "POST"
    assert calls["token"] == "Bearer tok123"
    assert json.loads(calls["body"]) == {"title": "Hi"}


def test_api_client_http_error(monkeypatch):
    import urllib.error

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("url", 401, "Unauthorized", {}, None)

        def read(self):
            return b'{"error": "nope"}'

    def fake_urlopen(req, body, timeout):
        raise FakeHTTPError()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ApiClient("https://x.io")
    with pytest.raises(ApiError) as exc_info:
        client.get("/me")
    assert exc_info.value.status == 401


# --------------------------------------------------------------------------- #
# REST adapter (Discord as representative)
# --------------------------------------------------------------------------- #

def test_discord_status_not_configured(monkeypatch):
    adapter = DiscordAdapter()
    monkeypatch.setattr(adapter, "_api_key", lambda: "")
    status = adapter.status()
    assert status["status"] == "not_configured"


def test_rest_adapter_graceful_guarded(monkeypatch):
    adapter = DiscordAdapter()
    monkeypatch.setattr(adapter, "is_configured", lambda: True)
    monkeypatch.setattr(adapter, "_http", lambda: _FakeClient())

    result = adapter._guarded("read_messages", limit=5)
    assert result["success"] is True
    assert result["count"] == 1
    assert result["messages"][0]["content"] == "hi"

    # A capability that hits ApiError degrades gracefully.
    class BoomClient:
        def get(self, *a, **k):
            raise ApiError(429, "rate limited")

        def post(self, *a, **k):
            raise ApiError(429, "rate limited")

    monkeypatch.setattr(adapter, "_http", lambda: BoomClient())
    failed = adapter._guarded("read_messages")
    assert failed["success"] is False
    assert "429" in failed["error"]


class _FakeClient:
    def get(self, path, **kwargs):
        return [{"id": "1", "author": {"username": "bob"}, "content": "hi",
                 "timestamp": "2025-01-01T00:00:00"}]

    def post(self, path, **kwargs):
        return {"ok": True, "id": "42"}


def test_discord_read_messages(monkeypatch):
    adapter = DiscordAdapter()
    monkeypatch.setattr(adapter, "is_configured", lambda: True)
    monkeypatch.setattr(adapter, "_http", lambda: _FakeClient())
    result = adapter.read_messages(limit=5)
    assert result["success"] is True
    assert result["messages"][0]["sender"] == "bob"


def test_discord_send_message_no_channel(monkeypatch):
    adapter = DiscordAdapter()
    monkeypatch.setattr(adapter, "is_configured", lambda: True)

    class NoChannel:
        def get(self, *a, **k):
            return []

        def post(self, *a, **k):
            raise AssertionError("should not post")

    monkeypatch.setattr(adapter, "_http", lambda: NoChannel())
    result = adapter.send_message(recipient="nobody", text="hi")
    assert result["success"] is False
    assert "nobody" in result["error"]


# --------------------------------------------------------------------------- #
# Todoist / Notion
# --------------------------------------------------------------------------- #

def test_todoist_create_task(monkeypatch):
    adapter = TodoistAdapter()
    monkeypatch.setattr(adapter, "is_configured", lambda: True)

    class Client:
        def post(self, path, json_body=None, **kwargs):
            assert path == "/tasks"
            return {"id": "t1"}

        def get(self, *a, **k):
            return []

    monkeypatch.setattr(adapter, "_http", lambda: Client())
    result = adapter.create_task("Finish report", due="tomorrow")
    assert result["success"] is True
    assert result["id"] == "t1"


def test_notion_create_task(monkeypatch):
    adapter = NotionAdapter()
    monkeypatch.setattr(adapter, "is_configured", lambda: True)

    class Client:
        def post(self, path, json_body=None, **kwargs):
            if path == "/search":
                return {"results": [{"id": "db1"}]}
            if path == "/pages":
                return {"id": "p1"}
            return {}

    monkeypatch.setattr(adapter, "_http", lambda: Client())
    result = adapter.create_task("Read chapter 3")
    assert result["success"] is True
    assert result["id"] == "p1"


# --------------------------------------------------------------------------- #
# OAuth adapters
# --------------------------------------------------------------------------- #

def test_google_status_requires_auth(monkeypatch, tmp_path):
    adapter = GoogleAdapter()
    monkeypatch.setattr(adapter, "_token_path", lambda: tmp_path / "token.json")
    monkeypatch.setattr(adapter, "_client_secret_path",
                        lambda: tmp_path / "client_secret.json")
    (tmp_path / "client_secret.json").write_text("{}")
    assert adapter.status()["status"] == "requires_auth"


def test_google_connect_not_configured(monkeypatch, tmp_path):
    adapter = GoogleAdapter()
    monkeypatch.setattr(adapter, "_token_path", lambda: tmp_path / "token.json")
    monkeypatch.setattr(adapter, "_client_secret_path",
                        lambda: tmp_path / "missing.json")
    result = adapter.connect()
    assert result["success"] is False


def test_google_disconnect(monkeypatch, tmp_path):
    adapter = GoogleAdapter()
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(adapter, "_token_path", lambda: token)
    result = adapter.disconnect()
    assert result["success"] is True
    assert not token.exists()


def test_google_guarded_unauthenticated(monkeypatch):
    adapter = GoogleAdapter()
    monkeypatch.setattr(adapter, "_access_token", lambda: "")
    result = adapter._guarded("read_messages")
    assert result["success"] is False
    assert "authorize" in result["error"].lower()


def test_microsoft_read_messages(monkeypatch):
    adapter = MicrosoftAdapter()
    monkeypatch.setattr(adapter, "_access_token", lambda: "tok")

    class Client:
        def get(self, path, **kwargs):
            assert path == "/me/messages"
            return {"value": [{"id": "m1", "subject": "S", "from": {"emailAddress": {"address": "a@b.c"}}}]}

    monkeypatch.setattr(adapter, "_http", lambda: Client())
    result = adapter.read_messages(limit=5)
    assert result["success"] is True
    assert result["messages"][0]["id"] == "m1"


def test_spotify_search_media(monkeypatch):
    adapter = SpotifyAdapter()
    monkeypatch.setattr(adapter, "_access_token", lambda: "tok")

    class Client:
        def get(self, path, **kwargs):
            return {"tracks": {"items": [{"name": "Song", "artists": [{"name": "Artist"}], "uri": "spotify:track:1"}]},
                    "albums": {"items": []}, "playlists": {"items": []}}

    monkeypatch.setattr(adapter, "_http", lambda: Client())
    result = adapter.search_media("song")
    assert result["success"] is True
    assert result["tracks"][0]["name"] == "Song"


# --------------------------------------------------------------------------- #
# Local adapters
# --------------------------------------------------------------------------- #

def test_windows_always_connected():
    adapter = WindowsAdapter()
    assert adapter.status()["status"] == "connected"
    assert adapter.supports("get_clipboard")
    assert adapter.supports("launch_process")


def test_filesystem_read_document(tmp_path):
    adapter = FileSystemAdapter(root=tmp_path)
    doc = tmp_path / "note.txt"
    doc.write_text("hello world", encoding="utf-8")
    result = adapter.read_document(path=str(doc))
    assert result["success"] is True
    assert "hello world" in result.get("text", "")


def test_filesystem_list_files(tmp_path):
    adapter = FileSystemAdapter(root=tmp_path)
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    result = adapter.list_files()
    assert result["count"] == 2


def test_filesystem_search(tmp_path):
    adapter = FileSystemAdapter(root=tmp_path)
    (tmp_path / "report_q3.txt").write_text("quarterly numbers", encoding="utf-8")
    (tmp_path / "junk.log").write_text("nothing", encoding="utf-8")
    result = adapter.search("quarterly")
    assert result["count"] == 1
    assert "report_q3.txt" in result["results"][0]["path"]


def test_vscode_read_file(tmp_path):
    adapter = VSCodeAdapter()
    doc = tmp_path / "code.py"
    doc.write_text("print('hi')", encoding="utf-8")
    result = adapter.read_file(path=str(doc))
    assert result["success"] is True
    assert "print" in result["text"]


def test_whatsapp_capabilities():
    adapter = WhatsAppAdapter()
    assert adapter.supports("read_messages")
    assert adapter.supports("send_message")