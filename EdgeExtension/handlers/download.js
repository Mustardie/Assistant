/**
 * download.js
 *
 * Handles download monitoring and commands for Nova.
 * Why this exists: Nova needs to be aware of browser downloads — when they
 * start, progress, complete, or fail. The extension monitors chrome.downloads
 * events and pushes notifications to Nova via the WebSocket.
 *
 * Push events are sent as unsolicited messages (no inResponseTo). The server
 * broadcasts them to Nova's event WebSocket (/ws/events).
 *
 * Commands like listing recent downloads, checking status, and searching
 * follow the standard request-response pattern.
 */

import { MessageType, createMessage, success, failure } from '../core/protocol.js';

// In-memory cache of recent downloads for search/list commands.
// The extension does NOT own the download history - chrome.downloads does.
// This cache is for convenience lookups and can be cleared.
let _downloadCache = [];
const MAX_CACHED = 100;

/**
 * Start monitoring download events.
 * Registers chrome.downloads listeners and forwards events to Nova.
 * @param {Connection} connection - The active server connection
 */
export function startDownloadMonitoring(connection) {
  // -- Listen for new downloads --
  if (chrome.downloads && chrome.downloads.onCreated) {
    chrome.downloads.onCreated.addListener((downloadItem) => {
      const event = {
        id: downloadItem.id,
        filename: downloadItem.filename,
        url: downloadItem.url,
        mime: downloadItem.mime || '',
        totalBytes: downloadItem.totalBytes || 0,
        state: downloadItem.state || 'in_progress',
        timestamp: new Date(downloadItem.startTime || Date.now()).toISOString(),
      };

      _cacheDownload(event);

      connection.send(createMessage(MessageType.DOWNLOAD_CREATED, event));
    });
  }

  // -- Listen for download state changes --
  if (chrome.downloads && chrome.downloads.onChanged) {
    chrome.downloads.onChanged.addListener((downloadDelta) => {
      const update = { id: downloadDelta.id };
      if (downloadDelta.state) {
        update.state = downloadDelta.state.current;
      }
      if (downloadDelta.filename) {
        update.filename = downloadDelta.filename.current;
      }
      if (downloadDelta.mime) {
        update.mime = downloadDelta.mime.current;
      }
      if (downloadDelta.received) {
        update.receivedBytes = downloadDelta.received.current;
      }
      if (downloadDelta.totalBytes) {
        update.totalBytes = downloadDelta.totalBytes.current;
      }
      if (downloadDelta.error) {
        update.error = downloadDelta.error.current;
      }
      update.timestamp = new Date().toISOString();

      // Update cache
      const cached = _downloadCache.find(d => d.id === downloadDelta.id);
      if (cached) Object.assign(cached, update);

      connection.send(createMessage(MessageType.DOWNLOAD_CHANGED, update));
    });
  }
}

function _cacheDownload(item) {
  _downloadCache.unshift(item);
  if (_downloadCache.length > MAX_CACHED) {
    _downloadCache.length = MAX_CACHED;
  }
}

/**
 * Get the path for a download ID using chrome.downloads API.
 * Falls back to cached path if available.
 */
async function _getDownloadPath(id) {
  return new Promise((resolve) => {
    if (!chrome.downloads || !chrome.downloads.search) {
      const cached = _downloadCache.find(d => d.id === id);
      resolve(cached?.filename || null);
      return;
    }
    chrome.downloads.search({ id }, (results) => {
      if (results && results.length > 0) {
        resolve(results[0].filename || null);
      } else {
        const cached = _downloadCache.find(d => d.id === id);
        resolve(cached?.filename || null);
      }
    });
  });
}

/**
 * Register all download command handlers on a connection.
 * @param {Connection} connection - The active server connection
 */
export function registerDownloadHandlers(connection) {
  // -- List recent downloads --
  connection.on(MessageType.DOWNLOAD_LIST, async (message, conn) => {
    try {
      const maxResults = message.payload.limit || 20;
      const items = _downloadCache.slice(0, maxResults);
      conn.send(createMessage(MessageType.DOWNLOAD_RESULT, success({
        downloads: items,
        count: items.length,
      }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.DOWNLOAD_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Get status of a specific download --
  connection.on(MessageType.DOWNLOAD_STATUS, async (message, conn) => {
    try {
      const id = message.payload.id;
      if (id === undefined || id === null) {
        conn.send(createMessage(MessageType.DOWNLOAD_RESULT, failure('download id is required'), message.messageId));
        return;
      }

      // Try chrome.downloads API first
      if (chrome.downloads && chrome.downloads.search) {
        chrome.downloads.search({ id }, (results) => {
          if (results && results.length > 0) {
            const d = results[0];
            conn.send(createMessage(MessageType.DOWNLOAD_RESULT, success({
              id: d.id,
              filename: d.filename,
              url: d.url,
              mime: d.mime,
              totalBytes: d.totalBytes,
              receivedBytes: d.bytesReceived,
              state: d.state,
              error: d.error || null,
              startTime: d.startTime,
              endTime: d.endTime || null,
            }), message.messageId));
          } else {
            conn.send(createMessage(MessageType.DOWNLOAD_RESULT, failure(`Download not found: ${id}`), message.messageId));
          }
        });
      } else {
        // Fall back to cache
        const cached = _downloadCache.find(d => d.id === id);
        if (cached) {
          conn.send(createMessage(MessageType.DOWNLOAD_RESULT, success(cached), message.messageId));
        } else {
          conn.send(createMessage(MessageType.DOWNLOAD_RESULT, failure(`Download not found in cache: ${id}`), message.messageId));
        }
      }
    } catch (err) {
      conn.send(createMessage(MessageType.DOWNLOAD_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Search downloads --
  connection.on(MessageType.DOWNLOAD_SEARCH, async (message, conn) => {
    try {
      const query = (message.payload.query || '').toLowerCase();
      const results = _downloadCache.filter(d =>
        (d.filename && d.filename.toLowerCase().includes(query)) ||
        (d.url && d.url.toLowerCase().includes(query)) ||
        (d.mime && d.mime.toLowerCase().includes(query))
      );
      conn.send(createMessage(MessageType.DOWNLOAD_RESULT, success({
        downloads: results,
        count: results.length,
        query,
      }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.DOWNLOAD_RESULT, failure(err.message), message.messageId));
    }
  });

  // -- Clear download cache --
  connection.on(MessageType.DOWNLOAD_CLEAR_CACHE, async (message, conn) => {
    try {
      const count = _downloadCache.length;
      _downloadCache = [];
      conn.send(createMessage(MessageType.DOWNLOAD_RESULT, success({
        cleared: true,
        removedCount: count,
      }), message.messageId));
    } catch (err) {
      conn.send(createMessage(MessageType.DOWNLOAD_RESULT, failure(err.message), message.messageId));
    }
  });
}
