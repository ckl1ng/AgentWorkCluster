import assert from 'node:assert/strict';
import test from 'node:test';

import { hasMessage, mergeMessageLists, upsertMessage } from './message-list.js';

test('history merge removes duplicate server ids already present in the cache', () => {
  const history = [
    { id: 1, encrypted_content: 'history', created_at: '2026-01-01T00:00:00Z' },
    { id: 2, created_at: '2026-01-01T00:00:01Z' },
  ];
  const current = [
    { id: 1, encrypted_content: 'stale' },
    { id: 1, encrypted_content: 'realtime', from_username: 'alice' },
  ];

  const result = mergeMessageLists(history, current);

  assert.deepEqual(result.map(message => message.id), [1, 2]);
  assert.equal(result[0].encrypted_content, 'realtime');
  assert.equal(result[0].from_username, 'alice');
});

test('numeric and string forms of a server id are treated as the same message', () => {
  const result = mergeMessageLists([{ id: 1 }], [{ id: '1', delivered: true }]);

  assert.equal(result.length, 1);
  assert.equal(result[0].delivered, true);
});

test('a realtime message replaces its local echo using client_message_id', () => {
  const local = { id: 'temp_request-1', client_message_id: 'request-1' };
  const received = { id: 7, client_message_id: 'request-1', delivered: true };

  const result = upsertMessage([local], received);

  assert.deepEqual(result, [{ id: 7, client_message_id: 'request-1', delivered: true }]);
  assert.equal(hasMessage(result, { id: 7 }), true);
});

test('a bridge entry collapses duplicates matched by different identifiers', () => {
  const result = mergeMessageLists([], [
    { id: 'temp_request-1', client_message_id: 'request-1' },
    { id: 7 },
    { id: 7, client_message_id: 'request-1', delivered: true },
  ]);

  assert.deepEqual(result, [{ id: 7, client_message_id: 'request-1', delivered: true }]);
});
