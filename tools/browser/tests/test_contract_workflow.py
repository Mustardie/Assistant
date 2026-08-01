"""
Contract regression test: the complete browser workflow
    open Wikipedia page -> wait for navigation -> read page -> summarize

Runs through the REAL production stack, exactly like the agent loop does:
    run_tool() -> tool_registry wrapper -> BrowserAgent -> EdgeExtensionClient
              -> HTTP /api/send -> FastAPI server -> WebSocket -> extension

The mock extension models the FIXED extension contract:
  - tab_open / tab_new / tab_navigate create the tab, mark it loading,
    emit navigation_completed, and ONLY THEN respond with the real URL
    (never "").
  - tab_list / tab_switch / tab_close / get_browser_state / get_page_text
    behave like the real handlers.

This test FAILS if any of the following contracts change:
  - tool signatures (e.g. browser_read() must accept 'url' without raising)
  - response formats (open returns non-empty url; read returns url/title/text)
  - browser command contracts (payload forwarded; navigation waits for load)
  - duplicate-tab prevention (second open of same URL must reuse, not create)
"""

import asyncio
import inspect
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
from tools.browser.browser_agent import browser_agent
from tools.tool_registry import TOOLS, run_tool

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("contract_test")
log.setLevel(logging.DEBUG)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 19002
HTTP_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
WS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
WS_EVENTS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws/events"

WIKI_URL = "https://en.wikipedia.org/wiki/Python_(programming_language)"
EXAMPLE_URL = "https://example.com/"

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


# ---------------------------------------------------------------------------
# Mock extension: models the REAL (fixed) extension behavior
# ---------------------------------------------------------------------------

