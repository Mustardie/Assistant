/**
 * background.js
 *
 * Service worker entry point for the Nova Browser Bridge extension.
 * Why this exists: The manifest declares this as the service worker.
 * It initializes all modules and starts the connection to Nova.
 *
 * This file is the composition root — it wires together protocol,
 * connection, and all handlers without deep knowledge of any.
 *
 * MV3 Service Worker Lifecycle:
 * Chrome/Edge terminates service workers after ~30s of inactivity.
 * The Connection class sends periodic heartbeat messages to prevent
 * termination. We also log all lifecycle events here to diagnose
 * any remaining issues.
 */

import { Connection } from './core/connection.js';
import { MessageType, createMessage } from './core/protocol.js';
import { registerPingHandler } from './handlers/ping.js';
import { registerInfoHandler } from './handlers/info.js';
import { registerPageHandler } from './handlers/page.js';
import { registerTabHandlers } from './handlers/tab.js';
import { registerActionHandlers } from './handlers/action.js';
import { registerDOMHandlers } from './handlers/dom.js';
import { registerDownloadHandlers, startDownloadMonitoring } from './handlers/download.js';

// The WebSocket URL of the Nova FastAPI server.
const SERVER_URL = 'ws://127.0.0.1:8742/ws';

// Bump EXT_CODE_VERSION (AND the server's EXPECTED_EXTENSION_CODE_VERSION
// in backend/browser_server.py) whenever this extension's code changes.
// The bridge then hot-reloads the extension automatically -- the server
// sees the stale version in the heartbeat and sends `extension_reload`.
// No manual edge://extensions reload needed after the first rollout.
const EXT_CODE_VERSION = '10';

console.log('[Nova Bridge] Background service worker starting');

// ---- MV3 lifecycle logging ------------------------------------------------

chrome.runtime.onSuspend.addListener(() => {
  console.log('[Nova Bridge] LIFECYCLE: runtime.onSuspend — service worker about to be terminated');
});

chrome.runtime.onSuspendCanceled.addListener(() => {
  console.log('[Nova Bridge] LIFECYCLE: runtime.onSuspendCanceled — termination canceled');
});

// ---- Connection & handlers ------------------------------------------------

// Create the persistent connection to Nova's backend server
const connection = new Connection(SERVER_URL);
connection.codeVersion = EXT_CODE_VERSION;

// ---- MV3 keep-alive: the service worker can be terminated while idle,
// and once asleep NOTHING (not even its own setInterval heartbeat or
// reconnect timer) runs until the browser wakes it. Chrome/Edge always
// wake the worker to deliver chrome.alarms events, so a recurring alarm
// guarantees the worker comes back and re-joins the bridge even after
// Nova's server restarted hours ago. The WebSocket + heartbeat keep it
// alive once connected; the alarm is the wake-up-from-death mechanism.
const KEEPALIVE_ALARM = 'nova-bridge-keepalive';

chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 0.5 });

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== KEEPALIVE_ALARM) return;
  if (connection.isConnected) return; // heartbeat already keeps us alive
  console.log('[Nova Bridge] Keepalive alarm: service worker woke up, reconnecting');
  connection.connect();
});

// Woken by the content script on every page load -- wakes us even if the
// alarms were never created (e.g. worker killed before first keepalive tick).
chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== 'nova-wake') return;
  if (!connection.isConnected) {
    console.log('[Nova Bridge] Page-load wake: service worker woke up, reconnecting');
    connection.connect();
  }
});

chrome.runtime.onStartup.addListener(() => {
  console.log('[Nova Bridge] Browser startup -- connecting');
  connection.connect();
});

chrome.runtime.onInstalled.addListener((details) => {
  console.log(`[Nova Bridge] LIFECYCLE: runtime.onInstalled — reason=${details.reason}`);
  connection.connect();
});

// ---- Self-reload: Nova can order this extension to reload itself --------
// Fired by the bridge server when the loaded code version is older than
// the one on disk (after code changes). chrome.runtime.reload() restarts
// the service worker, which loads the NEW code from disk -- no manual
// edge://extensions reload needed.
let lastReloadTs = 0;
connection.on('extension_reload', (message) => {
  const now = Date.now();
  if (now - lastReloadTs < 15000) {
    console.warn('[Nova Bridge] extension_reload ignored (too soon after last reload)');
    return;
  }
  lastReloadTs = now;
  const reason = (message.payload || {}).reason || 'code update';
  console.log(`[Nova Bridge] Reloading extension: ${reason}`);
  chrome.runtime.reload();
});

// Register all message handlers
registerPingHandler(connection);
registerInfoHandler(connection);
registerPageHandler(connection);
registerTabHandlers(connection);
registerActionHandlers(connection);
registerDOMHandlers(connection);
registerDownloadHandlers(connection);

// Start download event monitoring (pushes notifications to Nova)
startDownloadMonitoring(connection);

// ---- Tab event listeners for push notifications ---------------------------
// These fire on ANY tab change and send events to Nova via the connection.
// Only send events when the connection is established.

function pushEvent(type, payload) {
  if (connection.isConnected) {
    const msg = createMessage(type, payload);
    connection.send(msg);
  }
}

chrome.tabs.onCreated.addListener((tab) => {
  pushEvent(MessageType.TAB_CREATED, {
    tabId: tab.id,
    url: tab.url || tab.pendingUrl,
    title: tab.title || '',
    index: tab.index,
    windowId: tab.windowId,
  });
});

chrome.tabs.onRemoved.addListener((tabId, removeInfo) => {
  pushEvent(MessageType.TAB_REMOVED, {
    tabId,
    windowId: removeInfo.windowId,
    isWindowClosing: removeInfo.isWindowClosing,
  });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  // Only push if URL or title actually changed (skip loading progress changes)
  if (changeInfo.url || changeInfo.title) {
    pushEvent(MessageType.TAB_UPDATED, {
      tabId,
      url: tab.url,
      title: tab.title,
      status: tab.status,
    });
  }
  // Push navigation_completed when a tab finishes loading a new URL
  if (changeInfo.status === 'complete' && tab.url && tab.url !== 'about:blank') {
    pushEvent(MessageType.NAVIGATION_COMPLETED, {
      tabId,
      url: tab.url,
      title: tab.title,
    });
  }
});

chrome.tabs.onActivated.addListener((activeInfo) => {
  pushEvent(MessageType.TAB_ACTIVATED, {
    tabId: activeInfo.tabId,
    windowId: activeInfo.windowId,
  });
});

// Connect to the server (reconnection is handled internally)
connection.connect();
