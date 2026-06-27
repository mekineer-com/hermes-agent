import assert from 'node:assert/strict';
import { test } from 'node:test';
import { BackfillManager, oldestMessageTimestamp } from './backfill-manager.js';

function message(id, timestamp) {
  return {
    id: { _serialized: id },
    timestamp,
    from: '111@c.us',
    to: '222@c.us',
    body: `body ${id}`,
    type: 'chat',
  };
}

test('oldestMessageTimestamp ignores zero timestamps', () => {
  assert.equal(oldestMessageTimestamp([
    message('a', 0),
    message('b', 30),
    message('c', 20),
  ]), 20);
});

test('persistMessages skips rows before since and tracks present message keys', async () => {
  const persisted = [];
  const manager = new BackfillManager({
    client: {},
    store: { command: async () => ({}) },
    status: { write: () => {} },
    backfillLimit: 10,
    persistMessage: async (row) => {
      persisted.push(row.id._serialized);
      return { action: persisted.length === 1 ? 'insert' : 'update' };
    },
    now: () => 1234000,
  });

  const result = await manager.persistMessages([
    message('old', 900),
    message('new1', 1000),
    message('new2', 1100),
  ], 'backfill:fetchMessages', 1000);

  assert.deepEqual(persisted, ['new1', 'new2']);
  assert.deepEqual(result, {
    inserted: 1,
    updated: 1,
    skippedBeforeSince: 1,
    presentMsgKeys: ['new1', 'new2'],
    oldestPersistedTimestamp: 1000,
    startedAt: 1234,
  });
});

test('chatMessages reconciles missing rows through store.py and upserts changed chat', async () => {
  const commands = [];
  const nowValues = [1000000, 1234000];
  const manager = new BackfillManager({
    client: {},
    store: {
      command: async (op, payload) => {
        commands.push({ op, payload });
        if (op === 'mark_missing_in_chat_window') return { matched: 2 };
        return {};
      },
    },
    status: { write: () => {} },
    backfillLimit: 2,
    persistMessage: async () => ({ action: 'insert' }),
    now: () => nowValues.shift() ?? 1234000,
  });
  const chat = {
    id: { _serialized: '111@c.us' },
    name: 'Alice',
    fetchMessages: async () => [message('new1', 1000), message('new2', 1100)],
  };

  const result = await manager.chatMessages(chat, '111@c.us', 1000);

  assert.deepEqual(commands.map((command) => command.op), ['mark_missing_in_chat_window', 'upsert_chat']);
  assert.deepEqual(commands[0].payload.row, {
    chat_id: '111@c.us',
    chat_local_id: '111',
    min_timestamp: 1000,
    updated_before: 1000,
    present_msg_keys: ['new1', 'new2'],
    source: 'reconcile:fetchMessages_missing',
  });
  assert.equal(result.fetched, 2);
  assert.equal(result.reconciledRevoked, 2);
  assert.equal(result.incomplete, true);
});

test('chatsSince reports current db writeability', async () => {
  const writes = [];
  const manager = new BackfillManager({
    client: { getChats: async () => [] },
    store: { command: async () => ({}) },
    status: { write: (row) => writes.push(row) },
    backfillLimit: 2,
    persistMessage: async () => ({ action: 'insert' }),
    dbWriteable: () => false,
  });

  await manager.chatsSince(1000);

  assert.equal(writes[0].db_writeable, false);
});
