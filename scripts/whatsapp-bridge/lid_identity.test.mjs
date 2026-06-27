import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { LidIdentity } from './lid_identity.js';

function makeIdentity() {
  const dir = mkdtempSync(join(tmpdir(), 'lid-identity-'));
  return new LidIdentity({
    sessionDir: dir,
    aliasTtlMs: 5 * 60 * 1000,
  });
}

test('normalizes known lids to legacy phone JIDs', () => {
  const identity = makeIdentity();
  identity.learnPair('999@lid', '111@s.whatsapp.net');

  assert.equal(identity.normalizeId('999@lid'), '111@s.whatsapp.net');
  assert.equal(identity.normalizeId('222@lid'), '222@lid');
  assert.equal(identity.normalizeId('111:7@s.whatsapp.net'), '111@s.whatsapp.net');
});

test('persists learned lid pairs in Baileys key-store shape', () => {
  const identity = makeIdentity();
  let written = null;
  identity.setKeyStore({ set: async (value) => { written = value; } });

  identity.learnPair('999@lid', '111@s.whatsapp.net');

  assert.deepEqual(written, {
    'lid-mapping': {
      111: '999',
      '999_reverse': '111',
    },
  });
});

test('rebuilds pairs from mapping files and creds self identity', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lid-identity-'));
  writeFileSync(join(dir, 'lid-mapping-111.json'), JSON.stringify('999'));
  writeFileSync(join(dir, 'creds.json'), JSON.stringify({
    me: {
      id: '222:7@s.whatsapp.net',
      lid: '888@lid',
    },
  }));
  const identity = new LidIdentity({ sessionDir: dir, aliasTtlMs: 5 * 60 * 1000 });

  assert.equal(identity.normalizeId('999@lid'), '111@s.whatsapp.net');
  assert.equal(identity.normalizeId('888@lid'), '222@s.whatsapp.net');
});

test('learns mirrored DM aliases and reports duplicate', () => {
  const identity = makeIdentity();
  const first = identity.learnAliasFromDm({
    chatId: '999@lid',
    messageId: 'msg-1',
    fromMe: false,
    isGroup: false,
  });
  const second = identity.learnAliasFromDm({
    chatId: '111@s.whatsapp.net',
    messageId: 'msg-1',
    fromMe: false,
    isGroup: false,
  });

  assert.equal(first.duplicate, false);
  assert.deepEqual(second, { duplicate: true, previousChatId: '999@lid' });
  assert.equal(identity.normalizeId('999@lid'), '111@s.whatsapp.net');
});
