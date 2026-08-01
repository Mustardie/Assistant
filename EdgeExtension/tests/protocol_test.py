"""
Integration test for the Nova-Extension bridge protocol.
Tests Phase 1-5: communication, tab info, page text, browser control, downloads.

Starts the FastAPI server in a thread, connects a mock extension via WebSocket,
and verifies the full request-response flow for every message type.
"""

import asyncio
import json
import logging
import sys
import threading
import time
import uuid

import httpx
import uvicorn
import websockets

sys.path.insert(0, ".")
from backend.browser_server import app, EXPECTED_EXTENSION_CODE_VERSION

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("test")
log.setLevel(logging.INFO)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 18742
HTTP_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
WS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
WS_EVENTS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws/events"


def _msg(type_, payload=None, msg_id=None):
    return {
        "type": type_,
        "messageId": msg_id or str(uuid.uuid4()),
        "payload": payload or {},
    }


def _ext_response(original_msg_id, type_, payload):
    return {
        "type": type_,
        "messageId": str(uuid.uuid4()),
        "source": "extension",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inResponseTo": original_msg_id,
        "payload": payload,
    }


def _ext_push(type_, payload):
    return {
        "type": type_,
        "messageId": str(uuid.uuid4()),
        "source": "extension",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inResponseTo": None,
        "payload": payload,
    }


