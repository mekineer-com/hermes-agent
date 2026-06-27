import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { KnownState } from './known_state.js';

function makeState() {
  const dir = mkdtempSync(join(tmpdir(), 'known-state-'));
  const pairs = new Map();
  const identity = {
    forEachPair(callback) {
      for (const [lid, phone] of pairs) callback(lid, phone);
    },
    learnPair(lidValue, jidValue, { persistBatch = null } = {}) {
      const lid = String(lidValue || '').split('@')[0];
      const phone = String(jidValue || '').split('@')[0];
      if (!lid || !phone) return;
      pairs.set(lid, phone);
      if (persistBatch) {
        persistBatch[phone] = lid;
        persistBatch[`${lid}_reverse`] = phone;
      }
    },
    persistBatch(batch) {
      identity.lastPersistBatch = { ...batch };
    },
    lastPersistBatch: null,
  };
  const normalizeId = (value) => {
    const raw = String(value || '').trim();
    if (!raw) return '';
    const collapsed = raw.replace(/:.*@/, '@');
    const atIndex = collapsed.indexOf('@');
    if (atIndex < 0) return collapsed;
    const local = collapsed.slice(0, atIndex);
    const domain = collapsed.slice(atIndex + 1).toLowerCase();
    if (domain === 'lid' && pairs.has(local)) return `${pairs.get(local)}@s.whatsapp.net`;
    if (domain === 's.whatsapp.net') return `${local}@s.whatsapp.net`;
    return `${local}@${domain}`;
  };
  return {
    pairs,
    identity,
    state: new KnownState({
      knownChatsPath: join(dir, 'known_chats.json'),
      knownContactsPath: join(dir, 'known_contacts.json'),
      normalizeId,
      identity,
    }),
  };
}

test('canonicalizing a learned lid pair preserves chat and contact names', () => {
  const { state, pairs } = makeState();

  state.rememberChat('999@lid', {
    name: 'Annie',
    lastSenderName: 'Annie',
  });
  state.rememberPushName('999@lid', 'Annie');
  pairs.set('999', '111');
  state.canonicalize();

  assert.equal(state.getChat('999@lid'), undefined);
  const merged = state.getChat('111@s.whatsapp.net');
  assert.equal(merged.chatId, '111@s.whatsapp.net');
  assert.equal(merged.isGroup, false);
  assert.equal(merged.name, 'Annie');
  assert.equal(merged.lastSenderName, 'Annie');
  assert.equal(state.getPushName('111@s.whatsapp.net'), 'Annie');
  assert.equal(state.getPushName('999@lid'), undefined);
});

test('contact snapshots learn lid mappings before storing display names', () => {
  const { state, identity } = makeState();

  state.rememberContactsFromSnapshot([{
    id: '999@lid',
    lid: '999@lid',
    jid: '111@s.whatsapp.net',
    notify: 'Liz',
  }]);

  assert.equal(state.getPushName('111@s.whatsapp.net'), 'Liz');
  assert.equal(state.getPushName('999@lid'), undefined);
  assert.deepEqual(identity.lastPersistBatch, {
    111: '999',
    '999_reverse': '111',
  });
});

test('DM display name prefers contact cache over row fallback', () => {
  const { state } = makeState();

  state.rememberPushName('111@s.whatsapp.net', 'Liz');

  assert.equal(
    state.resolveDmDisplayName('111@s.whatsapp.net', { name: 'Old Liz' }),
    'Liz',
  );
  assert.equal(
    state.resolveDmDisplayName('222@s.whatsapp.net', { lastSenderName: 'Annie' }),
    'Annie',
  );
});
