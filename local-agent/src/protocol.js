export const PROTOCOL_VERSION = 1;

export function envelope(type, payload = {}) {
  if (typeof type !== 'string' || !type) throw new TypeError('message type is required');
  return {
    protocol_version: PROTOCOL_VERSION,
    message_id: `m_${crypto.randomUUID()}`,
    type,
    sent_at: new Date().toISOString(),
    ...payload,
  };
}

export function validateEnvelope(message) {
  if (!message || typeof message !== 'object') throw new Error('invalid message');
  if (message.protocol_version !== PROTOCOL_VERSION) throw new Error('unsupported protocol version');
  if (typeof message.message_id !== 'string' || !message.message_id) throw new Error('message_id is required');
  if (typeof message.type !== 'string' || !message.type) throw new Error('type is required');
  return message;
}

export function jsonRpcResult(id, result) { return { jsonrpc: '2.0', id, result }; }
export function jsonRpcError(id, code, message) { return { jsonrpc: '2.0', id, error: { code, message } }; }
