"""
edge_extension_client.py

HTTP client for the Nova Browser Bridge Edge Extension.
Why this exists: Nova (Python) communicates with the Edge extension via a
FastAPI bridge server. This client handles the HTTP/JSON protocol details
so callers only need to specify the command type and payload.

All commands return the extension's response as a dict.
If the extension is not connected, methods raise RuntimeError.
"""

import logging
import time
import uuid

import httpx

logger = logging.getLogger("nova.browser.extension")

SERVER_URL = "http://127.0.0.1:8742"
REQUEST_TIMEOUT = 30.0

# Retry settings for transient "Extension not connected" errors.
# These happen when the extension's MV3 service worker is terminated
# by the browser between commands. The client waits for the extension
# to reconnect and retries the command.
_MAX_RETRIES = 3
_RETRY_DELAYS = [1.0, 2.0, 4.0]  # exponential backoff: 1s, 2s, 4s = 7s total


class EdgeExtensionClient:
    """HTTP client for the Nova Browser Bridge FastAPI server."""

    def __init__(self, base_url: str = SERVER_URL, timeout: float = REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http = httpx.Client(timeout=httpx.Timeout(timeout), verify=False)

    # ------------------------------------------------------------------
    # Connection check
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Check server status. Returns the raw health response."""
        try:
            resp = self._http.get(f"{self.base_url}/health", timeout=5)
            return resp.json()
        except httpx.ConnectError:
            logger.debug("Health check failed: server unreachable")
            return {"status": "error", "extension_connected": False, "error": "Server unreachable"}
        except httpx.TimeoutException:
            logger.debug("Health check failed: timeout")
            return {"status": "error", "extension_connected": False, "error": "Server timeout"}
        except Exception as exc:
            logger.debug(f"Health check failed: {exc}")
            return {"status": "error", "extension_connected": False, "error": str(exc)}

    def is_connected(self) -> bool:
        """Return True if the extension is connected to the server."""
        return self.health().get("extension_connected", False)

    def require_connected(self):
        """Raise RuntimeError if the extension is not connected."""
        status = self.health()
        if not status.get("extension_connected"):
            logger.debug(
                "require_connected FAILED: ext_connected=%s status=%s error=%s",
                status.get("extension_connected"), status.get("status"),
                status.get("error", "none"),
            )
            err_type = status.get("error", "")
            if "unreachable" in err_type:
                detail = "Server not reachable"
            elif "timeout" in err_type:
                detail = "Server not responding"
            else:
                detail = "Extension not connected"
            raise RuntimeError(
                f"{detail}. Ensure:\n"
                f"  1. Nova is running: python main.py (starts the bridge automatically)\n"
                f"  2. The Nova Browser Bridge extension is loaded in Edge\n"
                f"  3. Edge is running with the extension active\n"
                f"  Server: {self.base_url}  health={status.get('status')}"
            )

    # ------------------------------------------------------------------
    # Send a command and return the parsed response
    # ------------------------------------------------------------------

    def send(self, command_type: str, payload: dict = None) -> dict:
        """
        Send a command to the extension and wait for the response.
        Returns the *payload* dict from the response message.

        Automatically retries up to 3 times with exponential backoff
        when the extension is temporarily disconnected (e.g. MV3 service
        worker restart). Raises RuntimeError on persistent failure.
        """
        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                self.require_connected()
            except RuntimeError as exc:
                detail = str(exc)
                # Only retry transient "Extension not connected" errors
                if "Extension not connected" not in detail:
                    raise
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_DELAYS[attempt]
                    logger.info(
                        "[Browser] Extension disconnected, retrying %s in %.0fs "
                        "(attempt %d/%d)", command_type, delay,
                        attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    last_error = exc
                    continue
                logger.error(
                    "[Browser] Extension still disconnected after %d retries: %s",
                    _MAX_RETRIES, command_type,
                )
                raise

            # Connected — send the command
            return self._send_inner(command_type, payload)

        # Should not reach here, but just in case:
        raise RuntimeError(
            f"Extension not connected after {_MAX_RETRIES} retries"
        ) from last_error

    def _send_inner(self, command_type: str, payload: dict = None) -> dict:
        """Send a command over HTTP and handle the response (no connection check)."""
        message_id = str(uuid.uuid4())
        body = {
            "type": command_type,
            "messageId": message_id,
            "payload": payload or {},
        }

        start = time.perf_counter()
        category = "Extension"

        try:
            resp = self._http.post(
                f"{self.base_url}/api/send",
                json=body,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            result = resp.json()

            latency_ms = int((time.perf_counter() - start) * 1000)
            payload_out = result.get("payload", result)
            success = payload_out.get("success", True) if isinstance(payload_out, dict) else True

            log_line = (
                f"[Browser] Command: {command_type} | "
                f"Request ID: {message_id} | "
                f"Latency: {latency_ms} ms | "
                f"Result: {'Success' if success else 'Failure'}"
            )
            if success:
                logger.info(log_line)
            else:
                err = payload_out.get("error", "unknown")
                logger.warning(f"{log_line} | Error: {err}")

            return payload_out

        except httpx.TimeoutException:
            category = "Network"
            latency_ms = int((time.perf_counter() - start) * 1000)
            logger.error(
                f"[Browser] Command: {command_type} | "
                f"Request ID: {message_id} | "
                f"Latency: {latency_ms} ms | "
                f"Result: Timeout ({category})"
            )
            raise RuntimeError(
                f"Extension did not respond to '{command_type}' within {self.timeout}s"
            )

        except httpx.HTTPStatusError as exc:
            category = "Backend"
            detail = self._extract_error(exc)
            latency_ms = int((time.perf_counter() - start) * 1000)
            logger.error(
                f"[Browser] Command: {command_type} | "
                f"Request ID: {message_id} | "
                f"Latency: {latency_ms} ms | "
                f"Result: HTTP {exc.response.status_code} ({category}) | "
                f"Detail: {detail}"
            )
            raise RuntimeError(f"Extension command '{command_type}' failed: {detail}")

        except httpx.RequestError as exc:
            category = "Network"
            latency_ms = int((time.perf_counter() - start) * 1000)
            logger.error(
                f"[Browser] Command: {command_type} | "
                f"Request ID: {message_id} | "
                f"Latency: {latency_ms} ms | "
                f"Result: Connection Error ({category}) | "
                f"Detail: {exc}"
            )
            raise RuntimeError(
                f"Cannot reach bridge server at {self.base_url}: {exc}"
            )

    def _extract_error(self, exc: httpx.HTTPStatusError) -> str:
        try:
            return exc.response.json().get("detail", str(exc))
        except Exception:
            return str(exc)

    # ------------------------------------------------------------------
    # Convenience: send with structured result handling
    # ------------------------------------------------------------------

    def send_and_check(self, command_type: str, payload: dict = None) -> dict:
        """
        Send a command, check success, return the payload.
        Raises RuntimeError if the extension reports failure.
        """
        result = self.send(command_type, payload)
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(
                f"Command '{command_type}' failed: {result.get('error', 'unknown error')}"
            )
        return result
