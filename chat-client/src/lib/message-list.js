function persistedMessageId(message) {
  const id = message?.id;
  if (id == null || String(id).startsWith('temp_')) return null;
  return String(id);
}

function clientMessageId(message) {
  const id = message?.client_message_id;
  return id == null || id === '' ? null : String(id);
}

/** Two entries represent the same message when either stable identifier matches. */
export function isSameMessage(left, right) {
  const leftId = persistedMessageId(left);
  const rightId = persistedMessageId(right);
  if (leftId && rightId && leftId === rightId) return true;

  const leftClientId = clientMessageId(left);
  const rightClientId = clientMessageId(right);
  return Boolean(leftClientId && rightClientId && leftClientId === rightClientId);
}

export function hasMessage(messages, candidate) {
  return messages.some(message => isSameMessage(message, candidate));
}

/**
 * Merge history with cached/realtime entries. Later entries carry fresher client-only
 * fields, while all duplicates are collapsed even if the input cache is already dirty.
 */
export function mergeMessageLists(history = [], current = [], limit = 200) {
  const merged = [];

  for (const message of [...history, ...current]) {
    const matchingIndexes = [];
    for (let index = 0; index < merged.length; index += 1) {
      if (isSameMessage(merged[index], message)) matchingIndexes.push(index);
    }

    if (matchingIndexes.length === 0) {
      merged.push(message);
      continue;
    }

    const firstIndex = matchingIndexes[0];
    let combined = merged[firstIndex];
    for (let index = 1; index < matchingIndexes.length; index += 1) {
      combined = { ...combined, ...merged[matchingIndexes[index]] };
    }
    merged[firstIndex] = { ...combined, ...message };

    for (let index = matchingIndexes.length - 1; index >= 1; index -= 1) {
      merged.splice(matchingIndexes[index], 1);
    }
  }

  return merged
    .sort((left, right) => (left.created_at || '').localeCompare(right.created_at || ''))
    .slice(-limit);
}

export function upsertMessage(messages, message, limit = 200) {
  return mergeMessageLists([], [...messages, message], limit);
}

export function messageRenderKey(message) {
  const id = persistedMessageId(message);
  if (id) return `id:${id}`;

  const clientId = clientMessageId(message);
  return clientId ? `client:${clientId}` : message;
}
