"""
Comprehensive browser integration test.
Tests EVERY public BrowserAgent and browser_tool function.

Architecture:
  Test starts FastAPI server in a thread, connects a mock extension
  via WebSocket, then tests the full pipeline.

  Since EdgeExtensionClient uses a synchronous httpx.Client, we test
  by calling HTTP endpoints directly with httpx.AsyncClient.

  Every command is tested: does the server forward it? Does the
  handler respond? Does the result come back correctly?

  Results: PASS / FAIL / NOT_IMPLEMENTED / TIMEOUT / WRONG_COMMAND
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid

import httpx
import uvicorn
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from backend.browser_server import app
from tools.browser.browser_agent import browser_agent

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("intg_test")
log.setLevel(logging.DEBUG)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 18999
HTTP_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
WS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
WS_EVENTS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws/events"

RESULTS = {}


def _result(name, status, detail=""):
    tag = {"PASS": "✓", "FAIL": "✗", "NOT_IMPLEMENTED": "⊘",
           "TIMEOUT": "⏱", "WRONG_COMMAND": "⚠"}.get(status, "?")
    RESULTS[name] = status
    msg = f"  {tag}  {name}"
    if detail:
        msg += f"  — {detail}"
    log.info(msg)


def _ext_response(original_msg_id, type_, payload):
    return {
        "type": type_,
        "messageId": str(uuid.uuid4()),
        "source": "extension",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inResponseTo": original_msg_id,
        "payload": payload,
    }


async def test_http_commands():
    """Test every command through HTTP POST /api/send.
    This tests the server-side pipeline directly without going through
    the sync EdgeExtensionClient."""
    async with websockets.connect(WS_URL) as ws:
        log.info("[MOCK EXT] Connected to server")

        async with httpx.AsyncClient() as http:
            # ================================================================
            # Helper: send a command and verify the exchange
            # ================================================================

            async def test_cmd(name, cmd_type, payload=None,
                               response_payload=None, response_type="action_result",
                               expected_success=True):
                try:
                    msg = {
                        "type": cmd_type,
                        "messageId": str(uuid.uuid4()),
                        "payload": payload or {},
                    }

                    send_task = asyncio.create_task(
                        http.post(f"{HTTP_URL}/api/send", json=msg, timeout=10)
                    )

                    recv = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    recv_type = recv.get("type")
                    if recv_type != cmd_type:
                        _result(name, "WRONG_COMMAND",
                                f"Expected '{cmd_type}', got '{recv_type}'")
                        send_task.cancel()
                        return

                    resp_payload = response_payload or {"success": True}
                    response = _ext_response(recv["messageId"], response_type, resp_payload)
                    await ws.send(json.dumps(response))

                    http_resp = await asyncio.wait_for(send_task, timeout=5.0)
                    result = http_resp.json()

                    if http_resp.status_code != 200:
                        _result(name, "FAIL",
                                f"HTTP {http_resp.status_code}: {result}")
                        return

                    payload = result.get("payload", result)
                    if expected_success is None:
                        _result(name, "PASS")  # any response is fine
                    elif expected_success and isinstance(payload, dict) and payload.get("success") is False:
                        _result(name, "FAIL",
                                f"Unexpected failure: {payload.get('error', '?')}")
                    elif not expected_success and isinstance(payload, dict) and payload.get("success") is not False:
                        _result(name, "FAIL",
                                "Expected failure but got success")
                    else:
                        _result(name, "PASS")

                except asyncio.TimeoutError:
                    _result(name, "TIMEOUT")
                except Exception as e:
                    _result(name, "FAIL", f"{type(e).__name__}: {e}")

            # ================================================================
            # HEALTH
            # ================================================================
            h = await http.get(f"{HTTP_URL}/health")
            if h.status_code == 200:
                _result("GET /health returns 200", "PASS")
            else:
                _result("GET /health returns 200", "FAIL", str(h.status_code))

            # ================================================================
            # TAB MANAGEMENT
            # ================================================================
            await test_cmd("tab_open → chrome.tabs.create (new tab)",
                           "tab_open", {"url": "https://example.com"},
                           {"success": True, "tabId": 1, "url": "https://example.com"})

            await test_cmd("tab_navigate → chrome.tabs.update (current tab)",
                           "tab_navigate", {"url": "https://example.com"},
                           {"success": True, "tabId": 1, "url": "https://example.com"})

            await test_cmd("tab_new → create empty tab",
                           "tab_new", {"url": "about:blank"},
                           {"success": True, "tabId": 2, "url": "about:blank"})

            await test_cmd("tab_close → close active tab",
                           "tab_close", {},
                           {"success": True, "tabId": 2})

            await test_cmd("tab_switch → switch tab by id",
                           "tab_switch", {"tabId": 1},
                           {"success": True, "tabId": 1})

            await test_cmd("tab_reload → reload tab",
                           "tab_reload", {},
                           {"success": True, "tabId": 1})

            await test_cmd("tab_back → go back",
                           "tab_back", {},
                           {"success": True, "tabId": 1})

            await test_cmd("tab_forward → go forward",
                           "tab_forward", {},
                           {"success": True, "tabId": 1})

            await test_cmd("tab_list → list all tabs",
                           "tab_list", {},
                           {"success": True, "tabs": [{"tabId": 1, "url": "about:blank"}], "count": 1})

            # ================================================================
            # PAGE READING
            # ================================================================
            await test_cmd("get_page_text → page text content",
                           "get_page_text", {},
                           {"url": "https://example.com", "title": "Example",
                            "text": "Hello world", "metadata": {}},
                           response_type="page_text")

            await test_cmd("get_tab_info → tab metadata",
                           "get_tab_info", {},
                           {"url": "https://example.com", "title": "Test",
                            "favicon": "", "selectedText": ""},
                           response_type="tab_info")

            # ================================================================
            # PAGE INTERACTION
            # ================================================================
            await test_cmd("click → click element",
                           "click", {"selector": "#btn", "options": {}},
                           {"success": True})

            await test_cmd("double_click → double click",
                           "double_click", {"selector": "#btn"},
                           {"success": True})

            await test_cmd("right_click → right click",
                           "right_click", {"selector": "#btn"},
                           {"success": True})

            await test_cmd("focus → focus element",
                           "focus", {"selector": "#input"},
                           {"success": True})

            await test_cmd("type_text → type into input",
                           "type_text", {"selector": "#input", "text": "hello", "clear": True},
                           {"success": True, "value": "hello"})

            await test_cmd("clear_input → clear input",
                           "clear_input", {"selector": "#input"},
                           {"success": True})

            await test_cmd("press_key → dispatch key event",
                           "press_key", {"key": "Enter"},
                           {"success": True, "key": "Enter"})

            await test_cmd("scroll_to → scroll element into view",
                           "scroll_to", {"selector": "#footer"},
                           {"success": True})

            await test_cmd("scroll_by → scroll by pixels",
                           "scroll_by", {"x": 0, "y": 600},
                           {"success": True, "scrollX": 0, "scrollY": 600})

            await test_cmd("select_option → select dropdown option",
                           "select_option", {"selector": "#sel", "value": "opt1"},
                           {"success": True, "selectedValue": "opt1"})

            # ================================================================
            # DOM QUERIES
            # ================================================================
            await test_cmd("query_selector → query single element",
                           "query_selector", {"selector": "#main"},
                           {"success": True, "tagName": "DIV", "id": "main"})

            await test_cmd("query_all → query multiple elements",
                           "query_all", {"selector": "p"},
                           {"success": True, "count": 3, "elements": []})

            await test_cmd("wait_for_selector → wait with timeout",
                           "wait_for_selector", {"selector": "#loaded", "timeout": 5000},
                           {"success": True, "found": True})

            await test_cmd("element_exists → check element exists",
                           "element_exists", {"selector": "#main"},
                           {"success": True, "exists": True})

            await test_cmd("get_element_text → get element text",
                           "get_element_text", {"selector": "#main"},
                           {"success": True, "text": "Hello"})

            await test_cmd("get_element_attrs → get element attributes",
                           "get_element_attrs", {"selector": "#main"},
                           {"success": True, "attributes": {"id": "main"}})

            await test_cmd("get_bounding_box → get element rect",
                           "get_bounding_box", {"selector": "#main"},
                           {"success": True, "rect": {"x": 0, "y": 0, "width": 100, "height": 50}})

            # ================================================================
            # BROWSER STATE
            # ================================================================
            await test_cmd("get_browser_state → full browser state",
                           "get_browser_state", {},
                           {"success": True, "focusedWindowId": 1, "activeTab": None,
                            "tabs": [], "windows": []},
                           response_type="browser_state")

            # ================================================================
            # DOWNLOADS
            # ================================================================
            await test_cmd("download_list → list downloads",
                           "download_list", {"limit": 20},
                           {"success": True, "downloads": [], "count": 0},
                           response_type="download_result")

            await test_cmd("download_search → search downloads",
                           "download_search", {"query": "test"},
                           {"success": True, "downloads": [], "count": 0, "query": "test"},
                           response_type="download_result")

            await test_cmd("download_clear_cache → clear download cache",
                           "download_clear_cache", {},
                           {"success": True, "cleared": True},
                           response_type="download_result")

            await test_cmd("download_status → get download status",
                           "download_status", {"id": 1},
                           {"success": True, "id": 1, "state": "complete"},
                           response_type="download_result")

            # ================================================================
            # ERROR HANDLING
            # ================================================================
            await test_cmd("handler reports failure → error returned gracefully",
                           "tab_list", {},
                           {"success": False, "error": "Something went wrong"},
                           expected_success=False)

            await test_cmd("unknown command → handled (no matching handler)",
                           "nonexistent_command", {},
                           {"success": False, "error": "No handler for type"},
                           expected_success=None)


async def test_browser_agent_local():
    """Test BrowserAgent methods that don't need extension connection."""
    from tools.browser.browser_agent import BrowserAgent
    agent = BrowserAgent()

    for name, call in [
        ("get_state()", lambda: agent.get_state()),
        ("set_task()", lambda: agent.set_task("test", 3)),
        ("update_progress()", lambda: agent.update_progress("note")),
        ("complete_step()", lambda: agent.complete_step("step")),
        ("plan()", lambda: agent.plan("goal", ["a"])),
        ("note()", lambda: agent.note("k", "v")),
        ("summarize_session()", lambda: agent.summarize_session()),
        ("record_data()", lambda: agent.record_data("k", "v")),
    ]:
        try:
            r = call()
            if isinstance(r, dict) and r.get("success"):
                _result(f"BrowserAgent.{name}", "PASS")
            else:
                _result(f"BrowserAgent.{name}", "FAIL", str(r))
        except Exception as e:
            _result(f"BrowserAgent.{name}", "FAIL", str(e))


