/**
 * page.js
 *
 * Handles get_page_text from Nova.
 * Why this exists: Nova needs to read the current webpage to understand context.
 *
 * Architecture:
 *   Nova -> WebSocket -> background.js -> page.js
 *     -> chrome.scripting.executeScript() -> injected function in tab
 *     -> chrome.runtime.sendMessage() chunks back to background
 *     -> background assembles chunks -> WebSocket -> Nova
 *
 * Uses executeScript (not a pre-registered content script) so it works
 * immediately on any tab, even ones loaded before the extension started.
 * Large pages are handled by chunking the response into manageable pieces.
 */

import { MessageType, createMessage } from '../core/protocol.js';

// Maps requestId -> { chunks: [], resolve, reject, timer }
const _pendingExtractions = new Map();

/**
 * Listen for chunked responses from injected page-reader scripts.
 * This listener lives for the lifetime of the service worker.
 */
chrome.runtime.onMessage.addListener((message) => {
  if (message.type !== 'page_extraction_chunk') return;

  const pending = _pendingExtractions.get(message.requestId);
  if (!pending) return;

  pending.chunks[message.chunkIndex] = message;

  if (message.chunkIndex === message.totalChunks - 1) {
    clearTimeout(pending.timer);
    _pendingExtractions.delete(message.requestId);
    pending.resolve(_assembleChunks(pending.chunks));
  }
});

/**
 * Assemble chunked response data into a single result object.
 */
function _assembleChunks(chunks) {
  const sorted = chunks.slice().sort((a, b) => a.chunkIndex - b.chunkIndex);
  const first = sorted[0];

  return {
    url: first.url || '',
    title: first.title || '',
    text: sorted.map(c => c.text || '').join(''),
    html: sorted.map(c => c.html || '').join(''),
    textLength: first.textLength || 0,
    htmlLength: first.htmlLength || 0,
    metadata: first.metadata || {},
    chunked: sorted.length > 1,
  };
}

/**
 * Inject the page reader into the active tab and wait for the result.
 * Returns the assembled page data.
 */
function _extractPage(tabId, requestId) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      _pendingExtractions.delete(requestId);
      reject(new Error('Page extraction timed out after 30s'));
    }, 30000);

    _pendingExtractions.set(requestId, { chunks: [], resolve, reject, timer });

    chrome.scripting.executeScript({
      target: { tabId },
      func: _pageReaderMain,
      args: [requestId],
    }).catch(err => {
      clearTimeout(timer);
      _pendingExtractions.delete(requestId);
      reject(new Error(`Script injection failed: ${err.message}`));
    });
  });
}

/**
 * Register the page text handler on a connection.
 * @param {Connection} connection - The active server connection
 */
export function registerPageHandler(connection) {
  connection.on(MessageType.GET_PAGE_TEXT, async (message, conn) => {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab) {
        conn.send(createMessage('error', { message: 'No active tab' }, message.messageId));
        return;
      }

      const requestId = crypto.randomUUID();
      const result = await _extractPage(tab.id, requestId);

      conn.send(createMessage(MessageType.PAGE_TEXT, result, message.messageId));

    } catch (err) {
      conn.send(createMessage('error', { message: err.message }, message.messageId));
    }
  });
}

// ---------------------------------------------------------------------------
// The function below is serialized via executeScript and rehydrated in the
// page's isolated world. It cannot reference any outer scope variables
// except through its arguments.
// ---------------------------------------------------------------------------

/**
 * Injected into the target tab. Extracts visible text, HTML, and metadata
 * from the page, then sends the data back in chunks via runtime.sendMessage.
 *
 * Chunking prevents structured-clone size limits from breaking on very
 * large pages. Each chunk carries a portion of the text and HTML.
 */
function _pageReaderMain(requestId) {
  const CHUNK_SIZE = 256 * 1024; // 256KB per chunk (text + html combined)

  // ---- extraction helpers ----

  function getVisibleText() {
    const hiddenTags = new Set([
      'SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'PATH',
      'META', 'LINK', 'TITLE', 'HEAD', 'IFRAME',
    ]);

    function isHidden(el) {
      if (el.hidden) return true;
      if (el.getAttribute('aria-hidden') === 'true') return true;
      const style = window.getComputedStyle(el);
      if (style.display === 'none') return true;
      if (style.visibility === 'hidden') return true;
      return false;
    }

    const parts = [];
    const walker = document.createTreeWalker(
      document.body,
      NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          if (node.nodeType === Node.ELEMENT_NODE) {
            if (hiddenTags.has(node.tagName)) return NodeFilter.FILTER_REJECT;
            if (isHidden(node)) return NodeFilter.FILTER_REJECT;
          }
          if (node.nodeType === Node.TEXT_NODE) {
            if (!node.textContent.trim()) return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      },
      false
    );

    while (walker.nextNode()) {
      if (walker.currentNode.nodeType === Node.TEXT_NODE) {
        parts.push(walker.currentNode.textContent.trim());
      }
    }

    return parts.join('\n').replace(/\n{4,}/g, '\n\n\n').trim();
  }

  function getMetadata() {
    const meta = {};
    document.querySelectorAll('meta').forEach(el => {
      const key = el.getAttribute('name') || el.getAttribute('property') || '';
      const content = el.getAttribute('content') || '';
      if (key && content) meta[key] = content;
    });

    return {
      description: meta.description || meta['og:description'] || '',
      keywords: meta.keywords || '',
      author: meta.author || '',
      viewport: meta.viewport || '',
      charset: document.characterSet || '',
      ogTitle: meta['og:title'] || '',
      ogDescription: meta['og:description'] || '',
      ogImage: meta['og:image'] || '',
      ogUrl: meta['og:url'] || '',
      twitterCard: meta['twitter:card'] || '',
      themeColor: meta['theme-color'] || '',
    };
  }

  // ---- chunked delivery ----

  const data = {
    url: location.href,
    title: document.title,
    text: getVisibleText(),
    html: document.documentElement.outerHTML,
    metadata: getMetadata(),
    textLength: 0,
    htmlLength: 0,
  };
  data.textLength = data.text.length;
  data.htmlLength = data.html.length;

  const combinedSize = data.textLength + data.htmlLength;
  const numChunks = Math.max(1, Math.ceil(combinedSize / CHUNK_SIZE));

  for (let i = 0; i < numChunks; i++) {
    const textStart = Math.floor(i * data.textLength / numChunks);
    const textEnd = Math.floor((i + 1) * data.textLength / numChunks);
    const htmlStart = Math.floor(i * data.htmlLength / numChunks);
    const htmlEnd = Math.floor((i + 1) * data.htmlLength / numChunks);

    chrome.runtime.sendMessage({
      type: 'page_extraction_chunk',
      requestId,
      chunkIndex: i,
      totalChunks: numChunks,
      text: data.text.substring(textStart, textEnd),
      html: data.html.substring(htmlStart, htmlEnd),
      // metadata and fixed fields only in the first chunk
      url: i === 0 ? data.url : undefined,
      title: i === 0 ? data.title : undefined,
      metadata: i === 0 ? data.metadata : undefined,
      textLength: i === 0 ? data.textLength : undefined,
      htmlLength: i === 0 ? data.htmlLength : undefined,
    });
  }
}
