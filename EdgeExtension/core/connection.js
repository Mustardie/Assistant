/**
 * connection.js
 *
 * Manages the WebSocket connection to the Nova FastAPI server.
 * Why this exists: The extension needs a persistent bidirectional channel
 * to receive commands from Nova and send responses back. WebSocket is the
 * simplest reliable mechanism for this.
 *
 * Responsibilities:
 * - Establish and maintain the WebSocket connection
 * - Exponential backoff reconnection on failure
 * - Dispatch incoming messages to registered type-based handlers
 * - Provide a send() method for outbound messages
 * - Send periodic heartbeats to keep the MV3 service worker alive
 * - Log all lifecycle events for debugging
 * - Track message IDs to prevent collisions
 */

export class Connection {
  /**
   * @param {string} serverUrl - WebSocket URL of the Nova server (e.g. ws://127.0.0.1:8742)
   */
  constructor(serverUrl) {
    this.serverUrl = serverUrl;
    this.ws = null;
    this.handlers = new Map();        // message type -> handler function
    this.reconnectDelay = 1000;       // initial reconnect delay (1s)
    this.maxReconnectDelay = 30000;   // maximum delay (30s)
    this.reconnectTimer = null;
    this.isConnected = false;
    this.intentionalClose = false;

    // Heartbeat to keep the MV3 service worker alive.
    // MV3 terminates service workers after ~30s of inactivity.
    // Sending a message every 20s counts as activity and prevents
    // the browser from killing the worker (and thus the WebSocket).
    this.heartbeatIntervalMs = 20000;
    this.heartbeatTimer = null;
    this.lastSentSeq = 0;

    // Sent message ID tracking — avoid collisions across reconnects
    this._sentIds = new Set();
    this._maxTrackedIds = 10000;
  }

  /**
   * Register a handler for a specific message type.
   * Handler receives (message, connection) when a matching message arrives.
   * @param {string} type - MessageType value
   * @param {function} handler - Callback to handle the message
   */
  on(type, handler) {
    this.handlers.set(type, handler);
  }

