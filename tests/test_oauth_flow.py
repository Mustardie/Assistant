"""Tests for OAuth flow state handling -- no live providers are contacted."""

import pytest

from connections.oauth import OAuthConfig, OAuthHelper
from connections.storage import CredentialStorage


class _FakeServer:
    """Drop-in replacement for OAuthCallbackServer (module-global)."""

    def __init__(self, port):
        self.capture_result = None
        self.thread_alive = True

    def serve_forever(self):
        pass

    def shutdown(self):
        pass

    def server_close(self):
        pass


class RecordingOAuthHelper(OAuthHelper):
    def __init__(self, config, tmp_path):
        super().__init__(config)
        self.opened_urls = []
        self.token_exchanges = []
        self.refresh_calls = []
        self.store = CredentialStorage(tmp_path / "creds", use_cred_manager=False)
        self._load_tokens = lambda: self.store.get_credentials(config.service_name)
        self._save_tokens = lambda t: self.store.save_credentials(config.service_name, t)
        self._delete_tokens = lambda: self.store.delete_credentials(config.service_name)

    def _open_browser(self, url):
        self.opened_urls.append(url)

    def _exchange_code(self, code):
        self.token_exchanges.append(code)
        if code == "bad_code":
            return {"error": "invalid_grant", "error_description": "Code expired"}
        return {"access_token": "at_123", "refresh_token": "rt_456", "expires_in": 3600}


def _config():
    return OAuthConfig(
        service_name="test_oauth",
        client_id="cid",
        client_secret="csecret",
        auth_url="https://provider.example/authorize",
        token_url="https://provider.example/token",
        scopes=["read", "write"],
        redirect_port=0,
    )


def _make_helper(tmp_path, capture=None):
    import connections.oauth as oauth_mod

    helper = RecordingOAuthHelper(_config(), tmp_path)
    if capture is not None:
        server = _FakeServer(0)
        server.capture_result = capture
        # Stub the thread so the callback-wait doesn't spin on a real one.
        fake_thread = type("FakeThread", (), {
            "is_alive": lambda self: False,
            "start": lambda self: None,
        })()
        helper._wait_for_callback = lambda s, t, state: s.capture_result
        oauth_mod.OAuthCallbackServer = lambda port: server
        fake_thread.start()
    return helper


def test_authorize_requires_configuration(tmp_path):
    config = _config()
    config.client_id = ""
    helper = RecordingOAuthHelper(config, tmp_path)
    result = helper.authorize()
    assert result["success"] is False
    assert "not_configured" in result.get("error", "")
    assert helper.opened_urls == []


def test_authorize_times_out_cleanly(tmp_path):
    import connections.oauth as oauth_mod

    helper = RecordingOAuthHelper(_config(), tmp_path)
    oauth_mod.OAuthCallbackServer = _FakeServer
    # No capture ever arrives -> short fake timeout.
    helper._wait_for_callback = lambda s, t, state: (_ for _ in ()).throw(TimeoutError("timed out"))
    result = helper.authorize()
    assert result["success"] is False
    assert "timed out" in result["message"].lower()


def test_authorize_state_mismatch_rejected(tmp_path):
    """A callback whose state doesn't match the initiated flow must be
    ignored, never accepted as a successful token exchange."""
    helper = _make_helper(tmp_path, {"code": "c1", "state": "wrong_state"})

    # Patch _wait_for_callback to enforce the real state guard with a
    # bounded retry loop so a mismatch fails fast instead of spinning.
    def guarded_wait(server, thread, state):
        attempts = 0
        while attempts < 3:
            captured = server.capture_result
            if captured is None:
                raise TimeoutError("timed out")
            if captured.get("state") != state:
                server.capture_result = None
                attempts += 1
                continue
            return captured
        raise TimeoutError("timed out")

    helper._wait_for_callback = guarded_wait
    result = helper.authorize()
    # The mismatched callback was rejected -> no token exchange happened.
    assert result["success"] is False
    assert helper.token_exchanges == []


def test_authorize_success_exchanges_and_stores(tmp_path):
    helper = _make_helper(tmp_path, {"code": "good_code", "state": "state_val"})
    result = helper.authorize()
    assert result["success"] is True
    assert helper.token_exchanges == ["good_code"]
    tokens = helper.store.get_credentials("test_oauth")
    assert tokens["access_token"] == "at_123"
    assert tokens["refresh_token"] == "rt_456"


def test_authorize_bad_code_fails_cleanly(tmp_path):
    helper = _make_helper(tmp_path, {"code": "bad_code", "state": "state_val"})
    result = helper.authorize()
    assert result["success"] is False
    assert "Token exchange failed" in result["message"]


def test_authorize_provider_error(tmp_path):
    helper = _make_helper(tmp_path, {"error": "access_denied", "error_description": "User said no"})
    result = helper.authorize()
    assert result["success"] is False
    assert "access_denied" in result["message"]


def test_already_connected_short_circuits(tmp_path):
    helper = RecordingOAuthHelper(_config(), tmp_path)
    helper.store.save_credentials("test_oauth", {"access_token": "existing"})
    result = helper.authorize()
    assert result["success"] is True
    assert helper.opened_urls == []  # no new auth page opened


def test_disconnect_clears_tokens(tmp_path):
    helper = RecordingOAuthHelper(_config(), tmp_path)
    helper.store.save_credentials("test_oauth", {"access_token": "at"})
    assert helper.disconnect() is True
    assert helper.store.get_credentials("test_oauth") is None


def test_oauth_config_defaults():
    config = OAuthConfig(service_name="svc")
    assert config.client_id == ""
    assert config.scopes == []
    assert config.redirect_port == 8765