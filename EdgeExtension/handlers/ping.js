/**
 * ping.js
 *
 * Handles the PING message from Nova.
 * Why this exists: The simplest possible handler that validates the entire
 * communication channel is working. Phase 1 only needs PING -> PONG.
 *
 * When a PING is received, responds with a PONG that includes the original
 * messageId for request-response correlation on the server side.
 */

import { MessageType, createMessage } from '../core/protocol.js';

/**
 * Register the PING handler on a connection.
 * @param {Connection} connection - The active server connection
 */
export function registerPingHandler(connection) {
  connection.on(MessageType.PING, (message, conn) => {
    console.log('[Nova Bridge] Received PING, sending PONG');
    const pong = createMessage(
      MessageType.PONG,
      { echo: message.payload },
      message.messageId
    );
    conn.send(pong);
  });
}
