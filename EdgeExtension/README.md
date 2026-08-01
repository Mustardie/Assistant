# Nova Browser Bridge

A Microsoft Edge Manifest V3 extension that gives Nova direct access to the browser DOM, tabs, and page events. Replaces the legacy Playwright-based browser automation with native browser integration.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Nova (Python)                        │
│  ┌──────────┐  ┌────────────────────────────────────┐   │
│  │  Agent   │  │  browser_server.py (FastAPI)        │   │
│  │  (LLM)   │──┤  ┌─────────────┐ ┌──────────────┐ │   │
│  │          │  │  │ POST /api/  │ │ WS /ws        │ │   │
│  └──────────┘  │  │ send        │ │ (extension    │ │   │
│                │  │             │ │  connected)   │ │   │
│                │  └──────┬──────┘ └──────┬───────┘ │   │
│                └─────────┼───────────────┼─────────┘   │
└──────────────────────────┼───────────────┼─────────────┘
                           │ HTTP          │ WebSocket
                    ┌──────┴───────────────┴──────┐
                    │        Edge Extension        │
                    │  ┌────────────────────────┐  │
                    │  │  background.js          │  │
                    │  │  (service worker)       │  │
                    │  │  ┌──────┐ ┌──────────────────┐ │  │
                    │  │  │conn.│ │handlers/         │ │  │
                    │  │  │     │ │ping.js, info.js, │ │  │
                    │  │  └──────┘ │page.js, tab.js,  │ │  │
                    │  │           │action.js, dom.js,│ │  │
                    │  │           │download.js       │ │  │
                    │  │           └──────────────────┘ │  │
                    │  └────────────────────────────────┘  │
                    │         │                            │
                    │         ▼                            │
                    │  download events push to             │
                    │  WS /ws/events (Nova listens)        │
                    └──────────────────────────────────────┘
```

**Key design decisions:**

- **Extension connects to Nova** (not the other way around). Extensions cannot run TCP servers, so the extension initiates a WebSocket connection to the FastAPI bridge server.
- **Nova uses HTTP request-response**. The `/api/send` endpoint blocks until the extension responds, giving Nova a synchronous calling convention.
- **Push events use a separate WebSocket**. Nova connects to `/ws/events` to receive real-time push notifications (download started/completed) without polling.
- **Every command has a request ID**. Responses always match their corresponding request ID for safe concurrent execution.
- **Consistent result format**. Every action returns `{ success: true, ... }` or `{ success: false, error: "..." }`.
- **The FastAPI server is the bridge**. It translates between HTTP (Nova) and WebSocket (extension), handling message correlation via `messageId`/`inResponseTo`.

## Folder Structure

```
EdgeExtension/
├── manifest.json          # Extension manifest (MV3)
├── background.js          # Service worker entry point / composition root
├── core/
│   ├── protocol.js        # Message format constants and helpers
│   └── connection.js      # WebSocket connection with reconnection
├── handlers/
│   ├── ping.js            # PING → PONG handler (Phase 1)
│   ├── info.js            # Tab URL, title, favicon, selection (Phase 2)
│   ├── page.js            # DOM read, visible text, HTML (Phase 3)
│   ├── tab.js             # Tab management (Phase 4)
│   ├── action.js          # Page interaction: click, type, scroll (Phase 4)
│   ├── dom.js             # DOM queries: query, wait, exists (Phase 4)
│   └── download.js        # Download event push + commands (Phase 5)
├── tests/
│   ├── protocol_test.py   # Integration tests for the bridge protocol
│   └── extraction_test.py # Unit tests for DOM text extraction logic
└── README.md
```

**Python side:**

```
backend/
└── browser_server.py      # FastAPI WebSocket bridge server
                           # Endpoints: GET /health, POST /api/send,
                           #   WS /ws (extension), WS /ws/events (Nova events)
