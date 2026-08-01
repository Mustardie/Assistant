"""
Test: EdgeExtensionClient retry logic for transient disconnects.
Verifies that send() retries when extension is temporarily disconnected
(MV3 service worker restart scenario).
"""
import logging
import os
import sys
import threading
import time
import json
import asyncio
import uuid

import httpx
import uvicorn
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.browser_server import app
from tools.browser.edge_extension_client import EdgeExtensionClient

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("retry_test")
log.setLevel(logging.DEBUG)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 19001
HTTP_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
WS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"

PASS = 0
FAIL = 0

def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  - {detail}")


async def _mock_reader(ws, stop_event):
    """Read and respond to all commands on a WS connection."""
    while not stop_event.is_set():
        try:
            data = await asyncio.wait_for(ws.recv(), timeout=1.0)
            msg = json.loads(data)
            cmd_type = msg.get("type")
            msg_id = msg.get("messageId")
            if cmd_type == "ping":
                resp = {"type": "pong", "messageId": str(uuid.uuid4()),
                        "source": "extension", "inResponseTo": msg_id,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "payload": {"echo": msg.get("payload", {})}}
                await ws.send(json.dumps(resp))
            elif cmd_type == "pong":
                continue
            else:
                resp = {"type": "action_result", "messageId": str(uuid.uuid4()),
                        "source": "extension", "inResponseTo": msg_id,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "payload": {"success": True}}
                await ws.send(json.dumps(resp))
        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed:
            break


async def test_retry_on_disconnect():
    """
    Test that send() retries when extension is temporarily disconnected.
    
    Sequence:
    1. Connect extension
    2. Close connection (simulate service worker termination)
    3. Start send() in a thread — it should fail require_connected(), retry
    4. While send() is retrying, reconnect extension
    5. Verify send() succeeds after reconnection
    """
    client = EdgeExtensionClient(base_url=HTTP_URL)
    executor = None
    loop = asyncio.get_running_loop()

    # Phase 1: Connect
    async with websockets.connect(WS_URL) as ws:
        stop = asyncio.Event()
        reader = asyncio.create_task(_mock_reader(ws, stop))
        await asyncio.sleep(0.2)

        # Verify connected
        c = client.is_connected()
        check("Initial connection", c)

        # Phase 2: Disconnect
        stop.set()
        reader.cancel()
    await asyncio.sleep(0.3)

    # Verify disconnected
    c = client.is_connected()
    check("Disconnected detected", not c, "expected: extension is disconnected")
    if c:
        return  # cannot test retry if still connected

    # Phase 3: Start send() (will retry) while reconnecting
    import concurrent.futures
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    # Schedule reconnection after a short delay (within retry window)
    async def reconnect_after_delay():
        await asyncio.sleep(1.5)  # enough for 1 retry attempt to fail
        log.info("[RETRY] Reconnecting extension...")
        async with websockets.connect(WS_URL) as ws2:
            stop2 = asyncio.Event()
            reader2 = asyncio.create_task(_mock_reader(ws2, stop2))
            # Wait for the retried command to arrive and be processed
            await asyncio.sleep(3.0)
            stop2.set()
            reader2.cancel()
            log.info("[RETRY] Reconnection complete")

    reconnect_task = asyncio.create_task(reconnect_after_delay())

    # Send command — should retry and succeed after reconnection
    try:
        log.info("[RETRY] Sending command while disconnected (expects retry)...")
        result = await loop.run_in_executor(
            executor, lambda: client.send("tab_list", {})
        )
        log.info(f"[RETRY] Command succeeded after retry: {result.get('success')}")
        check("send() retries and succeeds on transient disconnect", True)
    except Exception as e:
        log.error(f"[RETRY] Command failed: {e}")
        check("send() retries on transient disconnect", False, str(e))

    await reconnect_task
    if executor:
        executor.shutdown(wait=False)


async def test_no_retry_on_permanent_failure():
    """
    Test that send() does NOT retry on permanent failures
    (server unreachable, non-transient errors).
    """
    client = EdgeExtensionClient(base_url="http://127.0.0.1:19999", timeout=2.0)

    import concurrent.futures
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()

    try:
        result = await loop.run_in_executor(
            executor, lambda: client.send("tab_list", {})
        )
        check("No retry on server unreachable", False, "should have raised")
    except RuntimeError as e:
        msg = str(e)
        # Should mention "Server not reachable", not "Extension not connected"
        if "not reachable" in msg:
            check("No retry on server unreachable (raises immediately)", True)
        else:
            check("No retry on server unreachable", False, msg)
    finally:
        executor.shutdown(wait=False)


async def main():
    server_started = threading.Event()

    def start_server():
        config = uvicorn.Config(
            app, host=SERVER_HOST, port=SERVER_PORT,
            log_level="critical", ws_ping_interval=None, ws_ping_timeout=None,
        )
        server = uvicorn.Server(config)
        server_started.set()
        server.run()

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    server_started.wait()
    await asyncio.sleep(0.5)

    print("=== Test: Retry on transient disconnect ===")
    await test_retry_on_disconnect()

    print("\n=== Test: No retry on permanent failure ===")
    await test_no_retry_on_permanent_failure()

    print(f"\n{'='*50}")
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print(f"{'='*50}")

    server_thread.join(timeout=1)


if __name__ == "__main__":
    asyncio.run(main())
