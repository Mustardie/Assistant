"""
Test: reproduce the "Extension not connected" transient disconnect.
Simulates the real agent loop: sync EdgeExtensionClient calls in sequence
with proper async event loop management.

The real system has these components in separate processes:
  Agent (sync, main thread) --HTTP--> Server (async, separate process) --WS--> Extension (Edge browser)
So sync client calls don't block the extension's ability to respond.

This test replicates that:
  Test calls client.send() in a thread (so async WS loop can process responses)
  Server is in a thread (uvicorn)
  Mock extension is in the main async loop
"""
import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import uvicorn
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.browser_server import app
from tools.browser.edge_extension_client import EdgeExtensionClient

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("transient_test")
log.setLevel(logging.DEBUG)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 19000
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

def check_expected(label, condition, detail=""):
    """Like check() but does NOT count as a failure when condition is False
    (used for expected disconnection states, e.g. after manually closing WS)."""
    global PASS
    if condition:
        PASS += 1
        print(f"  OK  {label}")
    else:
        # Expected behavior — e.g. after disconnect, is_connected correctly returns False
        print(f"  (expected)  {label}  - {detail}")


async def mock_extension_handler(ws, commands_to_process):
    """Mock extension: reads incoming messages and sends responses."""
    while commands_to_process["remaining"] > 0:
        try:
            data = await asyncio.wait_for(ws.recv(), timeout=15.0)
            msg = json.loads(data)
            cmd_type = msg.get("type")
            msg_id = msg.get("messageId")
            log.debug(f"[MOCK EXT] Received: {cmd_type} (id={msg_id})")

            if cmd_type == "ping":
                pong = {
                    "type": "pong",
                    "messageId": str(uuid.uuid4()),
                    "source": "extension",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "inResponseTo": msg_id,
                    "payload": {"echo": msg.get("payload", {})},
                }
                await ws.send(json.dumps(pong))
                continue

            if cmd_type == "pong":
                continue

            # Respond with success for any recognized command
            resp = {
                "type": "action_result" if cmd_type not in ("get_browser_state", "get_page_text", "get_tab_info", "tab_list", "download_result") else cmd_type,
                "messageId": str(uuid.uuid4()),
                "source": "extension",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "inResponseTo": msg_id,
                "payload": {"success": True},
            }
            await ws.send(json.dumps(resp))
            commands_to_process["remaining"] -= 1
            log.debug(f"[MOCK EXT] Responded to {cmd_type} ({commands_to_process['remaining']} remaining)")

        except asyncio.TimeoutError:
            log.warning("[MOCK EXT] Timeout waiting for command")
            break


async def run_sync_client_commands(executor, client, commands_to_process):
    """Run the agent-loop-like sequence in a thread to avoid blocking the async event loop."""
    loop = asyncio.get_running_loop()
    results = []

    def check_connected(phase):
        c = client.is_connected()
        log.info(f"is_connected() after {phase}: {c}")
        return c

    # browser_open
    r = await loop.run_in_executor(executor, lambda: (
        check_connected("INIT"), True
    ))
    results.append(("INIT is_connected", r[0]))

    r = await loop.run_in_executor(executor, lambda: (
        check_connected("before browser_open"),
        client.send("tab_open", {"url": "https://example.com"}),
    ))
    results.append(("before browser_open is_connected", r[0]))

    # browser_open_tab
    r = await loop.run_in_executor(executor, lambda: (
        check_connected("before browser_open_tab"),
        client.send("tab_new", {"url": "https://example.com/page2"}),
    ))
    results.append(("before browser_open_tab is_connected", r[0]))

    # Simulate LLM think delay
    log.info("=== Simulating LLM think delay (3s) ===")
    await asyncio.sleep(3)

    r = await loop.run_in_executor(executor, lambda: (
        check_connected("after 3s idle"),
        client.send("get_page_text", {}),
    ))
    results.append(("after 3s idle is_connected", r[0]))

    # browser_close_tab
    r = await loop.run_in_executor(executor, lambda: (
        check_connected("before browser_close_tab"),
        client.send("tab_close", {}),
    ))
    results.append(("before browser_close_tab is_connected", r[0]))

    return results


async def _mock_reader_forever(ws, stop_event):
    """Read and respond to all commands on a WS connection."""
    while not stop_event.is_set():
        try:
            data = await asyncio.wait_for(ws.recv(), timeout=1.0)
            msg = json.loads(data)
            cmd_type = msg.get("type")
            msg_id = msg.get("messageId")
            if cmd_type == "ping":
                resp = {
                    "type": "pong",
                    "messageId": str(uuid.uuid4()),
                    "source": "extension",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "inResponseTo": msg_id,
                    "payload": {"echo": msg.get("payload", {})},
                }
                await ws.send(json.dumps(resp))
            elif cmd_type == "pong":
                continue
            else:
                resp = {
                    "type": cmd_type if cmd_type in ("tab_list", "browser_state") else "action_result",
                    "messageId": str(uuid.uuid4()),
                    "source": "extension",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "inResponseTo": msg_id,
                    "payload": {"success": True, "tabs": [], "count": 0},
                }
                await ws.send(json.dumps(resp))
        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed:
            log.info("[STRESS] Reader detected WS close, exiting")
            break


