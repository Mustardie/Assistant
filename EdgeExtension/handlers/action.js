/**
 * action.js
 *
 * Handles page interaction commands from Nova.
 * Why this exists: Nova needs to interact with web pages (click, type, scroll, etc.).
 * These commands inject scripts into the active tab to perform DOM operations.
 *
 * All injected functions receive the CSS selector and options via args[],
 * execute the action, and return success/failure with details.
 */

import { MessageType, createMessage, success, failure } from '../core/protocol.js';

/**
 * Execute a function in the active tab and return its result.
 * @param {string} selector - CSS selector for the target element
 * @param {Function} func - Function to execute in the page context, receives (selector, ...args)
 * @param {Array} [extraArgs] - Additional arguments passed to func after selector
 * @returns {Promise<object>} The result object from the injected function
 */
async function injectAction(selector, func, extraArgs = []) {
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
 * Injected: click an element.
 */
function _injectClick(selector, options) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  const opts = { bubbles: true, cancelable: true, ...options };
  if (opts.button === 'right') {
    el.dispatchEvent(new MouseEvent('contextmenu', opts));
  } else if (opts.dblclick) {
    el.dispatchEvent(new MouseEvent('dblclick', opts));
  } else {
    el.dispatchEvent(new MouseEvent('click', opts));
    el.focus();
  }
  return { success: true };
}

/**
 * Injected: focus an element.
 */
function _injectFocus(selector) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  el.focus();
  el.dispatchEvent(new Event('focus', { bubbles: true }));
  return { success: true };
}

/**
 * Injected: scroll element into view.
 */
function _injectScrollTo(selector) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  return { success: true, rect: el.getBoundingClientRect().toJSON() };
}

/**
 * Injected: scroll page by x, y.
 */
function _injectScrollBy(_, x, y) {
  window.scrollBy({ left: x, top: y, behavior: 'smooth' });
  return { success: true, scrollX: window.scrollX, scrollY: window.scrollY };
}

/**
 * Injected: clear an input.
 */
function _injectClearInput(selector) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  el.focus();
  el.value = '';
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { success: true };
}

/**
 * Injected: dispatch a keyboard event.
 */
function _injectPressKey(selector, key) {
  const el = selector ? document.querySelector(selector) : document.activeElement;
  if (selector && !el) return { success: false, error: `Element not found: ${selector}` };
  if (!el) return { success: false, error: 'No active element to type into' };
  el.focus();
  const eventOpts = { key, bubbles: true, cancelable: true, composed: true };
  el.dispatchEvent(new KeyboardEvent('keydown', eventOpts));
  el.dispatchEvent(new KeyboardEvent('keypress', eventOpts));
  el.dispatchEvent(new KeyboardEvent('keyup', eventOpts));
  return { success: true, key };
}

/**
 * Injected: select a dropdown option by value or label.
 */
function _injectSelectOption(selector, valueOrLabel) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  if (el.tagName !== 'SELECT') return { success: false, error: 'Element is not a SELECT' };

  // Try matching by value first, then by label text
  let found = false;
  for (const opt of el.options) {
    if (opt.value === valueOrLabel || opt.text === valueOrLabel) {
      opt.selected = true;
      found = true;
      break;
    }
  }
  if (!found) return { success: false, error: `Option not found: ${valueOrLabel}` };

  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { success: true, selectedValue: el.value };
}

/**
 * Injected: find elements by their visible text / aria-label / placeholder.
 * Assigns a stable `data-nova-target` id to every hit so later commands
 * (click, type) can target the element with `[data-nova-target="..."]`.
 * The description is normalized (case, quotes, whitespace) and matched
 * exactly first, then as a substring of the element's text.
 */