class MockExtension:
    """Simulates the Edge extension: tab store, navigation completion
    waiting, real URL responses, navigation_completed push events."""

    NAVIGATION_DELAY_S = 0.4

    def __init__(self):
        self.tabs = []          # [{tabId, url, title, status, active}]
        self.next_tab_id = 1
        self.tab_new_count = 0
        self.tab_open_count = 0
        self.received_urls = {}  # command type -> [urls]
        self.load_completed = asyncio.Event()
        self.ws = None

    def _tab(self, tab_id):
        for t in self.tabs:
            if t["tabId"] == tab_id:
                return t
        return None

    async def _simulate_navigation(self, tab):
        """loading -> complete, then push navigation_completed."""
        tab["status"] = "loading"
        await asyncio.sleep(self.NAVIGATION_DELAY_S)
        tab["status"] = "complete"
        self.load_completed.set()
        await self.ws.send(json.dumps({
            "type": "navigation_completed",
            "messageId": str(uuid.uuid4()),
            "source": "extension",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "inResponseTo": None,
            "payload": {"tabId": tab["tabId"], "url": tab["url"], "title": tab["title"]},
        }))

    async def handle(self, msg):
        """Respond to a command like the real tab.js handlers do."""
        cmd = msg.get("type")
        payload = msg.get("payload") or {}
        msg_id = msg.get("messageId")
        self.received_urls.setdefault(cmd, []).append(payload.get("url"))

        def resp(type_, data):
            return {
                "type": type_,
                "messageId": str(uuid.uuid4()),
                "source": "extension",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "inResponseTo": msg_id,
                "payload": data,
            }

        if cmd == "ping":
            await self.ws.send(json.dumps(resp("pong", {"echo": payload})))
            return

        if cmd in ("tab_new", "tab_open"):
            self.tab_new_count += (cmd == "tab_new")
            self.tab_open_count += (cmd == "tab_open")
            url = payload.get("url") or "about:blank"
            tab = {
                "tabId": self.next_tab_id,
                "url": url,
                "title": "Python (programming language) - Wikipedia" if "wikipedia" in url else "Example Domain",
                "status": "loading",
                "active": True,
            }
            self.next_tab_id += 1
            for t in self.tabs:
                t["active"] = False
            self.tabs.append(tab)
            await self._simulate_navigation(tab)
            # Real URL only AFTER navigation completed (never "").
            await self.ws.send(json.dumps(resp("action_result", {
                "success": True, "tabId": tab["tabId"], "url": tab["url"],
            })))
            return

        if cmd == "tab_navigate":
            url = payload.get("url")
            tab = self._tab(payload.get("tabId")) or self._active_tab()
            if not tab:
                await self.ws.send(json.dumps(resp("action_result", {"success": False, "error": "No active tab"})))
                return
            tab["url"] = url
            tab["title"] = "Navigated"
            await self._simulate_navigation(tab)
            await self.ws.send(json.dumps(resp("action_result", {"success": True, "tabId": tab["tabId"], "url": tab["url"]})))
            return

        if cmd == "tab_list":
            await self.ws.send(json.dumps(resp("action_result", {
                "success": True,
                "tabs": [dict(t) for t in self.tabs],
                "count": len(self.tabs),
            })))
            return

        if cmd == "tab_switch":
            tab = self._tab(payload.get("tabId"))
            if not tab:
                await self.ws.send(json.dumps(resp("action_result", {"success": False, "error": "Unknown tab"})))
                return
            for t in self.tabs:
                t["active"] = (t["tabId"] == tab["tabId"])
            await self.ws.send(json.dumps(resp("action_result", {"success": True, "tabId": tab["tabId"]})))
            return

        if cmd == "tab_close":
            tab_id = payload.get("tabId")
            removed = self._tab(tab_id)
            if removed:
                self.tabs.remove(removed)
            await self.ws.send(json.dumps(resp("action_result", {"success": True, "tabId": tab_id})))
            return

        if cmd == "get_browser_state":
            await self.ws.send(json.dumps(resp("browser_state", {
                "success": True,
                "focusedWindowId": 1,
                "activeTab": dict(self._active_tab()) if self._active_tab() else None,
                "tabs": [dict(t) for t in self.tabs],
                "windows": [],
            })))
            return

        if cmd == "get_page_text":
            tab = self._active_tab()
            if not tab:
                await self.ws.send(json.dumps(resp("page_text", {"success": False, "error": "No active tab"})))
                return
            text = ("Python is a programming language. " * 10) if "wikipedia" in tab["url"] else "Example domain page."
            await self.ws.send(json.dumps(resp("page_text", {
                "url": tab["url"],
                "title": tab["title"],
                "text": text,
                "html": "<p>page</p>",
                "textLength": len(text),
                "htmlLength": 11,
                "metadata": {"description": "test"},
                "chunked": False,
            })))
            return

        await self.ws.send(json.dumps(resp("action_result", {"success": False, "error": f"No handler for {cmd}"})))

    def _active_tab(self):
        for t in self.tabs:
            if t.get("active"):
                return t
        return self.tabs[-1] if self.tabs else None


async def mock_extension_loop(ws, mock):
    mock.ws = ws
    try:
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), timeout=30.0))
            log.debug("[MOCK EXT] %s", data.get("type"))
            await mock.handle(data)
    except asyncio.TimeoutError:
        pass
    except websockets.ConnectionClosed:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

EXECUTOR = ThreadPoolExecutor(max_workers=1)


