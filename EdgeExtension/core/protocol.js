/**
 * protocol.js
 *
 * Defines the message protocol between Nova (Python backend) and the Edge extension.
 * Every message follows the same structure for consistent routing and message correlation.
 *
 * Message format:
 * {
 *   type:        string  - Message type (e.g. "ping", "pong")
 *   messageId:   string  - UUID for correlation and request-response matching
 *   source:      string  - "jarvis" or "extension"
 *   timestamp:   string  - ISO 8601 timestamp of when the message was created
 *   inResponseTo: string | null  - messageId this message is responding to
 *   payload:     object  - Type-specific data
 * }
 */

export const MessageType = Object.freeze({
  // Phase 1: Communication
  PING: 'ping',
  PONG: 'pong',

  // Phase 2: Tab info
  GET_TAB_INFO: 'get_tab_info',
  TAB_INFO: 'tab_info',

  // Phase 3: Page text
  GET_PAGE_TEXT: 'get_page_text',
  PAGE_TEXT: 'page_text',

  // Phase 4: Tab management (Nova -> Ext)
  TAB_OPEN: 'tab_open',         // Open URL in NEW tab (chrome.tabs.create)
  TAB_NEW: 'tab_new',           // Create empty new tab
  TAB_NAVIGATE: 'tab_navigate', // Navigate CURRENT tab to URL (chrome.tabs.update)
  TAB_CLOSE: 'tab_close',
  TAB_SWITCH: 'tab_switch',
  TAB_RELOAD: 'tab_reload',
  TAB_BACK: 'tab_back',
  TAB_FORWARD: 'tab_forward',
  TAB_LIST: 'tab_list',

  // Browser state query (Nova -> Ext)
  GET_BROWSER_STATE: 'get_browser_state',
  BROWSER_STATE: 'browser_state',

  // Phase 4: Page interaction (Nova -> Ext)
  CLICK: 'click',
  DOUBLE_CLICK: 'double_click',
  RIGHT_CLICK: 'right_click',
  FOCUS: 'focus',
  SCROLL_TO: 'scroll_to',
  SCROLL_BY: 'scroll_by',
  TYPE_TEXT: 'type_text',
  CLEAR_INPUT: 'clear_input',
  PRESS_KEY: 'press_key',
  SELECT_OPTION: 'select_option',
  FIND_ELEMENT: 'find_element',
  FILL_FORM: 'fill_form',

  // Phase 4: DOM queries (Nova -> Ext)
  QUERY_SELECTOR: 'query_selector',
  QUERY_ALL: 'query_all',
  WAIT_FOR_SELECTOR: 'wait_for_selector',
  ELEMENT_EXISTS: 'element_exists',
  GET_ELEMENT_TEXT: 'get_element_text',
  GET_ELEMENT_ATTRS: 'get_element_attrs',
  GET_BOUNDING_BOX: 'get_bounding_box',

  // Phase 4: Generic result (Ext -> Nova)
  ACTION_RESULT: 'action_result',

  // Phase 5: Downloads - push events (Ext -> Nova)
  DOWNLOAD_CREATED: 'download_created',
  DOWNLOAD_CHANGED: 'download_changed',

  // Phase 5: Downloads - commands (Nova -> Ext)
  DOWNLOAD_LIST: 'download_list',
  DOWNLOAD_STATUS: 'download_status',
  DOWNLOAD_SEARCH: 'download_search',
  DOWNLOAD_CLEAR_CACHE: 'download_clear_cache',

  // Phase 5: Downloads - responses (Ext -> Nova)
  DOWNLOAD_RESULT: 'download_result',

  // Browser push events (Ext -> Nova)
  TAB_CREATED: 'tab_created',
  TAB_REMOVED: 'tab_removed',
  TAB_UPDATED: 'tab_updated',
  TAB_ACTIVATED: 'tab_activated',
  NAVIGATION_COMPLETED: 'navigation_completed',
});

/**
 * Creates a properly formatted message for the Nova-Extension protocol.
 * @param {string} type - A MessageType constant
 * @param {object} payload - Type-specific data
 * @param {string|null} [inResponseTo] - messageId this is in response to
 * @returns {object} A complete message object ready to send
 */
export function createMessage(type, payload = {}, inResponseTo = null) {
  const msg = {
    type,
    messageId: crypto.randomUUID(),
    source: 'extension',
    timestamp: new Date().toISOString(),
    inResponseTo,
    payload,
  };
  return msg;
}

/**
 * Creates a success result payload.
 * @param {object} data - Result data
 * @returns {object} { success: true, ...data }
 */
export function success(data = {}) {
  return { success: true, ...data };
}

/**
 * Creates a failure result payload.
 * @param {string} error - Error description
 * @returns {object} { success: false, error }
 */
export function failure(error) {
  return { success: false, error };
}