function _injectFindByText(description) {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const query = norm(description);
  if (!query) return { success: false, error: 'find_element requires a description' };

  const candidates = document.querySelectorAll(
    'button, a, input, textarea, select, label, [role="button"], [role="link"], ' +
    '[role="option"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], ' +
    '[role="checkbox"], [role="radio"], [role="tab"], [role="combobox"], ' +
    '[contenteditable="true"], [contenteditable=""], li[class]'
  );

  const hits = [];
  for (const el of candidates) {
    if (hits.length >= 8) break;
    const text = norm(el.textContent || '');
    const aria = norm(el.getAttribute('aria-label') || '');
    const placeholder = norm(el.getAttribute('placeholder') || '');
    const title = norm(el.getAttribute('title') || '');
    const name = norm(el.getAttribute('name') || '');
    const labelFor = el.tagName === 'LABEL' && el.htmlFor
      ? norm(document.getElementById(el.htmlFor)?.getAttribute('placeholder') || '')
      : '';
    if (text === query || aria === query || placeholder === query || title === query || name === query || labelFor === query) {
      // exact hit -- always keep
    } else if (query.length >= 3 && (text.includes(query) || aria.includes(query) || placeholder.includes(query) || title.includes(query))) {
      // substring hit on a SHORT element only, to avoid matching whole pages
      if (text.length > 200 && !aria && !placeholder) continue;
    } else {
      continue;
    }
    const visible = el.offsetParent !== null && el.getClientRects().length > 0;
    const target = `nova-${Date.now()}-${hits.length}-${Math.floor(Math.random() * 1e6)}`;
    el.setAttribute('data-nova-target', target);
    hits.push({
      selector: `[data-nova-target="${target}"]`,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || null,
      text: (el.textContent || '').trim().substring(0, 120),
      placeholder: el.getAttribute('placeholder') || null,
      visible,
      exact: text === query || aria === query || placeholder === query || title === query,
    });
  }

  if (hits.length === 0) {
    return { success: false, error: `No element matches: ${description}` };
  }
  const visible = hits.filter((h) => h.visible);
  const bestPool = visible.length ? visible : hits;
  bestPool.sort((a, b) => (b.exact ? 1 : 0) - (a.exact ? 1 : 0) || a.text.length - b.text.length);
  return {
    success: true,
    count: hits.length,
    elements: hits,
    best: bestPool[0],
  };
}

/**
 * Injected: type text into an input, textarea, or contenteditable.
 * Plain inputs/textareas use the native value setter (so React/Vue
 * controlled inputs actually see the change) and dispatch input/change
 * events. Contenteditable editors (Google Docs, Notion, WordPress)
 * get beforeinput + insertText via execCommand, which rich editors
 * treat as real input. When `keyboard` is true, per-character keydown/
 * keypress events with real keyCodes are dispatched instead -- some
 * editors (e.g. Google Docs' Kix) only react to keyboard events.
 */
function _injectTypeText(selector, text, shouldClear, keyboard) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: `Element not found: ${selector}` };
  el.focus();

  const isContentEditable = el.isContentEditable || el.getAttribute('contenteditable') === 'true';
  if (isContentEditable) {
    if (shouldClear) {
      el.textContent = '';
    }
    if (keyboard) {
      for (const ch of text) {
        const keyCode = ch.length === 1 ? ch.toUpperCase().charCodeAt(0) : 0;
        const base = { key: ch, code: ch, bubbles: true, cancelable: true, composed: true };
        el.dispatchEvent(new KeyboardEvent('keydown', { ...base, keyCode }));
        el.dispatchEvent(new KeyboardEvent('keypress', { ...base, keyCode, charCode: ch.charCodeAt(0) }));
        el.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: ch }));
        el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: ch }));
        el.dispatchEvent(new KeyboardEvent('keyup', { ...base, keyCode }));
      }
      return { success: true, mode: 'keyboard', typed: text.length };
    }
    const before = el.textContent || '';
    const ok = document.execCommand('insertText', false, text);
    const after = el.textContent || '';
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    return {
      success: true,
      mode: 'execCommand',
      execCommandOk: ok,
      inserted: after.length - before.length,
    };
  }

  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
    if (shouldClear) {
      el.value = '';
    }
    // React tracks input values through a value setter on the prototype;
    // setting .value directly is silently ignored by React.
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    if (setter) {
      setter.call(el, text);
    } else {
      el.value = text;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { success: true, mode: 'input', value: el.value };
  }

  return { success: false, error: `Cannot type into <${el.tagName.toLowerCase()}>` };
}

/**
 * Injected: fill a form from a list of semantic fields. Each field is
 * {field: "email"|"password"|placeholder/label text, value: "..."}.
 * For every field the most specific input/textarea/select (by name, id,
 * type, aria-label, placeholder, or associated label text) receives the
 * value with proper events.
 */
