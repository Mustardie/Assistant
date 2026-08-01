/**
 * dom.js
 *
 * Handles DOM query commands from Nova.
 * Why this exists: Nova needs to inspect page elements — checking existence,
 * reading text/attributes, getting bounding boxes, waiting for elements.
 *
 * All operations are performed by injecting scripts into the active tab.
 * Results are always returned with success/failure and structured data.
 */

import { MessageType, createMessage, success, failure } from '../core/protocol.js';

/**
 * Execute a function in the active tab and return its result.
 */
async function injectDOM(selector, func, extraArgs = []) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error('No active tab');
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func,
    args: [selector, ...extraArgs],
  });
  return result.result;
}

/**
 * Injected: query a single element and return basic info.
 */
function _injectQuerySelector(selector) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  return {
    success: true,
    tagName: el.tagName,
    id: el.id || null,
    className: el.className || null,
    textContent: (el.textContent || '').trim().substring(0, 5000),
    visible: el.offsetParent !== null,
    rect: el.getBoundingClientRect().toJSON(),
  };
}

/**
 * Injected: query all matching elements and return an array of basic info.
 */
function _injectQueryAll(selector) {
  const nodes = document.querySelectorAll(selector);
  const results = Array.from(nodes).map((el, i) => ({
    index: i,
    tagName: el.tagName,
    id: el.id || null,
    className: el.className || null,
    textContent: (el.textContent || '').trim().substring(0, 1000),
    visible: el.offsetParent !== null,
  }));
  return { success: true, count: results.length, elements: results };
}

/**
 * Injected: wait for a selector to appear with timeout.
 */
function _injectWaitForSelector(selector, timeoutMs) {
  return new Promise((resolve) => {
    if (document.querySelector(selector)) {
      resolve({ success: true, found: true });
      return;
    }
    const observer = new MutationObserver(() => {
      if (document.querySelector(selector)) {
        observer.disconnect();
        clearTimeout(timer);
        resolve({ success: true, found: true });
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    const timer = setTimeout(() => {
      observer.disconnect();
      resolve({ success: false, error: `Timeout waiting for: ${selector}` });
    }, timeoutMs);
  });
}

/**
 * Injected: check if an element exists.
 */
function _injectElementExists(selector) {
  const el = document.querySelector(selector);
  return { success: true, exists: !!el };
}

/**
 * Injected: get text content of an element.
 */
function _injectGetElementText(selector) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  return { success: true, text: (el.textContent || '').trim() };
}

/**
 * Injected: get attributes of an element.
 */
function _injectGetElementAttrs(selector) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  const attrs = {};
  for (const attr of el.attributes) {
    attrs[attr.name] = attr.value;
  }
  return { success: true, attributes: attrs };
}

/**
 * Injected: get bounding box of an element.
 */
function _injectGetBoundingBox(selector) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  return { success: true, rect: el.getBoundingClientRect().toJSON() };
}

// ---- Handler registration ----

/**
 * Register all DOM query handlers on a connection.
 * @param {Connection} connection - The active server connection
 */
export function registerDOMHandlers(connection) {
  connection.on(MessageType.QUERY_SELECTOR, async (message, conn) => {
    try {
      const result = await injectDOM(message.payload.selector, _injectQuerySelector);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.QUERY_ALL, async (message, conn) => {
    try {
      const result = await injectDOM(message.payload.selector, _injectQueryAll);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.WAIT_FOR_SELECTOR, async (message, conn) => {
    try {
      const timeout = message.payload.timeout || 10000;
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const [result] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: _injectWaitForSelector,
        args: [message.payload.selector, timeout],
      });
      conn.send(createMessage(MessageType.ACTION_RESULT, result.result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.ELEMENT_EXISTS, async (message, conn) => {
    try {
      const result = await injectDOM(message.payload.selector, _injectElementExists);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.GET_ELEMENT_TEXT, async (message, conn) => {
    try {
      const result = await injectDOM(message.payload.selector, _injectGetElementText);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.GET_ELEMENT_ATTRS, async (message, conn) => {
    try {
      const result = await injectDOM(message.payload.selector, _injectGetElementAttrs);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.GET_BOUNDING_BOX, async (message, conn) => {
    try {
      const result = await injectDOM(message.payload.selector, _injectGetBoundingBox);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });
}
