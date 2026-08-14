"""Small HTTP/JSON client + shared base for REST API adapters.

Uses only the standard library (urllib) so every adapter stays
dependency-free. Adapters that need real service calls use `ApiClient`;
unit tests inject a fake `client` so no network is ever touched.

Every network error is converted into a graceful {"success": False,
"error": ...} dict -- adapters never raise to the tool layer.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from adapters.base import BaseAdapter, ApiKeyAdapter

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Raised when an API call returns a non-2xx response."""

    def __init__(self, status: int, body: str, url: str = ""):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status}: {body[:200]}")


class ApiClient:
    """Minimal JSON-over-HTTP client. `base_url` may end with '/'; paths
    are joined without duplicating slashes."""

    def __init__(self, base_url: str = "", *, token: str = "",
                 token_header: str = "Authorization",
                 token_prefix: str = "Bearer ",
                 default_timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.token_header = token_header
        self.token_prefix = token_prefix
        self.timeout = default_timeout

    # ------------------------------------------------------------------ #
    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None, data: bytes | None = None,
                headers: dict | None = None) -> dict:
        url = f"{self.base_url}{path}" if self.base_url else path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, method=method.upper())
        if self.token:
            req.add_header(self.token_header, f"{self.token_prefix}{self.token}")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        body = data
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, body, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ApiError(exc.code, detail, url) from exc
        except urllib.error.URLError as exc:
            raise ApiError(0, str(exc.reason), url) from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}

    def get(self, path, **kwargs) -> dict:
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs) -> dict:
        return self.request("POST", path, **kwargs)

    def patch(self, path, **kwargs) -> dict:
        return self.request("PATCH", path, **kwargs)

    def put(self, path, **kwargs) -> dict:
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs) -> dict:
        return self.request("DELETE", path, **kwargs)


def _as_iso(dt: str | None) -> str:
    """Normalize a datetime-ish string to ISO; pass None through."""
    if not dt:
        return ""
    return str(dt).strip()


class RESTAdapter(ApiKeyAdapter):
    """Base for token-based adapters. Concrete services set:
        api_base_url     -- endpoint root
        config_key       -- settings field holding the key/token
        token_header     -- how to send the token
        token_prefix     -- e.g. 'Bearer ' or 'Bot '
    """

    api_base_url: str = ""
    token_header: str = "Authorization"
    token_prefix: str = "Bearer "

    def __init__(self):
        super().__init__()
        self._client: ApiClient | None = None

    # ------------------------------------------------------------------ #
    def _get_token(self) -> str:
        return self._api_key()

    def _http(self) -> ApiClient:
        """Return a configured client (builds it lazily). Tests may swap
        self._client for a fake before calling."""
        if self._client is None:
            self._client = ApiClient(
                self.api_base_url,
                token=self._get_token(),
                token_header=self.token_header,
                token_prefix=self.token_prefix,
            )
        return self._client

    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        if not self.is_configured():
            return {"status": "not_configured",
                    "message": f"{self.display_name} needs an API key/token to connect."}
        return {"status": "requires_auth",
                "message": f"{self.display_name} has a token configured. Click Connect to verify it."}

    def connect(self) -> dict:
        if not self.is_configured():
            return {"status": "not_configured",
                    "success": False,
                    "error": f"{self.display_name} needs an API key/token. Add one in Settings."}
        # Verifies connectivity; a real 401/403 means the token is wrong.
        try:
            self._verify_connection()
            return self._ok(message=f"{self.display_name} connected.")
        except ApiError as exc:
            if exc.status in (401, 403):
                return self._fail(
                    f"{self.display_name} rejected the token (HTTP {exc.status}). "
                    "Double-check the API key in Settings."
                )
            return self._fail(f"{self.display_name} is unreachable: {exc}")

    def disconnect(self) -> dict:
        # REST adapters don't hold tokens locally; disconnect = forget key.
        return {"success": True, "message": f"{self.display_name} disconnected."}

    def _verify_connection(self):
        """Subclasses override to hit a cheap 'whoami' endpoint."""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _guarded(self, method_name: str, *args, **kwargs) -> dict:
        """Wrap a capability method: catch everything, return graceful
        dicts. Also maps not_configured -> clear message."""
        if not self.is_configured():
            return self._fail(
                f"{self.display_name} isn't configured (no API token). Add it in Settings."
            )
        try:
            return getattr(self, method_name)(*args, **kwargs)
        except ApiError as exc:
            logger.warning("[%s] %s -> HTTP %s", self.name, method_name, exc.status)
            return self._fail(f"{self.display_name} API error (HTTP {exc.status}).")
        except Exception as exc:
            logger.warning("[%s] %s failed: %s", self.name, method_name, exc)
            return self._fail(f"{self.display_name} failed: {exc}")


class OAuthRESTAdapter(BaseAdapter):
    """Base for OAuth-based adapters (Google, Microsoft, Spotify). Holds an
    OAuthHelper; status/connect reflect whether tokens are stored."""

    authentication = "oauth"

    def __init__(self):
        super().__init__()
        self._client: ApiClient | None = None
        self._oauth = self._build_oauth()

    # ------------------------------------------------------------------ #
    def _build_oauth(self):
        """Subclasses return a configured OAuthHelper. Imported lazily to
        keep dependencies out of import time."""
        raise NotImplementedError

    @property
    def oauth(self):
        return self._oauth

    def _tokens(self) -> dict | None:
        try:
            return self._oauth.get_tokens()
        except Exception:
            return None

    def _access_token(self) -> str:
        tokens = self._tokens() or {}
        return str(tokens.get("access_token") or "")

    def _http(self) -> ApiClient:
        if self._client is None:
            self._client = ApiClient(token=self._access_token())
        # Keep the token fresh in case it was refreshed since last use.
        self._client.token = self._access_token()
        return self._client

    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        if not self.oauth.is_connected():
            return {"status": "requires_auth",
                    "message": f"{self.display_name} needs authorization to connect."}
        return {"status": "connected",
                "message": f"{self.display_name} is connected."}

    def connect(self) -> dict:
        result = self.oauth.authorize()
        return result

    def disconnect(self) -> dict:
        revoked = self.oauth.disconnect()
        return {"success": bool(revoked),
                "message": f"{self.display_name} disconnected."}

    # ------------------------------------------------------------------ #
    def _guarded(self, method_name: str, *args, **kwargs) -> dict:
        if not self._access_token():
            return self._fail(
                f"{self.display_name} isn't authorized. Open Connections and connect it."
            )
        try:
            return getattr(self, method_name)(*args, **kwargs)
        except ApiError as exc:
            if exc.status in (401, 403):
                return self._fail(f"{self.display_name} authorization expired or invalid.")
            logger.warning("[%s] %s -> HTTP %s", self.name, method_name, exc.status)
            return self._fail(f"{self.display_name} API error (HTTP {exc.status}).")
        except Exception as exc:
            logger.warning("[%s] %s failed: %s", self.name, method_name, exc)
            return self._fail(f"{self.display_name} failed: {exc}")