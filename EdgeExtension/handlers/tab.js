/**
 * tab.js
 *
 * Handles tab management commands from Nova.
 * Why this exists: Nova needs to control browser tabs (open, close, switch, navigate).
 * All operations use the chrome.tabs API directly from the service worker.
 * No DOM injection needed for tab-level commands.
 *
 * Key design:
 * - TAB_OPEN always creates a NEW tab (chrome.tabs.create) — never overwrites
 *   the user's current page.
 * - TAB_NAVIGATE updates the CURRENT tab's URL — use only when explicitly
 *   requested to replace the current page.
 * - TAB_NEW creates a new tab, navigating it to the given URL (about:blank
 *   when no URL is provided).
 * - Navigation commands (open/new/navigate) WAIT for the tab to finish
 *   loading (chrome.tabs.onUpdated status === 'complete') before responding.
 *   chrome.tabs.create() resolves immediately, while the tab's `url` is
 *   still empty and the real destination sits in `pendingUrl` — responding
 *   right away used to hand Nova an empty URL and let it read a half-loaded
 *   page. Waiting means Nova always gets the real, final URL and never
 *   reads before the page is ready.
 */

import { MessageType, createMessage, success, failure } from '../core/protocol.js';

const NAVIGATION_TIMEOUT_MS = 15000;

/**
 * Wait until the tab finishes loading or the timeout elapses.
 * Resolves with the tab object (or null when the tab is gone).
 */
function waitForTabLoad(tabId, timeoutMs = NAVIGATION_TIMEOUT_MS) {
  return new Promise((resolve) => {
    let settled = false;

    const finish = (tab) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      resolve(tab || null);
    };

    const onUpdated = (id, changeInfo, tab) => {
      if (id === tabId && changeInfo.status === 'complete') {
        finish(tab);
      }
    };

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(onUpdated);
      chrome.tabs.get(tabId, (tab) => {
        if (chrome.runtime.lastError) {
          resolve(null);
        } else {
          resolve(tab || null);
        }
      });
    }, timeoutMs);

    // Already loaded (e.g. about:blank or a fast cache hit)?
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError || (tab && tab.status === 'complete')) {
        finish(tab || null);
      }
    });

    chrome.tabs.onUpdated.addListener(onUpdated);
  });
}

/**
 * Resolve the tabId a payload refers to: payload.tabId if present,
 * otherwise the active tab of the current window. Resolves to null
 * when no tab matches.
 */