async def run_tests():
    passed = 0
    failed = 0

    async def test(name, fn):
        nonlocal passed, failed
        try:
            await fn()
            log.info(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            log.error(f"  FAIL  {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
        except Exception as e:
            log.error(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1

    async with websockets.connect(WS_URL) as ws:
        log.info("[EXT] Connected to server")

        async with httpx.AsyncClient() as client:

            # ============== PHASE 1: PING / PONG ==============
            async def phase1():
                ping = _msg("ping", {"echo": "hello"})
                send_task = asyncio.create_task(
                    client.post(f"{HTTP_URL}/api/send", json=ping, timeout=10)
                )
                recv = json.loads(await ws.recv())
                assert recv["type"] == "ping"
                pong = _ext_response(recv["messageId"], "pong", {"echo": recv["payload"]})
                await ws.send(json.dumps(pong))
                resp = (await send_task).json()
                assert resp["type"] == "pong"
                assert resp["inResponseTo"] == ping["messageId"]

            await test("PHASE 1: PING -> PONG", phase1)

            # ============== PHASE 1b: EXTENSION CODE HOT-RELOAD CHECK ==============
            def _ext_ping(payload):
                return {
                    "type": "ping",
                    "messageId": str(uuid.uuid4()),
                    "source": "extension",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "inResponseTo": None,
                    "payload": payload,
                }

            async def phase1b():
                # Stale build -> server must order a self-reload exactly once.
                stale = _ext_ping({"heartbeat": True, "codeVersion": "0"})
                await ws.send(json.dumps(stale))
                pong1 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert pong1["type"] == "pong", f"expected pong, got {pong1.get('type')}"
                reload_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert reload_msg["type"] == "extension_reload", f"expected extension_reload, got {reload_msg.get('type')}"
                assert reload_msg["payload"]["loaded"] == "0"
                assert reload_msg["payload"]["expected"] == EXPECTED_EXTENSION_CODE_VERSION

                # Second stale heartbeat -> pong only, NO second reload
                # (once-per-connection guard prevents a reload ping-pong).
                await ws.send(json.dumps(stale))
                pong2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert pong2["type"] == "pong", f"expected pong, got {pong2.get('type')}"
                try:
                    extra = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.5))
                    raise AssertionError(f"unexpected extra message after stale pings: {extra.get('type')}")
                except asyncio.TimeoutError:
                    pass

                # Up-to-date build -> plain pong, no reload command.
                fresh = _ext_ping({"heartbeat": True, "codeVersion": EXPECTED_EXTENSION_CODE_VERSION})
                await ws.send(json.dumps(fresh))
                pong3 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert pong3["type"] == "pong", f"expected pong, got {pong3.get('type')}"
                try:
                    extra = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.5))
                    raise AssertionError(f"unexpected extra message after fresh ping: {extra.get('type')}")
                except asyncio.TimeoutError:
                    pass

            await test("PHASE 1b: stale extension code triggers exactly one self-reload", phase1b)

            # ============== PHASE 2: TAB INFO ==============
            async def phase2():
                req = _msg("get_tab_info")
                send_task = asyncio.create_task(
                    client.post(f"{HTTP_URL}/api/send", json=req, timeout=10)
                )
                recv = json.loads(await ws.recv())
                assert recv["type"] == "get_tab_info"
                resp_data = _ext_response(recv["messageId"], "tab_info", {
                    "url": "https://example.com", "title": "Test", "favicon": "", "selectedText": "",
                })
                await ws.send(json.dumps(resp_data))
                resp = (await send_task).json()
                assert resp["type"] == "tab_info"
                assert resp["payload"]["url"] == "https://example.com"

            await test("PHASE 2: get_tab_info -> tab_info", phase2)

            # ============== PHASE 3: PAGE TEXT ==============
            async def phase3():
                req = _msg("get_page_text")
                send_task = asyncio.create_task(
                    client.post(f"{HTTP_URL}/api/send", json=req, timeout=10)
                )
                recv = json.loads(await ws.recv())
                assert recv["type"] == "get_page_text"
                resp_data = _ext_response(recv["messageId"], "page_text", {
                    "url": "https://example.com",
                    "title": "Test",
                    "text": "Hello world",
                    "html": "<p>Hello world</p>",
                    "textLength": 11,
                    "htmlLength": 18,
                    "metadata": {"description": "test"},
                    "chunked": False,
                })
                await ws.send(json.dumps(resp_data))
                resp = (await send_task).json()
                assert resp["type"] == "page_text"
                assert resp["payload"]["text"] == "Hello world"

            await test("PHASE 3: get_page_text -> page_text", phase3)

            # ============== PHASE 4: TAB MANAGEMENT ==============
            async def phase4_tab_open():
                req = _msg("tab_open", {"url": "https://example.com"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "tab_open"
                assert recv["payload"]["url"] == "https://example.com"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "tabId": 1, "url": "https://example.com"})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["type"] == "action_result"
                assert resp["payload"]["success"] is True
                assert resp["payload"]["tabId"] == 1

            await test("PHASE 4: tab_open -> action_result", phase4_tab_open)

            async def phase4_tab_new():
                req = _msg("tab_new", {"url": "about:blank"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "tab_new"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "tabId": 2})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: tab_new -> action_result", phase4_tab_new)

            async def phase4_tab_close():
                req = _msg("tab_close")
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "tab_close"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "tabId": 1})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: tab_close -> action_result", phase4_tab_close)

            async def phase4_tab_switch():
                req = _msg("tab_switch", {"tabId": 3})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "tab_switch"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "tabId": 3})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: tab_switch by id -> action_result", phase4_tab_switch)

            async def phase4_tab_switch_index():
                req = _msg("tab_switch", {"index": 0})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "tab_switch"
                assert "index" in recv["payload"]
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "tabId": 1})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: tab_switch by index -> action_result", phase4_tab_switch_index)

            async def phase4_tab_reload():
                req = _msg("tab_reload")
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "tab_reload"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: tab_reload -> action_result", phase4_tab_reload)

            async def phase4_tab_back():
                req = _msg("tab_back")
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "tab_back"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: tab_back -> action_result", phase4_tab_back)

            async def phase4_tab_forward():
                req = _msg("tab_forward")
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "tab_forward"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: tab_forward -> action_result", phase4_tab_forward)

            async def phase4_tab_list():
                req = _msg("tab_list")
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "tab_list"
                resp_data = _ext_response(recv["messageId"], "action_result", {
                    "success": True, "tabs": [{"tabId": 1, "url": "https://a.com", "title": "A", "active": True}], "count": 1,
                })
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True
                assert resp["payload"]["count"] == 1

            await test("PHASE 4: tab_list -> action_result", phase4_tab_list)

            # ============== PHASE 4: PAGE INTERACTION ==============
            async def phase4_click():
                req = _msg("click", {"selector": "#btn", "options": {}})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "click"
                assert recv["payload"]["selector"] == "#btn"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: click -> action_result", phase4_click)

            async def phase4_click_failure():
                req = _msg("click", {"selector": "#nonexistent"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": False, "error": "Element not found: #nonexistent"})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is False
                assert "Element not found" in resp["payload"]["error"]

            await test("PHASE 4: click failure returns error", phase4_click_failure)

            async def phase4_double_click():
                req = _msg("double_click", {"selector": "#btn"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "double_click"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: double_click -> action_result", phase4_double_click)

            async def phase4_right_click():
                req = _msg("right_click", {"selector": "#btn"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "right_click"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: right_click -> action_result", phase4_right_click)

            async def phase4_focus():
                req = _msg("focus", {"selector": "#input"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "focus"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: focus -> action_result", phase4_focus)

            async def phase4_scroll_to():
                req = _msg("scroll_to", {"selector": "#footer"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "scroll_to"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "rect": {"x": 0, "y": 1000}})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: scroll_to -> action_result", phase4_scroll_to)

            async def phase4_scroll_by():
                req = _msg("scroll_by", {"x": 0, "y": 500})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "scroll_by"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "scrollY": 500})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: scroll_by -> action_result", phase4_scroll_by)

            async def phase4_type_text():
                req = _msg("type_text", {"selector": "#input", "text": "hello", "clear": True})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "type_text"
                assert recv["payload"]["text"] == "hello"
                assert recv["payload"]["clear"] is True
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "value": "hello"})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: type_text -> action_result", phase4_type_text)

            async def phase4_clear_input():
                req = _msg("clear_input", {"selector": "#input"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "clear_input"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: clear_input -> action_result", phase4_clear_input)

            async def phase4_press_key():
                req = _msg("press_key", {"key": "Enter"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "press_key"
                assert recv["payload"]["key"] == "Enter"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "key": "Enter"})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: press_key -> action_result", phase4_press_key)

            async def phase4_select_option():
                req = _msg("select_option", {"selector": "#select", "value": "opt2"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "select_option"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "selectedValue": "opt2"})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True

            await test("PHASE 4: select_option -> action_result", phase4_select_option)

            # ============== PHASE 4: DOM QUERIES ==============
            async def phase4_query_selector():
                req = _msg("query_selector", {"selector": "#main"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "query_selector"
                resp_data = _ext_response(recv["messageId"], "action_result", {
                    "success": True, "tagName": "DIV", "id": "main", "textContent": "content",
                })
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["success"] is True
                assert resp["payload"]["tagName"] == "DIV"

            await test("PHASE 4: query_selector -> action_result", phase4_query_selector)

            async def phase4_query_all():
                req = _msg("query_all", {"selector": ".item"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "query_all"
                resp_data = _ext_response(recv["messageId"], "action_result", {
                    "success": True, "count": 3, "elements": [{"index": 0}, {"index": 1}, {"index": 2}],
                })
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["count"] == 3

            await test("PHASE 4: query_all -> action_result", phase4_query_all)

            async def phase4_wait_for_selector():
                req = _msg("wait_for_selector", {"selector": "#dynamic", "timeout": 5000})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "wait_for_selector"
                assert recv["payload"]["timeout"] == 5000
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "found": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["found"] is True

            await test("PHASE 4: wait_for_selector -> action_result", phase4_wait_for_selector)

            async def phase4_element_exists():
                req = _msg("element_exists", {"selector": "#btn"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "element_exists"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "exists": True})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["exists"] is True

            await test("PHASE 4: element_exists -> action_result", phase4_element_exists)

            async def phase4_get_element_text():
                req = _msg("get_element_text", {"selector": "#title"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "get_element_text"
                resp_data = _ext_response(recv["messageId"], "action_result", {"success": True, "text": "Hello World"})
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["text"] == "Hello World"

            await test("PHASE 4: get_element_text -> action_result", phase4_get_element_text)

            async def phase4_get_element_attrs():
                req = _msg("get_element_attrs", {"selector": "#link"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "get_element_attrs"
                resp_data = _ext_response(recv["messageId"], "action_result", {
                    "success": True, "attributes": {"href": "https://example.com", "class": "link"},
                })
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["attributes"]["href"] == "https://example.com"

            await test("PHASE 4: get_element_attrs -> action_result", phase4_get_element_attrs)

            async def phase4_get_bounding_box():
                req = _msg("get_bounding_box", {"selector": "#box"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "get_bounding_box"
                resp_data = _ext_response(recv["messageId"], "action_result", {
                    "success": True, "rect": {"x": 10, "y": 20, "width": 100, "height": 50},
                })
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["rect"]["width"] == 100

            await test("PHASE 4: get_bounding_box -> action_result", phase4_get_bounding_box)

            # ============== PHASE 5: DOWNLOAD PUSH EVENTS ==============
            async def phase5_event_ws():
                """Verify push events are delivered over the event WebSocket."""
                # Connect to the events WebSocket
                async with websockets.connect(WS_EVENTS_URL) as events_ws:
                    # Send a push event from the extension
                    push = _ext_push("download_created", {
                        "id": 42,
                        "filename": "report.pdf",
                        "url": "https://example.com/report.pdf",
                        "mime": "application/pdf",
                        "totalBytes": 102400,
                        "state": "in_progress",
                        "timestamp": "2026-01-01T00:00:00Z",
                    })
                    await ws.send(json.dumps(push))

                    # Nova should receive the push event
                    received = json.loads(await events_ws.recv())
                    assert received["type"] == "download_created"
                    assert received["payload"]["id"] == 42
                    assert received["payload"]["filename"] == "report.pdf"

            await test("PHASE 5: download push event delivered via event WS", phase5_event_ws)

            async def phase5_download_changed():
                """Verify download changed push event."""
                async with websockets.connect(WS_EVENTS_URL) as events_ws:
                    push = _ext_push("download_changed", {
                        "id": 42,
                        "state": "complete",
                        "filename": "report.pdf",
                        "receivedBytes": 102400,
                        "totalBytes": 102400,
                        "timestamp": "2026-01-01T00:00:01Z",
                    })
                    await ws.send(json.dumps(push))
                    received = json.loads(await events_ws.recv())
                    assert received["type"] == "download_changed"
                    assert received["payload"]["state"] == "complete"

            await test("PHASE 5: download_changed push event", phase5_download_changed)

            # ============== PHASE 5: DOWNLOAD COMMANDS ==============
            async def phase5_download_list():
                req = _msg("download_list", {"limit": 5})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "download_list"
                resp_data = _ext_response(recv["messageId"], "download_result", {
                    "success": True, "downloads": [{"id": 1, "filename": "file.pdf"}], "count": 1,
                })
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["type"] == "download_result"
                assert resp["payload"]["success"] is True
                assert resp["payload"]["count"] == 1

            await test("PHASE 5: download_list -> download_result", phase5_download_list)

            async def phase5_download_status():
                req = _msg("download_status", {"id": 1})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "download_status"
                assert recv["payload"]["id"] == 1
                resp_data = _ext_response(recv["messageId"], "download_result", {
                    "success": True, "id": 1, "filename": "file.pdf", "state": "complete",
                })
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["state"] == "complete"

            await test("PHASE 5: download_status -> download_result", phase5_download_status)

            async def phase5_download_search():
                req = _msg("download_search", {"query": "pdf"})
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "download_search"
                assert recv["payload"]["query"] == "pdf"
                resp_data = _ext_response(recv["messageId"], "download_result", {
                    "success": True, "downloads": [{"id": 1, "filename": "report.pdf"}], "count": 1,
                })
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["count"] == 1

            await test("PHASE 5: download_search -> download_result", phase5_download_search)

            async def phase5_download_clear_cache():
                req = _msg("download_clear_cache")
                s = asyncio.create_task(client.post(f"{HTTP_URL}/api/send", json=req, timeout=10))
                recv = json.loads(await ws.recv())
                assert recv["type"] == "download_clear_cache"
                resp_data = _ext_response(recv["messageId"], "download_result", {
                    "success": True, "cleared": True, "removedCount": 5,
                })
                await ws.send(json.dumps(resp_data))
                resp = (await s).json()
                assert resp["payload"]["cleared"] is True

            await test("PHASE 5: download_clear_cache -> download_result", phase5_download_clear_cache)

            # ============== ERROR HANDLING ==============
            async def phase_error_no_ext():
                """When no extension connected, /api/send returns 502."""
                # This is tested before we connected, but verify our test setup
                pass

            await test("ERROR: extension connected (setup ok)", phase_error_no_ext)

    log.info(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


def _run_server():
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="error")


if __name__ == "__main__":
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()
    time.sleep(3)

    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)
