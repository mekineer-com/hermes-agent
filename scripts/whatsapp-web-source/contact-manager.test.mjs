import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ContactManager } from './contact-manager.js';

function manager(overrides = {}) {
  const commands = [];
  const writes = [];
  const logs = [];
  const store = {
    command: async (op, payload = {}) => {
      commands.push({ op, payload });
      if (op === 'in_scope_contact_ids') {
        return { contact_ids: ['111@c.us'], contact_local_ids: ['111'] };
      }
      if (op === 'get_metadata') return { value: null };
      if (op === 'prune_scope') {
        return { deleted_chats: 2, deleted_contacts: 3, backup_path: payload.backup_path };
      }
      return {};
    },
  };
  const contactManager = new ContactManager({
    enabled: true,
    intervalSeconds: 0,
    activeSince: 1000,
    dbPath: '/tmp/web_source.db',
    store,
    client: { getContactById: async (id) => ({ id, name: `Name ${id}` }) },
    page: () => ({}),
    status: { write: (row, options) => writes.push({ row, options }) },
    isReady: () => true,
    dbWriteable: () => true,
    logger: {
      log: (...args) => logs.push(['log', ...args]),
      error: (...args) => logs.push(['error', ...args]),
    },
    now: () => 1234000,
    makeTimestampLabel: () => 'stamp',
    ...overrides,
  });
  return { contactManager, commands, writes, logs };
}

test('persistForMessageRow dedupes ids and writes sparse contacts without enrichment', async () => {
  const { contactManager, commands } = manager();

  await contactManager.persistForMessageRow({
    chat_id: '111@c.us',
    from_id: '111@c.us',
    to_id: '222@c.us',
    author_id: '',
  }, false);

  assert.deepEqual(commands.map((command) => command.op), ['upsert_contact', 'upsert_contact']);
  assert.deepEqual(
    commands.map((command) => command.payload.row.contact_id),
    ['111@c.us', '222@c.us'],
  );
});

test('snapshot writes scoped contact status fields', async () => {
  const { contactManager, commands, writes } = manager({
    dbWriteable: () => false,
    readSnapshot: async (_page, scope) => [
      { contact_id: '111@c.us', contact_local_id: '111', raw: {}, name: 'Alice' },
      { contact_id: '222@c.us', contact_local_id: '222', raw: {}, name: 'Bob' },
    ].filter((row) => scope.contactIds.has('111@c.us') || row.contact_id === '222@c.us'),
  });

  await contactManager.snapshot();

  assert.deepEqual(commands.map((command) => command.op), [
    'in_scope_contact_ids',
    'upsert_contact',
    'upsert_contact',
  ]);
  assert.deepEqual(writes[0].row, {
    state: 'ready',
    wwebjs_ready: true,
    db_writeable: false,
    error: null,
    last_contact_snapshot_at: 1234,
    last_contact_snapshot_rows: 2,
    last_contact_snapshot_scope_ids: 1,
  });
});

test('pruneScopeOnce uses one active-since metadata guard and records backup path', async () => {
  const { contactManager, commands, writes } = manager();

  await contactManager.pruneScopeOnce();

  assert.deepEqual(commands.map((command) => command.op), [
    'get_metadata',
    'prune_scope',
    'set_metadata',
  ]);
  assert.equal(commands[0].payload.key, 'prune_scope:1000');
  assert.equal(commands[1].payload.backup_path, '/tmp/web_source.db.bak-prune-stamp');
  assert.deepEqual(writes[0].row, {
    state: 'ready',
    wwebjs_ready: true,
    db_writeable: true,
    error: null,
    last_prune_at: 1234,
    last_prune_active_since: 1000,
    last_prune_deleted_chats: 2,
    last_prune_deleted_contacts: 3,
    last_prune_backup_path: '/tmp/web_source.db.bak-prune-stamp',
  });
});