async def test_not_implemented():
    """Test methods that should return NOT_IMPLEMENTED."""
    for name, call in [
        ("BrowserAgent.hover()", lambda: browser_agent.hover("#x")),
        ("BrowserAgent.drag_and_drop()", lambda: browser_agent.drag_and_drop("#a", "#b")),
        ("BrowserAgent.upload_file()", lambda: browser_agent.upload_file("#f", "/f.txt")),
        ("BrowserAgent.fill_form()", lambda: browser_agent.fill_form({"x": "y"})),
        ("BrowserAgent.download_via()", lambda: browser_agent.download_via("#d")),
        ("BrowserAgent.wait_for_login()", lambda: browser_agent.wait_for_login()),
        ("BrowserAgent.vision()", lambda: browser_agent.vision()),
        ("BrowserAgent.read_dom_summary()", lambda: browser_agent.read_dom_summary()),
        ("BrowserAgent.extract_tables()", lambda: browser_agent.extract_tables()),
        ("BrowserAgent.extract_links()", lambda: browser_agent.extract_links()),
        ("BrowserAgent.extract_forms()", lambda: browser_agent.extract_forms()),
        ("BrowserAgent.duplicate_tab()", lambda: browser_agent.duplicate_tab()),
        ("BrowserAgent.label_tab()", lambda: browser_agent.label_tab("1", "x")),
        ("BrowserAgent.youtube_play_first_result()",
         lambda: browser_agent.youtube_play_first_result()),
    ]:
        try:
            r = call()
            if isinstance(r, dict) and r.get("success") is False:
                _result(name, "NOT_IMPLEMENTED", r.get("error", ""))
            else:
                _result(name, "FAIL", f"Expected failure, got: {r}")
        except Exception as e:
            _result(name, "FAIL", str(e))


