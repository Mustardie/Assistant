"""Transport to the Premiere CEP panel.

Shape deliberately mirrors ``tools/browser/edge_extension_client.py``: Python
is a plain HTTP client, the other side hosts the server. For the browser that
server is the FastAPI bridge; here it is a small Node HTTP server running
inside the CEP panel (CEP panels get a full Node runtime, so this needs no
extra process and no extra port juggling on the user's machine).

Why HTTP and not a socket the panel dials out to: ExtendScript execution is
strictly request/response and single-threaded inside Premiere, and a plain
POST models that exactly. It also means the Python side stays synchronous and
importable from the existing tool registry without dragging asyncio in.

The panel does the ``evalScript`` call; this module only speaks JSON and
converts transport failures into the typed errors in ``premiere.errors``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from premiere.errors import BridgeError, HostError, PremiereError, UnsupportedError

logger = logging.getLogger("nova.premiere.bridge")

DEFAULT_HOST = os.environ.get("PREMIERE_BRIDGE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PREMIERE_BRIDGE_PORT", "8089"))

#: Generous because a single operation can trigger a Premiere re-render (adding
#: an effect to a 4K clip is not instant) and ExtendScript blocks the host
#: while it runs.
DEFAULT_TIMEOUT = 120.0

_RETRY_DELAYS = (0.5, 1.5, 3.0)

_SETUP_HELP = (
    "Premiere bridge unreachable. Check:\n"
    "  1. Adobe Premiere Pro is running with a project open\n"
    "  2. The Nova Premiere Bridge panel is open "
    "(Window > Extensions > Nova Premiere Bridge)\n"
    "  3. The extension is installed: run "
    "`python -m premiere.install` and restart Premiere\n"
    "  4. Unsigned CEP extensions are allowed (the installer sets "
    "PlayerDebugMode; it needs a Premiere restart to take effect)"
)


class PremiereBridge:
    """Synchronous JSON client for the Premiere panel."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        self._client = None
        self._last_health: dict = {}

    # -- lazily built so importing this module never costs an httpx client --
    @property
    def client(self):
        if self._client is None:
            import httpx
            self._client = httpx.Client(
                timeout=self.timeout,
                # The panel is on loopback; a proxy would break it and this
                # environment sets HTTPS_PROXY globally.
                trust_env=False,
            )
        return self._client

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Panel + Premiere status. Never raises -- callers branch on it."""
        try:
            import httpx
        except ImportError:
            return {"connected": False, "error": "httpx is not installed"}
        try:
            response = self.client.get(f"{self.base_url}/health", timeout=5.0)
            data = response.json()
            data["connected"] = True
            self._last_health = data
            return data
        except Exception as exc:  # noqa: BLE001 - health must never propagate
            logger.debug("Premiere bridge health check failed: %s", exc)
            return {"connected": False, "error": str(exc)}

    def is_connected(self) -> bool:
        return bool(self.health().get("connected"))

    def require_connected(self) -> dict:
        status = self.health()
        if not status.get("connected"):
            raise BridgeError(
                "Cannot reach Adobe Premiere Pro",
                hint=_SETUP_HELP,
                detail={"endpoint": self.base_url, "reason": status.get("error")},
            )
        if not status.get("project_open", True):
            raise BridgeError(
                "Premiere is running but no project is open",
                hint="Open or create a project in Premiere, then retry",
            )
        return status

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def call(self, op: str, params: Optional[dict] = None, *,
             timeout: Optional[float] = None) -> Any:
        """Run one host operation and return its payload.

        Raises ``HostError``/``UnsupportedError`` on a Premiere-side failure so
        the engine can classify it; transport problems become ``BridgeError``.
        """
        return self._post("/exec", {"op": op, "params": params or {}},
                          timeout=timeout)

    def call_batch(self, operations: list, *, on_error: str = "abort",
                   timeout: Optional[float] = None) -> Any:
        """Run several operations in one round trip.

        Batching matters more here than in most bridges: each ``evalScript``
        round trip costs a few milliseconds of IPC plus a Premiere UI tick, so
        a 200-keyframe animation sent one call at a time is visibly slow and
        can leave the timeline half-updated if the user closes the panel
        mid-way. One batch is also one Premiere undo group.
        """
        return self._post(
            "/batch",
            {"ops": operations, "on_error": on_error},
            timeout=timeout or max(self.timeout, 30.0 + 0.5 * len(operations)),
        )

    # ------------------------------------------------------------------

    def _post(self, path: str, body: dict, *, timeout: Optional[float] = None) -> Any:
        try:
            import httpx
        except ImportError as exc:
            raise BridgeError(
                "The 'httpx' package is required to talk to Premiere",
                hint="pip install httpx",
                detail={"reason": str(exc)},
            ) from None

        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                response = self.client.post(
                    url, json=body, timeout=timeout or self.timeout
                )
                response.raise_for_status()
                return self._unwrap(response.json())
            except httpx.HTTPStatusError as exc:
                # A 4xx/5xx from the panel carries a structured error body; it
                # is a real answer, not a transport hiccup, so do not retry.
                raise self._from_status(exc) from None
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError) as exc:
                last_error = exc
                if attempt < len(_RETRY_DELAYS):
                    delay = _RETRY_DELAYS[attempt]
                    logger.debug(
                        "Premiere bridge connect failed (%s); retrying in %.1fs",
                        exc, delay,
                    )
                    time.sleep(delay)
                    continue
                raise BridgeError(
                    "Cannot reach Adobe Premiere Pro",
                    hint=_SETUP_HELP,
                    detail={"endpoint": url, "reason": str(exc)},
                ) from None
            except httpx.ReadTimeout as exc:
                raise BridgeError(
                    f"Premiere did not respond within {timeout or self.timeout:.0f}s",
                    hint="A long operation may still be running inside Premiere. "
                         "Check the timeline before retrying -- retrying a "
                         "mutating batch can double-apply it.",
                    detail={"endpoint": url},
                ) from exc
            except json.JSONDecodeError as exc:
                raise BridgeError(
                    "Premiere panel returned a malformed response",
                    detail={"reason": str(exc)},
                ) from None

        raise BridgeError("Premiere bridge failed", detail={"reason": str(last_error)})

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """Turn the panel's ``{ok, result|error}`` envelope into a value."""
        if not isinstance(payload, dict):
            return payload
        if payload.get("ok"):
            return payload.get("result")

        error = payload.get("error") or {}
        if isinstance(error, str):
            error = {"message": error}
        message = error.get("message") or "Premiere reported an unspecified failure"
        code = error.get("code", "")

        if code == "unsupported":
            raise UnsupportedError(
                message,
                alternative=error.get("alternative", ""),
                detail=error.get("detail"),
            )
        raise HostError(
            message,
            hint=error.get("hint", ""),
            detail=error.get("detail"),
            path=error.get("at", ""),
        )

    @staticmethod
    def _from_status(exc) -> PremiereError:
        try:
            body = exc.response.json()
        except Exception:  # noqa: BLE001
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return HostError(
                error["message"],
                hint=error.get("hint", ""),
                detail=error.get("detail"),
            )
        return BridgeError(
            f"Premiere panel returned HTTP {exc.response.status_code}",
            detail={"body": (exc.response.text or "")[:400]},
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


#: Module-level default, matching how ``tools/browser`` exposes its client.
bridge = PremiereBridge()
