"""Local OAuth authorization helper.

Provides a secure local callback server that receives the provider's
redirect and exchanges the authorization code for tokens. Tokens are
never exposed to the UI or logged; they are stored via
CredentialStorage (Windows DPAPI protected).

Usage for an adapter:

    from connections.oauth import OAuthHelper, OAuthConfig

    oauth = OAuthHelper(OAuthConfig(
        client_id=...,
        client_secret=...,
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=[...],
        redirect_port=8765,
        service_name="google",
    ))
    oauth.ensure_connected()   # returns bool (True if tokens stored)
    oauth.refresh_if_needed()  # called before API use
    oauth.disconnect()         # clears stored tokens
"""

import json
import logging
import secrets
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .storage import credential_storage

logger = logging.getLogger(__name__)

# Long default timeout (OAuth user flow can take minutes).
_AUTH_TIMEOUT_S = 300


@dataclass
class OAuthConfig:
    """Configuration for an OAuth 2.0 authorization-code flow."""

    service_name: str
    client_id: str = ""
    client_secret: str = ""
    auth_url: str = ""
    token_url: str = ""
    scopes: list = field(default_factory=list)
    redirect_port: int = 8765
    extra_auth_params: dict = field(default_factory=dict)
    extra_token_params: dict = field(default_factory=dict)
    token_refresh_url: str = ""
    token_extra_headers: dict = field(default_factory=dict)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Serves the OAuth redirect callback. Captures the code/state query."""

    server: "OAuthCallbackServer"

    def log_message(self, *args):
        # Don't leak the code into logs.
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        error = query.get("error", [None])[0]

        if error:
            self.server.capture_result = {
                "error": error,
                "error_description": query.get("error_description", [None])[0],
            }
            body = b"<h2>Authorization failed.</h2><p>You can close this tab.</p>"
            self.send_response(400)
        else:
            self.server.capture_result = {
                "code": query.get("code", [None])[0],
                "state": query.get("state", [None])[0],
            }
            body = b"<h2>Authorization successful!</h2><p>You can close this tab.</p>"
            self.send_response(200)

        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()


class OAuthCallbackServer(ThreadingHTTPServer):
    """Threaded local HTTP server that captures the OAuth redirect."""

    capture_result: dict | None = None

    def __init__(self, port: int):
        super().__init__(("127.0.0.1", port), _CallbackHandler)
        self.daemon_threads = True
        self.timeout = 0.5


def _post_form(url: str, data: dict, headers: dict | None = None) -> dict:
    """POST application/x-www-form-urlencoded and return parsed JSON."""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=_AUTH_TIMEOUT_S) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"raw": raw.decode("utf-8", errors="replace")}


class OAuthHelper:
    """Drives a standard OAuth 2.0 authorization-code flow.

    - Opens the provider's official authorization page in the default
      browser (via os.startfile).
    - Runs a local HTTPServer to receive the redirect callback.
    - Exchanges the authorization code for access/refresh tokens.
    - Stores tokens encrypted via CredentialStorage.
    - Refreshes tokens on demand.
    """

    def __init__(self, config: OAuthConfig, *, open_browser: Callable | None = None):
        self.config = config
        self._open_browser = open_browser or self._default_open_browser

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def is_connected(self) -> bool:
        creds = self._load_tokens()
        return bool(creds and creds.get("access_token"))

    def get_tokens(self) -> dict | None:
        return self._load_tokens()

    def authorize(self) -> dict:
        """Run the full authorization flow. Returns a dict with
        {'success': bool, 'message': str}."""
        if self.is_connected():
            return {"success": True, "message": f"{self.config.service_name} is already connected."}

        if not self.config.client_id:
            return {
                "success": False,
                "error": "not_configured",
                "message": (
                    f"{self.config.service_name} integration requires OAuth client "
                    "credentials (client_id / client_secret) that aren't configured yet. "
                    "Add them to your configuration to enable authorization."
                ),
            }

        state = secrets.token_urlsafe(16)
        server = OAuthCallbackServer(self.config.redirect_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": f"http://127.0.0.1:{self.config.redirect_port}/",
            "scope": " ".join(self.config.scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        params.update(self.config.extra_auth_params)
        auth_uri = f"{self.config.auth_url}?{urllib.parse.urlencode(params)}"

        logger.info("[OAuth] Opening authorization page for %s", self.config.service_name)
        self._open_browser(auth_uri)

        try:
            self._wait_for_callback(server, thread, state)
            result = server.capture_result
        except TimeoutError as exc:
            server.shutdown()
            return {"success": False, "message": str(exc)}
        finally:
            if thread.is_alive():
                server.shutdown()
            server.server_close()

        if result.get("error"):
            return {
                "success": False,
                "message": (
                    f"Authorization was declined or failed: {result.get('error')} "
                    f"{result.get('error_description') or ''}".strip()
                ),
            }

        code = result.get("code")
        if not code:
            return {"success": False, "message": "No authorization code was returned."}

        token_resp = self._exchange_code(code)
        if "error" in token_resp:
            return {
                "success": False,
                "message": f"Token exchange failed: {token_resp.get('error')} "
                           f"{token_resp.get('error_description') or ''}".strip(),
            }

        token_resp["stored_at"] = time.time()
        self._save_tokens(token_resp)
        return {"success": True, "message": f"{self.config.service_name} connected successfully."}

    def refresh(self) -> dict:
        """Refresh the access token using the stored refresh token."""
        creds = self._load_tokens()
        refresh_token = (creds or {}).get("refresh_token")
        if not refresh_token:
            return {"success": False, "message": "No refresh token stored for this service."}

        data = {
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        data.update(self.config.extra_token_params)
        token_url = self.config.token_refresh_url or self.config.token_url
        resp = _post_form(token_url, data, self.config.token_extra_headers)

        if "error" in resp:
            return {
                "success": False,
                "message": f"Token refresh failed: {resp.get('error')} "
                           f"{resp.get('error_description') or ''}".strip(),
            }

        merged = dict(creds or {})
        merged.update(resp)
        merged["stored_at"] = time.time()
        self._save_tokens(merged)
        return {"success": True, "message": "Token refreshed.", "tokens": merged}

    def refresh_if_needed(self, *, expires_in_buffer_s: int = 60) -> bool:
        """Refresh the access token if it is close to expiring. Returns
        True when a usable access token is available afterwards."""
        creds = self._load_tokens()
        if not creds or not creds.get("access_token"):
            return False
        stored_at = float(creds.get("stored_at") or 0)
        expires_in = float(creds.get("expires_in") or 0)
        if expires_in and (time.time() - stored_at) > (expires_in - expires_in_buffer_s):
            result = self.refresh()
            if not result.get("success"):
                logger.warning(
                    "[OAuth] Refresh failed for %s: %s",
                    self.config.service_name, result.get("message"),
                )
                return False
        return True

    def disconnect(self) -> bool:
        """Remove stored credentials (disconnect/revoke)."""
        return self._delete_tokens()

    def _delete_tokens(self) -> bool:
        return credential_storage.delete_credentials(self.config.service_name)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _default_open_browser(url: str):
        try:
            import os
            os.startfile(url)
        except Exception as exc:
            logger.warning("[OAuth] Could not open browser automatically: %s", exc)

    def _wait_for_callback(self, server: OAuthCallbackServer, thread: threading.Thread, state: str):
        deadline = time.time() + _AUTH_TIMEOUT_S
        while time.time() < deadline:
            if server.capture_result is not None:
                captured_state = server.capture_result.get("state")
                if captured_state is not None and captured_state != state:
                    logger.warning("[OAuth] State mismatch in callback -- ignoring")
                    server.capture_result = None
                    continue
                return
            time.sleep(0.2)
        raise TimeoutError(
            "Authorization timed out. Make sure you completed the sign-in in the browser."
        )

    def _load_tokens(self) -> dict | None:
        return credential_storage.get_credentials(self.config.service_name)

    def _save_tokens(self, tokens: dict) -> bool:
        return credential_storage.save_credentials(self.config.service_name, tokens)

    def _exchange_code(self, code: str) -> dict:
        data = {
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": f"http://127.0.0.1:{self.config.redirect_port}/",
        }
        data.update(self.config.extra_token_params)
        return _post_form(self.config.token_url, data, self.config.token_extra_headers)