async function resolveTargetTabId(payload) {
  if (payload && payload.tabId !== undefined && payload.tabId !== null) {
    return payload.tabId;
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab ? tab.id : null;
}

/**
 * Register all tab management handlers on a connection.
 * @param {Connection} connection - The active server connection
 */
export function registerTabHandlers(connection) {
  // -- Tab open: create a NEW tab and navigate to URL --
  // NEVER overwrites the current tab. Use tab_navigate for that.
  connection.on(MessageType.TAB_OPEN, async (message, conn) => {
    try {
      const url = message.payload.url;
      if (!url) {
        conn.send(createMessage(MessageType.ACTION_RESULT, failure('url is required'), message.messageId));
        return;
      }
      const tab = await chrome.tabs.create({ url, active: true });
      const loaded = await waitForTabLoad(tab.id);
      const finalUrl = (loaded && loaded.url) || tab.pendingUrl || url;
      conn.send(createMessage(MessageType.ACTION_RESULT,
        success({ tabId: tab.id, url: finalUrl }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Tab navigate: update CURRENT tab's URL (replaces current page) --
  // Use this only when the user explicitly wants to navigate the current tab.
  connection.on(MessageType.TAB_NAVIGATE, async (message, conn) => {
    try {
      const url = message.payload.url;
      if (!url) {
        conn.send(createMessage(MessageType.ACTION_RESULT, failure('url is required'), message.messageId));
        return;
      }
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab) {
        conn.send(createMessage(MessageType.ACTION_RESULT, failure('No active tab'), message.messageId));
        return;
      }
      await chrome.tabs.update(tab.id, { url });
      const loaded = await waitForTabLoad(tab.id);
      const finalUrl = (loaded && loaded.url) || url;
      conn.send(createMessage(MessageType.ACTION_RESULT,
        success({ tabId: tab.id, url: finalUrl }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Tab new (create a new tab, optionally navigating it to url) --
  connection.on(MessageType.TAB_NEW, async (message, conn) => {
    try {
      const url = message.payload.url || 'about:blank';
      const tab = await chrome.tabs.create({ url });
      const loaded = await waitForTabLoad(tab.id);
      const finalUrl = (loaded && loaded.url) || tab.pendingUrl || url;
      conn.send(createMessage(MessageType.ACTION_RESULT,
        success({ tabId: tab.id, url: finalUrl }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Tab close --
  connection.on(MessageType.TAB_CLOSE, async (message, conn) => {
    try {
      const tabId = await resolveTargetTabId(message.payload);
      if (tabId === null) {
        conn.send(createMessage(MessageType.ACTION_RESULT, failure('No active tab'), message.messageId));
        return;
      }
      await chrome.tabs.remove(tabId);
      conn.send(createMessage(MessageType.ACTION_RESULT, success({ tabId }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Tab switch --
  connection.on(MessageType.TAB_SWITCH, async (message, conn) => {
    try {
      let tabId = message.payload.tabId;
      if (tabId === undefined || tabId === null) {
        const tabs = await chrome.tabs.query({ currentWindow: true });
        const index = message.payload.index;
        if (index === undefined || index < 0 || index >= tabs.length) {
          conn.send(createMessage(MessageType.ACTION_RESULT, failure(`Invalid tab index: ${index}`), message.messageId));
          return;
        }
        tabId = tabs[index].id;
      }
      await chrome.tabs.update(tabId, { active: true });
      conn.send(createMessage(MessageType.ACTION_RESULT, success({ tabId }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Tab reload --
  connection.on(MessageType.TAB_RELOAD, async (message, conn) => {
    try {
      const tabId = await resolveTargetTabId(message.payload);
      if (tabId === null) {
        conn.send(createMessage(MessageType.ACTION_RESULT, failure('No active tab'), message.messageId));
        return;
      }
      await chrome.tabs.reload(tabId);
      const loaded = await waitForTabLoad(tabId);
      const finalUrl = (loaded && loaded.url) || '';
      conn.send(createMessage(MessageType.ACTION_RESULT,
        success({ tabId, url: finalUrl }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Tab back --
  connection.on(MessageType.TAB_BACK, async (message, conn) => {
    try {
      const tabId = await resolveTargetTabId(message.payload);
      if (tabId === null) {
        conn.send(createMessage(MessageType.ACTION_RESULT, failure('No active tab'), message.messageId));
        return;
      }
      await chrome.tabs.goBack(tabId);
      const loaded = await waitForTabLoad(tabId);
      const finalUrl = (loaded && loaded.url) || '';
      conn.send(createMessage(MessageType.ACTION_RESULT,
        success({ tabId, url: finalUrl }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Tab forward --
  connection.on(MessageType.TAB_FORWARD, async (message, conn) => {
    try {
      const tabId = await resolveTargetTabId(message.payload);
      if (tabId === null) {
        conn.send(createMessage(MessageType.ACTION_RESULT, failure('No active tab'), message.messageId));
        return;
      }
      await chrome.tabs.goForward(tabId);
      const loaded = await waitForTabLoad(tabId);
      const finalUrl = (loaded && loaded.url) || '';
      conn.send(createMessage(MessageType.ACTION_RESULT,
        success({ tabId, url: finalUrl }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Tab list --
  connection.on(MessageType.TAB_LIST, async (message, conn) => {
    try {
      const tabs = await chrome.tabs.query({ currentWindow: true });
      const list = tabs.map((t, i) => ({
        tabId: t.id,
        index: i,
        url: t.url,
        title: t.title,
        active: t.active,
      }));
      conn.send(createMessage(MessageType.ACTION_RESULT, success({ tabs: list, count: list.length }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Get browser state (lightweight snapshot, no DOM injection needed) --
  connection.on(MessageType.GET_BROWSER_STATE, async (message, conn) => {
    try {
      const windows = await chrome.windows.getAll({ populate: true });
      const state = {
        focusedWindowId: null,
        activeTab: null,
        tabs: [],
        windows: [],
      };

      for (const win of windows) {
        const winInfo = {
          windowId: win.id,
          focused: win.focused,
          tabs: [],
        };
        if (win.focused) {
          state.focusedWindowId = win.id;
        }
        for (const t of (win.tabs || [])) {
          const tabInfo = {
            tabId: t.id,
            index: t.index,
            url: t.url,
            title: t.title,
            active: t.active,
            windowId: win.id,
            status: t.status,
          };
          winInfo.tabs.push(tabInfo);
          state.tabs.push(tabInfo);
          if (t.active && win.focused) {
            state.activeTab = tabInfo;
          }
        }
        state.windows.push(winInfo);
      }

      conn.send(createMessage(MessageType.BROWSER_STATE, success(state), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.BROWSER_STATE, failure(err.message), message.messageId));
    }
  });
}