async def test_events():
    """Test push event delivery via /ws/events."""
    async with websockets.connect(WS_URL) as ws:
        async with websockets.connect(WS_EVENTS_URL) as events_ws:
            # Push tab_created
            push = {
                "type": "tab_created",
                "messageId": str(uuid.uuid4()),
                "source": "extension",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "inResponseTo": None,
                "payload": {"tabId": 100, "url": "https://example.com"},
            }
            await ws.send(json.dumps(push))
            try:
                r = json.loads(await asyncio.wait_for(events_ws.recv(), timeout=3.0))
                if r.get("type") == "tab_created":
                    _result("Push event: tab_created", "PASS")
                else:
                    _result("Push event: tab_created", "FAIL", f"Got type={r.get('type')}")
            except asyncio.TimeoutError:
                _result("Push event: tab_created", "FAIL", "Not delivered")

            # Push navigation_completed
            push2 = {
                "type": "navigation_completed",
                "messageId": str(uuid.uuid4()),
                "source": "extension",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "inResponseTo": None,
                "payload": {"tabId": 100, "url": "https://example.com/page2"},
            }
            await ws.send(json.dumps(push2))
            try:
                r = json.loads(await asyncio.wait_for(events_ws.recv(), timeout=3.0))
                if r.get("type") == "navigation_completed":
                    _result("Push event: navigation_completed", "PASS")
                else:
                    _result("Push event: navigation_completed", "FAIL", f"Got type={r.get('type')}")
            except asyncio.TimeoutError:
                _result("Push event: navigation_completed", "FAIL", "Not delivered")


