"""
browser_server.py -- FastAPI WebSocket bridge between Nova and the Edge extension.

Why this file exists:
Nova (Python) needs a communication channel to the Edge extension. The extension
cannot expose an HTTP server, so it connects to us via WebSocket instead. This
server acts as the bidirectional bridge:

  Nova (Python)  <--HTTP / WS-events-->  browser_server  <--WebSocket-->  Edge Extension

Nova sends commands as HTTP POST requests to /api/send. The server forwards them
to the extension over WebSocket, waits for the response, and returns it to Nova.
Push events (e.g. download notifications) from the extension are broadcast to
Nova's event WebSocket (WS /ws/events).

Usage:
  main.py starts the bridge automatically in-process (start_bridge/stop_bridge
  below). No separate terminal is required -- just `python main.py`.
  For standalone debugging:
    python -m backend.browser_server
  (Starts on 127.0.0.1:8742 by default)

Endpoints:
  GET  /health        - Server status and extension connection state
  POST /api/send      - Send a message to the extension and wait for response
  WS   /ws            - WebSocket endpoint for the extension to connect
  WS   /ws/events     - WebSocket endpoint for Nova to receive push events
"""

import asyncio
import logging
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
import uvicorn

logger = logging.getLogger("nova.browser_server")

app = FastAPI(title="Nova Browser Bridge Server")

# -- Active WebSocket connection from the extension --
_extension_ws: Optional[WebSocket] = None

# -- Extension code hot-reload --
# The extension reports its code version on every heartbeat ping. When the
# loaded version is older than EXPECTED_EXTENSION_CODE_VERSION, the server
# orders a self-reload (extension sends `extension_reload` -> the extension
# calls chrome.runtime.reload() and picks up the new code from disk). Bump
# this together with EXT_CODE_VERSION in EdgeExtension/background.js.
EXPECTED_EXTENSION_CODE_VERSION = "10"
_extension_reload_sent = False  # once per connection, so a stale build
                                # cannot ping-pong reload forever

# -- Event listeners (Nova connects here to receive push events) --
_event_listeners: list[WebSocket] = []

# -- Pending requests awaiting response from the extension --
_pending_requests: dict[str, dict] = {}

# -- Keepalive task --
_keepalive_task: Optional[asyncio.Task] = None


# -- Message models --

class OutgoingMessage(BaseModel):
    """Message sent FROM Nova TO the extension."""
    type: str
    messageId: str = ""
    payload: dict = {}


class IncomingMessage(BaseModel):
    """Message received FROM the extension."""
    type: str
    messageId: str
    source: str
    timestamp: str
    inResponseTo: Optional[str] = None
    payload: dict = {}