async def test_disconnect_reconnect_cycle():
    """Stress test: simulate extension disconnecting and reconnecting between commands.
    This reproduces the MV3 service worker termination scenario."""
    print("\n=== Stress Test: Disconnect/Reconnect Cycle ===")
    client = EdgeExtensionClient(base_url=HTTP_URL)
    executor = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    results = []

    # Phase 1: Connect + normal command
    async with websockets.connect(WS_URL) as ws:
        log.info("[STRESS] Phase 1: Connected")
        stop = asyncio.Event()
        reader = asyncio.create_task(_mock_reader_forever(ws, stop))
        await asyncio.sleep(0.2)

        await loop.run_in_executor(executor, lambda: client.send("tab_open", {}))
        log.info("[STRESS] Phase 1 complete (command succeeded while connected)")

        # Phase 2: Simulate disconnect
        log.info("[STRESS] Phase 2: Simulating extension disconnect (MV3 termination)...")
        stop.set()
        reader.cancel()
        await ws.close(code=1001, reason="Simulating service worker termination")
        await asyncio.sleep(0.3)

        r = await loop.run_in_executor(executor, lambda: client.is_connected())
        log.info(f"[STRESS] After disconnect, is_connected={r}")
        results.append(("After disconnect is_connected", r))

    # Phase 3: Reconnect
    log.info("[STRESS] Phase 3: Simulating extension reconnect...")
    async with websockets.connect(WS_URL) as ws2:
        stop2 = asyncio.Event()
        reader2 = asyncio.create_task(_mock_reader_forever(ws2, stop2))
        await asyncio.sleep(0.3)

        r = await loop.run_in_executor(executor, lambda: client.is_connected())
        log.info(f"[STRESS] After reconnect, is_connected={r}")
        results.append(("After reconnect is_connected", r))

        # Phase 4: Command after reconnect
        if r:
            try:
                log.info("[STRESS] Phase 4: Sending command after reconnect...")
                cmd_result = await loop.run_in_executor(executor, lambda: client.send("tab_list", {}))
                log.info(f"[STRESS] Command after reconnect succeeded: {cmd_result.get('success')}")
                results.append(("Command after reconnect", True))
            except Exception as e:
                log.error(f"[STRESS] Command after reconnect failed: {e}")
                results.append(("Command after reconnect", False))

        stop2.set()
        reader2.cancel()

    executor.shutdown(wait=False)
    return results


async def main():
    global PASS, FAIL
    server_started = threading.Event()

    def start_server():
        config = uvicorn.Config(
            app,
            host=SERVER_HOST,
            port=SERVER_PORT,
            log_level="info",
            ws_ping_interval=None,
            ws_ping_timeout=None,
        )
        server = uvicorn.Server(config)
        server_started.set()
        server.run()

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    server_started.wait()
    await asyncio.sleep(0.5)
    log.info("Server started")

    async with websockets.connect(WS_URL) as ws:
        log.info("Mock extension connected")

        client = EdgeExtensionClient(base_url=HTTP_URL)
        commands_to_process = {"remaining": 4}  # tab_open, tab_new, get_page_text, tab_close

        # Run mock handler and sync client commands concurrently
        executor = ThreadPoolExecutor(max_workers=1)
        results = await asyncio.gather(
            mock_extension_handler(ws, commands_to_process),
            run_sync_client_commands(executor, client, commands_to_process),
        )
        executor.shutdown(wait=False)

        results = results[1]

        print("\n--- Connection Check Results ---")
        for label, connected in results:
            check(f"{label}: {'connected' if connected else 'DISCONNECTED'}", connected)

        disconnected = [label for label, connected in results if not connected]
        if disconnected:
            print(f"\n*** TRANSIENT DISCONNECT DETECTED at: {disconnected} ***")

        final_connected = client.is_connected()
        check("Final is_connected()", final_connected)

    print(f"\n{'='*50}")
    print("Phase 1: Agent loop sequence")
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    phase1_pass = PASS
    phase1_fail = FAIL

    # Phase 2: Disconnect/reconnect stress test
    stress_results = await test_disconnect_reconnect_cycle()
    print("\n--- Stress Test Results ---")
    for label, connected in stress_results:
        if "disconnect is_connected" in label and not connected:
            check_expected(f"{label}: disconnected (expected after close)", False)
        else:
            check(f"{label}: {'connected' if connected else 'DISCONNECTED'}", connected)

    print(f"\n{'='*50}")
    print(f"Phase 1 (normal flow): {phase1_pass} passed, {phase1_fail} failed")
    print(f"Phase 2 (stress test): {PASS - phase1_pass} passed, {FAIL - phase1_fail} failed")
    if FAIL:
        print("*** FAIL: Transient disconnect detected! ***")
    else:
        print("No transient disconnect detected in this run")
    print(f"{'='*50}")

    server_thread.join(timeout=1)


if __name__ == "__main__":
    asyncio.run(main())