async def call_tool(tool, arguments):
    """Run a sync tool call without starving the async event loop
    (EdgeExtensionClient blocks on httpx for up to 30s)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(EXECUTOR, lambda: run_tool(tool, arguments))


async def run_workflow(mock, events_ws):
    """The exact desired agent sequence, driven through run_tool()."""

    # ---- 1. Open the Wikipedia page (must return the REAL url) ----
    ok, result = await call_tool("browser_open_tab", {"url": WIKI_URL, "label": "Wikipedia"})
    check("browser_open_tab success", ok and isinstance(result, dict) and result.get("success"),
          str(result)[:200])
    if isinstance(result, dict):
        check("browser_open_tab returns non-empty real url",
              bool(result.get("url")) and result["url"].startswith("https://en.wikipedia.org/"),
              f"url={result.get('url')!r}")
        check("browser_open_tab returns tabId", isinstance(result.get("tabId"), int),
              f"tabId={result.get('tabId')!r}")
        check("first open is NOT a reuse", not result.get("reused"), str(result)[:200])
        check("browser_open_tab command actually issued to extension",
              mock.tab_new_count == 1 and mock.received_urls.get("tab_new") == [WIKI_URL],
              f"tab_new_count={mock.tab_new_count}")
        check("response sent only AFTER navigation completed",
              mock._tab(result["tabId"]) is not None and mock._tab(result["tabId"])["status"] == "complete",
              f"status={mock._tab(result['tabId']) if result.get('tabId') else None}")
        wiki_tab_id = result["tabId"]

    # ---- 1b. navigation_completed push event was delivered ----
    try:
        evt = json.loads(await asyncio.wait_for(events_ws.recv(), timeout=3.0))
        check("navigation_completed event delivered with real url",
              evt.get("type") == "navigation_completed" and evt.get("payload", {}).get("url") == WIKI_URL,
              str(evt)[:200])
    except asyncio.TimeoutError:
        check("navigation_completed event delivered with real url", False, "event not received")

    # ---- 2. Wait for load (verified complete) ----
    ok, result = await call_tool("browser_wait_for_load", {"tab": "Wikipedia"})
    check("browser_wait_for_load success",
          ok and isinstance(result, dict) and result.get("success") and result.get("status") == "complete",
          str(result)[:200])

    # ---- 3. Read the page ----
    ok, result = await call_tool("browser_read_page", {})
    check("browser_read_page success",
          ok and isinstance(result, dict) and result.get("success"), str(result)[:200])
    if isinstance(result, dict):
        check("read_page returns url", result.get("url", "").startswith("https://en.wikipedia.org/"),
              f"url={result.get('url')!r}")
        check("read_page returns title", "Wikipedia" in (result.get("title") or ""),
              f"title={result.get('title')!r}")
        check("read_page returns text", "Python" in (result.get("text") or ""),
              f"text={str(result.get('text'))[:80]!r}")

    # ---- 4. browser_read(url=...) must NOT raise TypeError (contract fix) ----
    ok, result = await call_tool("browser_read", {"url": WIKI_URL})
    check("browser_read accepts url kwarg without TypeError",
          ok and isinstance(result, dict) and result.get("success"),
          f"ok={ok} result={str(result)[:120]}")
    if isinstance(result, dict):
        check("browser_read returns visible page text (not just title)",
              "Python" in (result.get("text") or ""),
              f"text={str(result.get('text'))[:80]!r}")
        check("browser_read returns title",
              "Wikipedia" in (result.get("title") or ""),
              f"title={result.get('title')!r}")
        check("browser_read returns url",
              (result.get("url") or "").startswith("https://en.wikipedia.org/"),
              f"url={result.get('url')!r}")
        check("browser_read reports truncation flag",
              isinstance(result.get("truncated"), bool), str(result)[:120])

    # ---- 4b. tabId argument alias: the model copies "tabId" from tool
    #          results into the next call; it must work, not TypeError ----
    ok, result = await call_tool("browser_read", {"tabId": wiki_tab_id})
    check("browser_read accepts tabId alias for tab",
          ok and isinstance(result, dict) and result.get("success")
          and "Python" in (result.get("text") or ""),
          f"ok={ok} result={str(result)[:120]}")
    ok, result = await call_tool("browser_vision", {"tabId": wiki_tab_id})
    check("browser_vision accepts tabId alias for tab (no TypeError)",
          ok and "unexpected keyword argument" not in str(result),
          f"ok={ok} result={str(result)[:120]}")

    # ---- 5. Duplicate open must reuse, not create a new tab ----
    ok, result = await call_tool("browser_open_tab", {"url": WIKI_URL, "label": "Wikipedia"})
    check("duplicate browser_open_tab still succeeds",
          ok and isinstance(result, dict) and result.get("success"), str(result)[:200])
    check("duplicate open reuses existing tab", isinstance(result, dict) and result.get("reused") is True,
          str(result)[:200])
    check("no second tab_new command issued",
          mock.tab_new_count == 1, f"tab_new_count={mock.tab_new_count}")

    # ---- 6. Open a second page, then close it by purpose label ----
    ok, result = await call_tool("browser_open_tab", {"url": EXAMPLE_URL, "label": "Example"})
    check("second page opens", ok and isinstance(result, dict) and result.get("success"), str(result)[:200])
    ok, result = await call_tool("browser_close_tab", {"tab": "Example"})
    check("browser_close_tab by label succeeds",
          ok and isinstance(result, dict) and result.get("success"), str(result)[:200])
    check("closed tab actually removed",
          all(t["url"] != EXAMPLE_URL for t in mock.tabs), str(mock.tabs)[:200])

    # ---- 7. Switch back by label and read again ----
    ok, result = await call_tool("browser_switch_tab", {"tab": "Wikipedia"})
    check("browser_switch_tab by label succeeds",
          ok and isinstance(result, dict) and result.get("success"), str(result)[:200])
    ok, result = await call_tool("browser_read_page_light", {})
    check("read_page_light still reads Wikipedia after switch",
          ok and isinstance(result, dict) and "wikipedia" in (result.get("url") or ""),
          str(result)[:200])

    # ---- 7b. Same-product host alias: chatgpt.com <-> chat.openai.com ----
    mock.tabs.append({"tabId": 999, "url": "https://chatgpt.com/", "title": "ChatGPT",
                      "status": "complete", "active": False})
    ok, result = await call_tool("browser_open_tab", {"url": "https://chat.openai.com/chat", "label": "ChatGPT"})
    check("open_tab reuses same-product tab across host alias",
          ok and isinstance(result, dict) and result.get("reused") is True
          and result.get("tabId") == 999 and mock.tab_new_count == 2,
          str(result)[:200])

    # ---- 7c. Root-URL request reuses a tab on the same host ----
    mock.tabs.append({"tabId": 998, "url": "https://www.youtube.com/watch?v=abc123", "title": "Video",
                      "status": "complete", "active": False})
    ok, result = await call_tool("browser_open_tab", {"url": "https://youtube.com", "label": "YouTube"})
    check("open_tab root URL reuses tab on same host",
          ok and isinstance(result, dict) and result.get("reused") is True
          and result.get("tabId") == 998 and mock.tab_new_count == 2,
          str(result)[:200])

    # ---- 8. Summarize (workflow completion) ----
    ok, result = await call_tool("browser_summarize_session", {"format": "text"})
    check("browser_summarize_session succeeds",
          ok and isinstance(result, dict) and result.get("success"), str(result)[:200])

    # ---- 9. Signature contract matrix (static) ----
    sig = inspect.signature(TOOLS["browser_read"])
    check("TOOLS[browser_read] accepts url",
          "url" in sig.parameters and "tab" in sig.parameters, str(sig))
    sig = inspect.signature(TOOLS["browser_open_tab"])
    check("TOOLS[browser_open_tab] accepts url,label",
          {"url", "label"} <= set(sig.parameters), str(sig))
    sig = inspect.signature(TOOLS["browser_goto"])
    check("TOOLS[browser_goto] accepts url,tab",
          {"url", "tab"} <= set(sig.parameters), str(sig))
    sig = inspect.signature(TOOLS["browser_wait_for_load"])
    check("TOOLS[browser_wait_for_load] accepts tab,timeout",
          {"tab", "timeout"} <= set(sig.parameters), str(sig))


async def main():
    global PASS, FAIL

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

    # Point the REAL singleton (used by run_tool) at the test server.
    browser_agent._ext = EdgeExtensionClient(base_url=HTTP_URL)
    # Fresh bookkeeping state for this run.
    browser_agent._opened_urls = {}
    browser_agent._tab_labels = {}

    mock = MockExtension()
    async with websockets.connect(WS_URL) as ws:
        reader = asyncio.create_task(mock_extension_loop(ws, mock))
        await asyncio.sleep(0.2)

        async with websockets.connect(WS_EVENTS_URL) as events_ws:
            await asyncio.sleep(0.2)
            await run_workflow(mock, events_ws)

        reader.cancel()

    print(f"\n{'='*50}")
    print(f"CONTRACT WORKFLOW RESULTS: {PASS} passed, {FAIL} failed")
    print(f"{'='*50}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