```

## Communication Protocol

Every message follows this structure:

```json
{
  "type":         "string",      // Message type identifier
  "messageId":    "string",      // UUID for correlation
  "source":       "string",      // "jarvis" or "extension"
  "timestamp":    "string",      // ISO 8601
  "inResponseTo": "string|null", // messageId being responded to
  "payload":      "object"       // Type-specific data
}
```

### Phase 1: Ping / Pong

```
Nova → Server → Extension:
  { "type": "ping", "messageId": "abc-123", "source": "jarvis", "payload": {} }

Extension → Server → Nova:
  { "type": "pong", "messageId": "def-456", "inResponseTo": "abc-123",
    "source": "extension", "payload": { "echo": {} } }
```

### Phase 2: Tab Info

```
Nova → Server → Extension:
  { "type": "get_tab_info", "messageId": "abc-123", "source": "jarvis", "payload": {} }

Extension → Server → Nova:
  { "type": "tab_info", "messageId": "def-456", "inResponseTo": "abc-123",
    "source": "extension",
    "payload": {
      "url": "https://example.com/page",
      "title": "Page Title",
      "favicon": "https://example.com/favicon.ico",
      "selectedText": "text the user has highlighted"
    } }
```

### Phase 3: Page Text

```
Nova → Server → Extension:
  { "type": "get_page_text", "messageId": "abc-123", "source": "jarvis", "payload": {} }

Extension → Server → Nova:
  { "type": "page_text", "messageId": "def-456", "inResponseTo": "abc-123",
    "source": "extension",
    "payload": {
      "url": "https://example.com/page",
      "title": "Page Title",
      "text": "Full visible text content of the page...",
      "html": "<html>...</html>",
      "textLength": 12345,
      "htmlLength": 67890,
      "metadata": {
        "description": "Meta description",
        "keywords": "keyword1, keyword2",
        "author": "Author name",
        "viewport": "width=device-width",
        "charset": "UTF-8",
        "ogTitle": "Open Graph title",
        "ogDescription": "Open Graph description",
        "ogImage": "https://...",
        "themeColor": "#ffffff"
      },
      "chunked": false
    } }
```

### Phase 4: Browser Control (Tab Management)

```
Nova → Ext: tab_open     { "url": "https://..." }
Nova → Ext: tab_new      { "url": "about:blank" }
Nova → Ext: tab_close    {}
Nova → Ext: tab_switch   { "tabId": 5 } or { "index": 2 }
Nova → Ext: tab_reload   {}
Nova → Ext: tab_back     {}
Nova → Ext: tab_forward  {}
Nova → Ext: tab_list     {}

Ext → Nova: action_result  { "success": true, "tabId": 5 }
                            or { "success": false, "error": "..." }
```

### Phase 4: Browser Control (Page Interaction)

```
Nova → Ext: click         { "selector": "#btn", "options": {} }
Nova → Ext: double_click  { "selector": "#btn" }
Nova → Ext: right_click   { "selector": "#btn" }
Nova → Ext: focus         { "selector": "#input" }
Nova → Ext: scroll_to     { "selector": "#footer" }
Nova → Ext: scroll_by     { "x": 0, "y": 500 }
Nova → Ext: type_text     { "selector": "#input", "text": "hello", "clear": true }
Nova → Ext: clear_input   { "selector": "#input" }
Nova → Ext: press_key     { "key": "Enter" } or { "selector": "#input", "key": "a" }
Nova → Ext: select_option { "selector": "#select", "value": "opt2" }
```

### Phase 4: Browser Control (DOM Queries)

```
Nova → Ext: query_selector      { "selector": "#main" }
  → { "success": true, "tagName": "DIV", "id": "main", "textContent": "...",
      "visible": true, "rect": { "x", "y", "width", "height" } }

Nova → Ext: query_all           { "selector": ".item" }
  → { "success": true, "count": 3, "elements": [...] }

Nova → Ext: wait_for_selector   { "selector": "#dynamic", "timeout": 5000 }
  → { "success": true, "found": true }

Nova → Ext: element_exists      { "selector": "#btn" }
  → { "success": true, "exists": true }

Nova → Ext: get_element_text    { "selector": "#title" }
  → { "success": true, "text": "Hello" }

Nova → Ext: get_element_attrs   { "selector": "#link" }
  → { "success": true, "attributes": { "href": "...", "class": "..." } }