# -- WebSocket endpoint for the extension --

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for the Edge extension to connect."""
    global _extension_ws, _keepalive_task, _extension_reload_sent

    # Accept the connection
    await websocket.accept()

    # If a previous extension was connected, replace it cleanly
    if _extension_ws is not None:
        logger.debug("Replacing previous extension connection")
        try:
            old_ws = _extension_ws
            _extension_ws = None
            await old_ws.close(code=1001, reason="New connection replacing old")
        except Exception:
            pass

    _extension_ws = websocket
    logger.info("Extension connected")

    _extension_reload_sent = False

    # Start a server-side keepalive task that sends periodic pings
    # as a backup in case the extension heartbeat stalls.
    if _keepalive_task is None or _keepalive_task.done():
        _keepalive_task = asyncio.create_task(_server_keepalive(), name="server-keepalive")

    # Start stale request cleanup task
    if not hasattr(websocket_endpoint, "_cleanup_task") or websocket_endpoint._cleanup_task.done():
        websocket_endpoint._cleanup_task = asyncio.create_task(
            _stale_requests_cleanup(), name="stale-requests-cleanup"
        )

    receive_timeout = 45.0  # seconds without any message triggers disconnect detection

    try:
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=receive_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"No message from extension for {receive_timeout}s — "
                    "connection may be dead"
                )
                # Try sending a ping to see if the connection is alive
                try:
                    await websocket.send_json({
                        "type": "ping",
                        "messageId": f"svr-ping-{uuid.uuid4().hex[:8]}",
                        "source": "server",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "inResponseTo": None,
                        "payload": {"server_keepalive": True},
                    })
                    logger.debug("Server keepalive ping sent")
                    # Continue the loop — wait for pong or next message
                    continue
                except Exception as e:
                    logger.error(f"Keepalive ping failed — connection is dead: {e}")
                    raise WebSocketDisconnect(code=1006, reason=f"Keepalive failed: {e}")

            # Got a message — process it
            logger.debug(f"Received message from extension: type={data.get('type')} "
                         f"id={data.get('messageId', '?')}")

            msg = IncomingMessage(**data)

            # Handle PING from extension (heartbeat).
            # Respond with PONG immediately — don't route through _handle_extension_message.
            if msg.type == "ping":
                pong = {
                    "type": "pong",
                    "messageId": f"srv-{uuid.uuid4().hex[:12]}",
                    "source": "server",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "inResponseTo": msg.messageId,
                    "payload": {"echo": msg.payload},
                }
                await websocket.send_json(pong)
                logger.debug(f"Responded to extension heartbeat (id={msg.messageId})")

                # Extension code hot-reload check: the heartbeat carries the
                # loaded code version; if it is older than what we expect,
                # order a self-reload so edits take effect without a manual
                # edge://extensions reload. Sent at most once per connection
                # (a still-stale build then only logs, never loops), and
                # deferred while requests are in flight so nothing breaks.
                code_version = (msg.payload or {}).get("codeVersion")
                if (
                    code_version is not None
                    and str(code_version) != EXPECTED_EXTENSION_CODE_VERSION
                ):
                    if _extension_reload_sent:
                        logger.warning(
                            "[Extension] code version %r is still outdated (expected %s) "
                            "-- manual reload required",
                            code_version, EXPECTED_EXTENSION_CODE_VERSION,
                        )
                    elif _pending_requests:
                        logger.info(
                            "[Extension] code version %r outdated, but %d request(s) in "
                            "flight -- deferring reload",
                            code_version, len(_pending_requests),
                        )
                    else:
                        logger.warning(
                            "[Extension] code version %r is outdated (expected %s) "
                            "-- sending extension_reload",
                            code_version, EXPECTED_EXTENSION_CODE_VERSION,
                        )
                        _extension_reload_sent = True
                        await websocket.send_json({
                            "type": "extension_reload",
                            "messageId": f"srv-reload-{uuid.uuid4().hex[:8]}",
                            "source": "server",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "inResponseTo": None,
                            "payload": {
                                "reason": (
                                    f"extension code is version {code_version}, "
                                    f"Nova expects {EXPECTED_EXTENSION_CODE_VERSION}"
                                ),
                                "expected": EXPECTED_EXTENSION_CODE_VERSION,
                                "loaded": str(code_version),
                            },
                        })
                continue

            # Everything else (including PONG responses) goes through normal routing.
            # _handle_extension_message will resolve pending requests by matching
            # inResponseTo, log unmatched PONGs, and broadcast push events.
            await _handle_extension_message(msg)

    except WebSocketDisconnect as wsd:
        if wsd.code in (1000, 1001):
            logger.info(f"Extension disconnected (code={wsd.code})")
        else:
            logger.warning(f"Extension disconnected: code={wsd.code} reason=\"{wsd.reason}\"")
    except Exception as e:
        logger.error(f"WebSocket receive loop EXCEPTION: {type(e).__name__}: {e}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
    finally:
        # Cancel the keepalive task
        if _keepalive_task and not _keepalive_task.done():
            _keepalive_task.cancel()
            _keepalive_task = None

        if _extension_ws is websocket:
            _extension_ws = None

        # Fail all pending requests
        pending_count = len(_pending_requests)
        if pending_count > 0:
            logger.warning(f"Failing {pending_count} pending request(s) due to disconnect")
        for req_id, pending in list(_pending_requests.items()):
            pending["response"] = {
                "type": "error",
                "payload": {
                    "error": "Extension disconnected",
                    "success": False,
                },
            }
            pending["event"].set()
        _pending_requests.clear()


async def _server_keepalive():
    """Background task: send a server-initiated ping every 30 seconds
    to detect silent disconnections and keep the channel alive."""
    while True:
        try:
            await asyncio.sleep(30.0)
            ws = _extension_ws
            if ws is None:
                continue
            try:
                ping_msg = {
                    "type": "ping",
                    "messageId": f"svr-ping-{uuid.uuid4().hex[:8]}",
                    "source": "server",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "inResponseTo": None,
                    "payload": {"server_keepalive": True},
                }
                await ws.send_json(ping_msg)
                logger.debug("Server keepalive ping sent (background task)")
            except Exception as e:
                logger.warning(f"Server keepalive send failed: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Server keepalive task error: {e}")


async def _stale_requests_cleanup():
    """Background task: periodically purge pending requests that have been
    waiting longer than the timeout, preventing memory leaks from orphaned
    requests (e.g. extension received command but crashed before responding)."""
    CLEANUP_INTERVAL = 15.0
    MAX_WAIT = 60.0  # well above the 30s HTTP timeout
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            now = asyncio.get_event_loop().time()
            stale = []
            for req_id, pending in list(_pending_requests.items()):
                created = pending.get("created_at", 0)
                if now - created > MAX_WAIT:
                    stale.append(req_id)
            for req_id in stale:
                pending = _pending_requests.pop(req_id, None)
                if pending:
                    pending["response"] = {
                        "type": "error",
                        "payload": {"error": "Request timed out", "success": False},
                    }
                    pending["event"].set()
                    logger.warning(f"Cleaned up stale pending request: {req_id}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Stale request cleanup error: {e}")


# -- WebSocket endpoint for Nova event listeners --

@app.websocket("/ws/events")
async def events_endpoint(websocket: WebSocket):
    """WebSocket endpoint for Nova to receive push events from the extension."""
    await websocket.accept()
    _event_listeners.append(websocket)
    logger.info(f"Nova event listener connected ({len(_event_listeners)} total)")

    try:
        while True:
            # Keep connection alive; listen for disconnect
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"Event WebSocket error: {e}")
                break
    finally:
        try:
            _event_listeners.remove(websocket)
        except ValueError:
            pass
        logger.info(f"Nova event listener disconnected ({len(_event_listeners)} remaining)")


async def _broadcast_event(msg: IncomingMessage):
    """Forward an unsolicited extension message to all connected Nova event listeners."""
    if not _event_listeners:
        logger.debug(f"No event listeners, dropping event: {msg.type}")
        return

    dead: list[WebSocket] = []
    for ws in _event_listeners:
        try:
            await ws.send_json(msg.model_dump())
        except Exception as exc:
            logger.warning(f"Failed to send event to listener: {exc}")
            dead.append(ws)

    for ws in dead:
        try:
            _event_listeners.remove(ws)
        except ValueError:
            pass


async def _handle_extension_message(msg: IncomingMessage):
    """
    Route a message from the extension.

    If the message is a response to a pending request (matched by inResponseTo),
    resolve the pending request so the HTTP caller gets the response.

    Otherwise, broadcast it as a push event to all Nova event listeners.
    """
    if msg.inResponseTo and msg.inResponseTo in _pending_requests:
        pending = _pending_requests[msg.inResponseTo]
        pending["response"] = msg.model_dump()
        pending["event"].set()
        logger.debug(f"Resolved pending request {msg.inResponseTo}")
    elif msg.type == "pong":
        # PONG with no matching pending request — likely a response to our
        # server keepalive. No action needed.
        logger.debug(f"Unmatched PONG (inResponseTo={msg.inResponseTo})")
    else:
        logger.info(f"Push event from extension: type={msg.type} (no matching pending request)")
        await _broadcast_event(msg)


# -- HTTP endpoint for Nova --

@app.post("/api/send")
async def send_to_extension(message: OutgoingMessage):
    """
    Send a message to the extension and wait for its response.

    Nova calls this endpoint with a command message. The server forwards it
    to the extension via WebSocket, blocks until the extension responds,
    and returns the response to Nova.

    Returns the extension's response message as JSON.
    Raises 502 if the extension is not connected.
    Raises 504 if the extension doesn't respond within 30 seconds.
    """
    if not _extension_ws:
        raise HTTPException(
            status_code=502,
            detail="Extension not connected. Ensure Edge is running with the extension loaded."
        )

    if not message.messageId:
        message.messageId = str(uuid.uuid4())

    event = asyncio.Event()
    _pending_requests[message.messageId] = {
        "event": event, "response": None, "created_at": asyncio.get_event_loop().time(),
    }

    try:
        await _extension_ws.send_json(message.model_dump())
        logger.debug(f"Sent {message.type} to extension (id={message.messageId})")

        try:
            await asyncio.wait_for(event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="Extension did not respond within 30 seconds"
            )

        pending = _pending_requests.pop(message.messageId)
        return pending["response"]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in send_to_extension: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _pending_requests.pop(message.messageId, None)


# -- Health check --

@app.get("/health")
async def health():
    """Health check endpoint. Returns server status and whether the extension is connected."""
    return {
        "status": "ok",
        "extension_connected": _extension_ws is not None,
        "event_listeners": len(_event_listeners),
        "pending_requests": len(_pending_requests),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# -- Entry point --

def run_server(host: str = "127.0.0.1", port: int = 8742):
    """Start the browser bridge server. Call this from main.py or run standalone."""
    uvicorn.run(app, host=host, port=port, log_level="info",
                ws_ping_interval=None,  # Disable uvicorn's built-in ping so our
                ws_ping_timeout=None)   # custom keepalive is the only mechanism


# ---------------------------------------------------------------------------
# Embedded lifecycle (used by main.py so `python main.py` starts the bridge
# automatically -- no separate terminal or `python -m backend.browser_server`
# required).
# ---------------------------------------------------------------------------

_bridge_server: Optional[uvicorn.Server] = None
_bridge_thread: Optional[threading.Thread] = None


def is_bridge_running(host: str = "127.0.0.1", port: int = 8742,
                      timeout: float = 2.0) -> bool:
    """Return True if a Nova browser bridge is already serving on host:port."""
    try:
        import httpx
        resp = httpx.get(f"http://{host}:{port}/health", timeout=timeout)
        if resp.status_code != 200:
            return False
        data = resp.json()
        return isinstance(data, dict) and data.get("status") == "ok"
    except Exception:
        return False


def start_bridge(host: str = "127.0.0.1", port: int = 8742,
                 wait_timeout: float = 10.0) -> bool:
    """Start the browser bridge in a background thread.

    If a bridge is already running on host:port (e.g. an older Nova
    instance, or a manually started `python -m backend.browser_server`),
    it is reused instead of starting a duplicate.

    Returns True if a bridge is available when the call returns.
    """
    global _bridge_server, _bridge_thread

    if is_bridge_running(host, port):
        logger.info("Browser bridge already running on http://%s:%s -- reusing it",
                    host, port)
        return True

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        ws_ping_interval=None,
        ws_ping_timeout=None,
        log_config=None,
    )
    _bridge_server = uvicorn.Server(config)
    _bridge_thread = threading.Thread(
        target=_bridge_server.run,
        daemon=True,
        name="browser-bridge",
    )
    _bridge_thread.start()
    logger.info("Browser bridge starting on http://%s:%s (thread: %s)",
                host, port, _bridge_thread.name)

    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if getattr(_bridge_server, "started", False):
            logger.info("Browser bridge started on http://%s:%s", host, port)
            return True
        if is_bridge_running(host, port):
            logger.info("Browser bridge responding on http://%s:%s", host, port)
            return True
        time.sleep(0.1)

    logger.error(
        "Browser bridge failed to start on http://%s:%s within %.1fs -- "
        "check that port %s is free", host, port, wait_timeout, port,
    )
    return False


def stop_bridge(timeout: float = 5.0) -> None:
    """Gracefully stop the embedded browser bridge.

    Safe to call multiple times. Does nothing if the bridge was not
    started by this process (reused an existing instance), or if it
    was never started.
    """
    global _bridge_server, _bridge_thread

    server, thread = _bridge_server, _bridge_thread
    _bridge_server = None
    _bridge_thread = None

    if server is None:
        return

    server.should_exit = True
    logger.info("Browser bridge shutdown requested")

    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("Browser bridge thread did not stop within %.1fs", timeout)
        else:
            logger.info("Browser bridge stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_server()
