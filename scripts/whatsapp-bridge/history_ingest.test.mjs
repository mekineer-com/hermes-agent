import test from 'node:test';
import assert from 'node:assert/strict';

import {
  canonicalizeMessageIds,
  historyMessageSources,
  isStartupReplay,
  isRecentlySentEcho,
  upsertEventMode,
} from './history_ingest.js';

test('historyMessageSources includes top-level messaging-history messages', () => {
  const rows = historyMessageSources({
    chats: [
      {
        id: '111@s.whatsapp.net',
        messages: [{ key: { id: 'nested' }, message: { conversation: 'nested' } }],
      },
    ],
    messages: [
      { key: { id: 'top', remoteJid: '222@s.whatsapp.net' }, message: { conversation: 'top' } },
    ],
  });

  assert.deepEqual(rows.map((row) => row.message.key.id), ['nested', 'top']);
  assert.equal(rows[0].chatFallback, '111@s.whatsapp.net');
  assert.equal(rows[1].chatFallback, '');
});

test('canonicalizeMessageIds re-normalizes after alias learning', () => {
  const lidMap = { '247789598601266': '15133278228' };
  const normalize = (value) => {
    const raw = String(value || '').trim();
    if (raw.endsWith('@lid')) {
      const local = raw.split('@')[0];
      return lidMap[local] ? `${lidMap[local]}@s.whatsapp.net` : raw;
    }
    return raw;
  };

  const ids = canonicalizeMessageIds({
    chatId: '247789598601266@lid',
    fromMe: false,
  }, normalize);

  assert.equal(ids.chatId, '15133278228@s.whatsapp.net');
  assert.equal(ids.senderId, '15133278228@s.whatsapp.net');
});

test('isRecentlySentEcho only suppresses bridge-sent fromMe echoes', () => {
  const recentlySentIds = new Set(['sent-1']);

  assert.equal(isRecentlySentEcho({ fromMe: true, messageId: 'sent-1' }, recentlySentIds), true);
  assert.equal(isRecentlySentEcho({ fromMe: false, messageId: 'sent-1' }, recentlySentIds), false);
  assert.equal(isRecentlySentEcho({ fromMe: true, messageId: 'other' }, recentlySentIds), false);
});

test('upsertEventMode treats append as persist-only history', () => {
  assert.deepEqual(upsertEventMode('notify'), {
    forwardable: true,
    persistOnly: false,
    sourceSurface: 'messages.upsert',
  });
  assert.deepEqual(upsertEventMode('append'), {
    forwardable: true,
    persistOnly: true,
    sourceSurface: 'messages.upsert:append',
  });
  assert.equal(upsertEventMode('replace').forwardable, false);
});

test('isStartupReplay identifies old notify rows delivered after bridge startup', () => {
  assert.equal(isStartupReplay({
    timestamp: 1000,
    bridgeStartedAtSeconds: 1200,
    graceSeconds: 120,
  }), true);
  assert.equal(isStartupReplay({
    timestamp: 1090,
    bridgeStartedAtSeconds: 1200,
    graceSeconds: 120,
  }), false);
  assert.equal(isStartupReplay({
    timestamp: { low: 1000 },
    bridgeStartedAtSeconds: 1200,
    graceSeconds: 120,
  }), true);
  assert.equal(isStartupReplay({
    timestamp: 1000,
    bridgeStartedAtSeconds: 5000,
    graceSeconds: 36000,
  }), true);
  assert.equal(isStartupReplay({
    timestamp: 4500,
    bridgeStartedAtSeconds: 5000,
    graceSeconds: 36000,
  }), false);
});