Nova → Ext: get_bounding_box    { "selector": "#box" }
  → { "success": true, "rect": { "x", "y", "width", "height" } }
```

All DOM and page interaction commands respond with `action_result`.

### Phase 5: Downloads

Push events (Ext → Nova, delivered via WS /ws/events):

```
download_created  { "id", "filename", "url", "mime", "totalBytes", "state", "timestamp" }
download_changed  { "id", "state", "filename?", "receivedBytes?", "totalBytes?", "error?" }
```

Commands (Nova → Ext):

```
Nova → Ext: download_list         { "limit": 20 }
Nova → Ext: download_status       { "id": 5 }
Nova → Ext: download_search       { "query": "pdf" }
Nova → Ext: download_clear_cache  {}

Ext → Nova: download_result  { "success": true, "downloads": [...], "count": N }
                              or { "success": false, "error": "..." }
```

## Setup

### 1. Python dependencies

```bash
pip install fastapi uvicorn pydantic
```

### 2. Start the bridge server

```bash
python -m backend.browser_server
```

The server starts on `http://127.0.0.1:8742`. Verify:

```bash
curl http://127.0.0.1:8742/health
# → {"status":"ok","extension_connected":false,"pending_requests":0}
```

### 3. Load the extension in Edge

1. Open Edge and go to `edge://extensions/`
2. Enable **Developer mode** (toggle in bottom-left)
3. Click **Load unpacked**
4. Select the `EdgeExtension/` folder
5. The extension icon appears in the toolbar

### 4. Verify communication

```bash
curl -X POST http://127.0.0.1:8742/api/send \
  -H "Content-Type: application/json" \
  -d '{"type": "ping", "payload": {}}'
```

If the extension is loaded and connected, you'll receive a PONG response:

```json
{
  "type": "pong",
  "messageId": "...",
  "source": "extension",
  "inResponseTo": "...",
  "payload": { "echo": {} }
}
```

## Development Workflow

### Adding a new message handler

1. Create `handlers/<name>.js` with a `register<Name>Handler(connection)` function
2. Import and call it in `background.js`

Example:

```javascript
// handlers/echo.js
import { createMessage } from '../core/protocol.js';
export function registerEchoHandler(connection) {
  connection.on('echo', (msg, conn) => {
    conn.send(createMessage('echo_response', msg.payload, msg.messageId));
  });
}
```

```javascript
// background.js
import { registerEchoHandler } from './handlers/echo.js';
registerEchoHandler(connection);
```

### Testing

```bash
# Unit tests for the DOM extraction logic (no server needed)
python EdgeExtension/tests/extraction_test.py

# Integration tests for the bridge protocol (starts server automatically)
python EdgeExtension/tests/protocol_test.py
```

- All tests run via Python (no Node.js required)::
  ```bash
  curl -X POST http://127.0.0.1:8742/api/send \
    -H "Content-Type: application/json" \
    -d '{"type": "get_page_text"}'
  ```
- Check Edge's service worker console (`edge://extensions` → service worker link) for extension logs
- Watch the server logs for WebSocket activity

### Edge Extension debugging

- Open `edge://extensions/` and click "service worker" to open DevTools for the background script
- Use `console.log()` in extension code to debug
- The extension reconnects automatically if the server restarts

## Phase Plan

| Phase | Status | Description |
|-------|--------|-------------|
| 1     | ✅     | Communication layer (PING/PONG) |
| 2     | ✅     | Read tab URL, title, favicon, selection |
| 3     | ✅     | Read page text, HTML, DOM, metadata |
| 4     | ✅     | Browser control: tabs, click, type, scroll, DOM queries |
| 5     | ✅     | Download event push + list/status/search commands |

## Design Principles

- **Small files**: Each file has one responsibility
- **Comments**: Every file explains why it exists at the top
- **Minimal abstractions**: No unnecessary classes or patterns
- **Plain JavaScript**: No build step, no TypeScript, no bundler
- **Explicit wiring**: `background.js` is the composition root, imports are explicit