  /**
   * Initiate connection to the server. Starts the WebSocket handshake.
   */
  connect() {
    if (this.ws) {
      const state = this.ws.readyState;
      if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) {
        return; // already connected/connecting -- safe to call from alarms/startup events
      }
      this.ws = null; // socket is dead -- reopen below
    }
    console.log('[Nova Bridge] connect() called');
    this.intentionalClose = false;
    this._startHeartbeat();
    this._open();
  }

  /**
   * Close the connection and stop reconnection attempts.
   */
  disconnect() {
    console.log('[Nova Bridge] disconnect() called');
    this.intentionalClose = true;
    this._stopHeartbeat();
    this._clearReconnect();
    if (this.ws) {
      this.ws.close(1000, 'Intentional disconnect');
      this.ws = null;
    }
    this.isConnected = false;
  }

  /**
   * Send a JSON message over the WebSocket.
   * Tracks message IDs to prevent collisions across reconnects.
   * @param {object} message - Message object (use createMessage from protocol.js)
   */
  send(message) {
    this.lastSentSeq++;
    // Track message IDs for collision detection
    if (message.messageId) {
      this._sentIds.add(message.messageId);
      if (this._sentIds.size > this._maxTrackedIds) {
        // Prune oldest entries
        const iter = this._sentIds.values();
        for (let i = 0; i < 1000; i++) {
          const val = iter.next();
          if (val.done) break;
          this._sentIds.delete(val.value);
        }
      }
    }
    if (this.ws && this.isConnected) {
      this.ws.send(JSON.stringify(message));
    } else {
      console.warn(`[Nova Bridge] send() called while not connected, seq=${this.lastSentSeq}, type=${message.type}`);
    }
  }

  /**
   * Check if a message ID was already sent (to avoid collisions).
   * @param {string} messageId
   * @returns {boolean}
   */
  hasSentId(messageId) {
    return this._sentIds.has(messageId);
  }

  // ---- Heartbeat ---------------------------------------------------

  _startHeartbeat() {
    this._stopHeartbeat();
    console.log(`[Nova Bridge] Starting heartbeat every ${this.heartbeatIntervalMs}ms`);
    this.heartbeatTimer = setInterval(() => {
      if (this.isConnected) {
        const ping = {
          type: 'ping',
          messageId: `hb-${Date.now()}-${this.lastSentSeq}`,
          source: 'extension',
          timestamp: new Date().toISOString(),
          inResponseTo: null,
          payload: {
            heartbeat: true,
            // Lets the Nova server detect a stale (pre-hot-reload) build
            // and command `extension_reload` when it knows the loaded
            // code version is older than the one on disk.
            codeVersion: this.codeVersion || '0',
          },
        };
        try {
          this.ws.send(JSON.stringify(ping));
        } catch (err) {
          console.error('[Nova Bridge] Heartbeat send failed:', err.message);
        }
      } else {
        console.warn('[Nova Bridge] Heartbeat skipped — not connected');
      }
    }, this.heartbeatIntervalMs);
  }

  _stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  // ---- WebSocket lifecycle ----------------------------------------

  /** @private Open the WebSocket and attach event handlers. */
  _open() {
    // NEVER kill an existing live (or mid-handshake) socket. Closing it here
    // used to trigger onclose -> _scheduleReconnect -> _open -> close again:
    // a self-sustaining ~1s reconnect loop that never heals.
    if (this.ws) {
      const state = this.ws.readyState;
      if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) {
        return;
      }
      this.ws = null;
    }

    // Clear old sent IDs on fresh connection to avoid unbounded growth
    this._sentIds.clear();

    try {
      console.log(`[Nova Bridge] Opening WebSocket to ${this.serverUrl}`);
      this.ws = new WebSocket(this.serverUrl);
    } catch (err) {
      console.error('[Nova Bridge] WebSocket construction failed:', err.message);
      this._scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log(`[Nova Bridge] WebSocket OPEN (readyState=${this.ws.readyState})`);
      this.isConnected = true;
      this.reconnectDelay = 1000;
      this._startHeartbeat();
    };

    this.ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type !== 'pong') {
          console.log(`[Nova Bridge] Received: type=${message.type} id=${message.messageId}`);
        }
        this._dispatch(message);
      } catch (err) {
        console.error('[Nova Bridge] Failed to parse message:', err.message);
      }
    };

    this.ws.onclose = (event) => {
      console.log(
        `[Nova Bridge] WebSocket CLOSE: code=${event.code} reason="${event.reason}" wasClean=${event.wasClean}`
      );
      this.isConnected = false;
      // NOTE: the heartbeat deliberately KEEPS RUNNING while disconnected.
      // Each 20s tick is service-worker activity, and an MV3 worker that
      // goes idle for ~30s is terminated by the browser -- killing the
      // reconnect timer with it and leaving the extension dead until a
      // manual reload. The heartbeat keeps the worker alive through the
      // whole reconnect backoff, so the bridge is re-joined automatically
      // whenever Nova comes back.
      if (!this.intentionalClose) {
        console.log('[Nova Bridge] Unintentional close — scheduling reconnect');
        this._scheduleReconnect();
      } else {
        console.log('[Nova Bridge] Intentional close — no reconnect');
      }
    };

    this.ws.onerror = (event) => {
      const msg = event.message || event.type || 'unknown error';
      console.error(`[Nova Bridge] WebSocket ERROR: ${msg}`);
      console.error(`[Nova Bridge] WebSocket readyState at error: ${this.ws ? this.ws.readyState : 'null'}`);
    };
  }

  /** @private Route a parsed message to its registered handler. */
  async _dispatch(message) {
    const handler = this.handlers.get(message.type);
    if (handler) {
      try {
        await handler(message, this);
      } catch (err) {
        console.error(`[Nova Bridge] Handler error for ${message.type}:`, err.message);
        console.error(err.stack);
      }
    } else {
      if (message.type === 'pong') {
        return;
      }
      if (message.type === 'ping') {
        console.log('[Nova Bridge] Server ping, dispatching to ping handler');
        const pingHandler = this.handlers.get('ping');
        if (pingHandler) {
          try {
            await pingHandler(message, this);
          } catch (err) {
            console.error('[Nova Bridge] Server ping handler error:', err.message);
          }
        }
        return;
      }
      console.warn(`[Nova Bridge] No handler for message type: ${message.type}`);
    }
  }

  /** @private Schedule a reconnect with exponential backoff. */
  _scheduleReconnect() {
    this._clearReconnect();
    console.log(`[Nova Bridge] Reconnecting in ${this.reconnectDelay}ms`);
    this.reconnectTimer = setTimeout(() => {
      console.log('[Nova Bridge] Reconnect timer fired');
      this._open();
    }, this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
  }

  /** @private Cancel a pending reconnect. */
  _clearReconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}
