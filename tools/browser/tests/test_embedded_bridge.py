"""
End-to-end test of the embedded bridge lifecycle (as used by main.py):
  1. start_bridge() -> bridge comes up in-process
  2. Mock extension connects via WebSocket
  3. Commands flow through the full pipeline
  4. stop_bridge() -> clean shutdown
  5. Reuse scenario: second start_bridge() while running reuses, not duplicates
"""
import asyncio
import json
import logging
import os
import sys
import time
import uuid

import httpx
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.browser_server import is_bridge_running, start_bridge, stop_bridge

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("embedded_test")
log.setLevel(logging.DEBUG)

HOST = "127.0.0.1"
PORT = 8742
HTTP_URL = f"http://{HOST}:{PORT}"
WS_URL = f"ws://{HOST}:{PORT}/ws"

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


async def main():
    global PASS, FAIL

    # Ensure no leftover bridge from a previous test run
    stop_bridge()
    if is_bridge_running():
        print("A bridge is already running on 8742 (external process) -- stopping test")
        check("Test requires port 8742 free", False)
        return

    # 1. Start the embedded bridge (same call main.py uses)
    print("\n=== Phase 1: start_bridge() ===")
    ok = start_bridge()
    check("start_bridge() returns True", ok)
    check("is_bridge_running() True", is_bridge_running())

    # 2. Reuse scenario
    print("\n=== Phase 2: duplicate start reuses ===")
    ok2 = start_bridge()
    check("second start_bridge() returns True (reused)", ok2)

    # 3. Full pipeline with mock extension
    print("\n=== Phase 3: pipeline (extension -> command -> response) ===")
    async with websockets.connect(WS_URL) as ws:
        check("extension connects to embedded bridge", True)
        await asyncio.sleep(0.2)

        async with httpx.AsyncClient() as http:
            msg = {"type": "tab_list", "messageId": str(uuid.uuid4()), "payload": {}}
            send_task = asyncio.create_task(
                http.post(f"{HTTP_URL}/api/send", json=msg, timeout=10)
            )
            recv = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            check("server forwarded command to extension", recv.get("type") == "tab_list")

            resp = {
                "type": "tab_list", "messageId": str(uuid.uuid4()),
                "source": "extension", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "inResponseTo": recv["messageId"],
                "payload": {"success": True, "tabs": [], "count": 0},
            }
            await ws.send(json.dumps(resp))
            http_resp = await asyncio.wait_for(send_task, timeout=5.0)
            result = http_resp.json()
            check("command response returned to caller",
                  http_resp.status_code == 200 and result.get("payload", {}).get("success"))

    # 4. Stop the bridge
    print("\n=== Phase 4: stop_bridge() ===")
    stop_bridge()
    await asyncio.sleep(0.3)
    check("is_bridge_running() False after stop", not is_bridge_running())

    # 5. Restart after stop works (fresh start)
    print("\n=== Phase 5: restart ===")
    ok3 = start_bridge()
    check("restart works", ok3 and is_bridge_running())
    stop_bridge()
    await asyncio.sleep(0.3)

    print(f"\n{'='*50}")
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
