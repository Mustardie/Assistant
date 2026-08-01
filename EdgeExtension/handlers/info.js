/**
 * info.js
 *
 * Handles get_tab_info from Nova.
 * Why this exists: Nova needs to know what page the user is looking at.
 * This handler queries the active tab and returns URL, title, favicon,
 * and any text the user has selected on the page.
 *
 * Uses chrome.tabs.query for tab metadata and chrome.scripting.executeScript
 * to read the selected text from the page DOM.
 */

import { MessageType, createMessage } from '../core/protocol.js';

/**
 * Register the tab info handler on a connection.
 * @param {Connection} connection - The active server connection
 */
export function registerInfoHandler(connection) {
  connection.on(MessageType.GET_TAB_INFO, async (message, conn) => {
    try {
      const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tabs || tabs.length === 0) {
        conn.send(createMessage('error', { message: 'No active tab found' }, message.messageId));
        return;
      }

      const tab = tabs[0];

      // Get selected text via injected script
      let selectedText = '';
      try {
        const [result] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: () => window.getSelection().toString(),
        });
        selectedText = result.result || '';
      } catch (e) {
        // scripting may fail on restricted pages (chrome://, edge://, etc.)
        selectedText = '';
      }

      conn.send(createMessage(MessageType.TAB_INFO, {
        url: tab.url || '',
        title: tab.title || '',
        favicon: tab.favIconUrl || '',
        selectedText: selectedText,
      }, message.messageId));

    } catch (err) {
      conn.send(createMessage('error', { message: err.message }, message.messageId));
    }
  });
}