function _injectFillForm(fields) {
  if (!Array.isArray(fields) || fields.length === 0) {
    return { success: false, error: 'fill_form requires a non-empty fields array' };
  }
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const controls = document.querySelectorAll(
    'input, textarea, select, [contenteditable="true"], [contenteditable=""]'
  );
  const results = [];
  let filled = 0;

  const visible = (el) => el.offsetParent !== null || el.getClientRects().length > 0;

  for (const field of fields) {
    const label = norm(field.field);
    const value = String(field.value ?? '');
    let target = null;

    for (const el of controls) {
      if (!visible(el)) continue;
      const elName = norm(el.getAttribute('name') || '');
      const elId = norm(el.id || '');
      const elType = norm(el.getAttribute('type') || el.tagName.toLowerCase());
      const elAria = norm(el.getAttribute('aria-label') || '');
      const elPlaceholder = norm(el.getAttribute('placeholder') || '');
      let elLabelText = '';
      if (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) {
        elLabelText = norm(document.querySelector(`label[for="${CSS.escape(el.id)}"]`).textContent || '');
      }
      const hits = [elName, elId, elType, elAria, elPlaceholder, elLabelText];
      if (hits.includes(label) || hits.some((h) => h && h.includes(label))) {
        target = el;
        break;
      }
    }

    if (!target) {
      results.push({ field: field.field, success: false, error: 'No matching field found' });
      continue;
    }

    target.focus();
    const isContentEditable = target.isContentEditable || target.getAttribute('contenteditable') === 'true';
    if (target.tagName === 'SELECT') {
      for (const opt of target.options) {
        if (norm(opt.text) === norm(value) || opt.value === value) {
          opt.selected = true;
          target.dispatchEvent(new Event('change', { bubbles: true }));
          results.push({ field: field.field, success: true, value: target.value });
          filled += 1;
          break;
        }
      }
      if (!results.length || !results[results.length - 1].success) {
        results.push({ field: field.field, success: false, error: `Option not found: ${value}` });
      }
      continue;
    }
    if (isContentEditable) {
      if (target.textContent) target.textContent = '';
      document.execCommand('insertText', false, value);
      target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
    } else {
      const proto = target.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      if (setter) setter.call(target, value);
      else target.value = value;
      target.dispatchEvent(new Event('input', { bubbles: true }));
      target.dispatchEvent(new Event('change', { bubbles: true }));
    }
    results.push({ field: field.field, success: true, value: value.substring(0, 60) });
    filled += 1;
  }

  return { success: filled > 0, filled, results };
}

// ---- Handler registration ----

/**
 * Register all page interaction handlers on a connection.
 * @param {Connection} connection - The active server connection
 */
export function registerActionHandlers(connection) {
  connection.on(MessageType.CLICK, async (message, conn) => {
    try {
      const result = await injectAction(message.payload.selector, _injectClick, [message.payload.options || {}]);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.DOUBLE_CLICK, async (message, conn) => {
    try {
      const result = await injectAction(message.payload.selector, _injectClick, [{ dblclick: true }]);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.RIGHT_CLICK, async (message, conn) => {
    try {
      const result = await injectAction(message.payload.selector, _injectClick, [{ button: 'right' }]);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.FOCUS, async (message, conn) => {
    try {
      const result = await injectAction(message.payload.selector, _injectFocus);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.SCROLL_TO, async (message, conn) => {
    try {
      const result = await injectAction(message.payload.selector, _injectScrollTo);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.SCROLL_BY, async (message, conn) => {
    try {
      const x = message.payload.x || 0;
      const y = message.payload.y || 0;
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const [result] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: _injectScrollBy,
        args: [null, x, y],
      });
      conn.send(createMessage(MessageType.ACTION_RESULT, result.result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.TYPE_TEXT, async (message, conn) => {
    try {
      const text = message.payload.text || '';
      const shouldClear = message.payload.clear === true;
      const keyboard = message.payload.keyboard === true;
      const result = await injectAction(message.payload.selector, _injectTypeText, [text, shouldClear, keyboard]);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.CLEAR_INPUT, async (message, conn) => {
    try {
      const result = await injectAction(message.payload.selector, _injectClearInput);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.PRESS_KEY, async (message, conn) => {
    try {
      const key = message.payload.key || '';
      if (!key) {
        conn.send(createMessage(MessageType.ACTION_RESULT, failure('key is required'), message.messageId));
        return;
      }
      const selector = message.payload.selector || null;
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const [result] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: _injectPressKey,
        args: [selector, key],
      });
      conn.send(createMessage(MessageType.ACTION_RESULT, result.result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.SELECT_OPTION, async (message, conn) => {
    try {
      const value = message.payload.value || message.payload.label;
      if (!value) {
        conn.send(createMessage(MessageType.ACTION_RESULT, failure('value or label is required'), message.messageId));
        return;
      }
      const result = await injectAction(message.payload.selector, _injectSelectOption, [value]);
      conn.send(createMessage(MessageType.ACTION_RESULT, result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.FIND_ELEMENT, async (message, conn) => {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const [result] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: _injectFindByText,
        args: [message.payload.description],
      });
      conn.send(createMessage(MessageType.ACTION_RESULT, result.result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });

  connection.on(MessageType.FILL_FORM, async (message, conn) => {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const [result] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: _injectFillForm,
        args: [message.payload.fields || []],
      });
      conn.send(createMessage(MessageType.ACTION_RESULT, result.result, message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.ACTION_RESULT, failure(err.message), message.messageId));
    }
  });
}