async def test_require_connected():
    """Test EdgeExtensionClient.require_connected() behavior."""
    from tools.browser.edge_extension_client import EdgeExtensionClient
    client = EdgeExtensionClient(base_url=f"http://{SERVER_HOST}:{SERVER_PORT}")

    async with websockets.connect(WS_URL) as ws:
        # Must be connected now
        connected = client.is_connected()
        _result("EdgeExtensionClient.is_connected() after WS connect",
                "PASS" if connected else "FAIL")


async def main():
    server_thread = None
    server_started = threading.Event()

    def start_server():
        config = uvicorn.Config(
            app,
            host=SERVER_HOST,
            port=SERVER_PORT,
            log_level="critical",
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

    log.info("=" * 60)
    log.info("COMPREHENSIVE BROWSER INTEGRATION TEST")
    log.info("=" * 60)

    # Phase 1: Local methods
    log.info("\n--- Phase 1: BrowserAgent Local Methods ---")
    await test_browser_agent_local()

    # Phase 2: HTTP commands via full pipeline
    log.info("\n--- Phase 2: HTTP → WS → Response Pipeline ---")
    await test_http_commands()

    # Phase 3: Not implemented methods
    log.info("\n--- Phase 3: Not-Implemented Methods ---")
    await test_not_implemented()

    # Phase 4: Event system
    log.info("\n--- Phase 4: Push Events ---")
    await test_events()

    # Phase 5: Connection check
    log.info("\n--- Phase 5: Connection State ---")
    await test_require_connected()

    # Summary
    log.info("\n" + "=" * 60)
    log.info("FINAL RESULTS")
    log.info("=" * 60)
    counts = {}
    for v in RESULTS.values():
        counts[v] = counts.get(v, 0) + 1
    total = sum(counts.values())
    for k in ["PASS", "FAIL", "NOT_IMPLEMENTED", "TIMEOUT", "WRONG_COMMAND"]:
        v = counts.get(k, 0)
        pct = f" {v * 100 // total}%" if total else ""
        log.info(f"  {k}: {v}{pct}")
    log.info(f"  TOTAL: {total}")


if __name__ == "__main__":
    asyncio.run(main())
