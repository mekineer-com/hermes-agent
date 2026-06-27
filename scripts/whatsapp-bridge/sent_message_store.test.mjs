import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { SentMessageStore } from './sent_message_store.js';

function makeStore() {
  const dir = mkdtempSync(join(tmpdir(), 'sent-store-'));
  return new SentMessageStore({
    recentlySentIdsPath: join(dir, 'recently_sent_ids.json'),
    sentMessageStorePath: join(dir, 'sent_message_store.json'),
    recentlySentRetentionMs: 30 * 24 * 60 * 60 * 1000,
    maxRecentIds: 500,
  });
}

test('sent message store serves exact and id-only Baileys retry lookups', () => {
  const store = makeStore();
  const sent = { key: { remoteJid: '111@s.whatsapp.net', id: 'abc', fromMe: true } };
  const content = { conversation: 'hello' };

  store.storeSent(sent, content);

  assert.deepEqual(store.getForBaileysKey(sent.key), content);
  assert.deepEqual(store.getByMessageId('abc'), content);
  assert.equal(store.getForBaileysKey({ remoteJid: '222@s.whatsapp.net', id: 'missing', fromMe: true }), undefined);
});

test('sent echo ids persist and reload', () => {
  const store = makeStore();
  store.trackSent('sent-1');

  const reloaded = new SentMessageStore({
    recentlySentIdsPath: store.recentlySentIdsPath,
    sentMessageStorePath: store.sentMessageStorePath,
    recentlySentRetentionMs: 30 * 24 * 60 * 60 * 1000,
    maxRecentIds: 500,
  });
  reloaded.load();

  assert.equal(reloaded.isEcho('sent-1'), true);
  assert.equal(reloaded.isEcho('other'), false);
});
