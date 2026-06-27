import test from 'node:test';
import assert from 'node:assert/strict';

import {
  isConversationChatId,
  jidLocal,
  messageKey,
  normalizeContactRow,
  normalizeMessage,
} from './normalization.js';

test('normalizeMessage preserves WhatsApp projection fields', () => {
  const row = normalizeMessage({
    id: { _serialized: 'true_123_c_us_MSG' },
    from: '123@c.us',
    to: '12025550199@c.us',
    author: '456@c.us',
    fromMe: false,
    timestamp: 100,
    type: 'chat',
    body: 'hello',
    hasMedia: false,
    ack: 1,
    rawData: { id: { _serialized: 'true_123_c_us_MSG' }, t: 100 },
  }, 'event:message');

  assert.equal(row.msg_key, 'true_123_c_us_MSG');
  assert.equal(row.chat_id, '123@c.us');
  assert.equal(row.chat_local_id, '123');
  assert.equal(row.author_id, '456@c.us');
  assert.equal(row.author_local_id, '456');
  assert.equal(row.source, 'event:message');
  assert.equal(row.revoked, false);
});

test('normalize helpers reject non-conversation chats and preserve contact names', () => {
  assert.equal(isConversationChatId('status@broadcast'), false);
  assert.equal(isConversationChatId('abc@newsletter'), false);
  assert.equal(isConversationChatId('123@c.us'), true);
  assert.equal(messageKey({ rawData: { id: { _serialized: 'RAW' } } }), 'RAW');

  const contact = normalizeContactRow({
    id: { _serialized: '123@c.us' },
    name: 'Test Contact',
    shortName: 'R',
    pushname: 'Push',
  });
  assert.equal(contact.contact_id, '123@c.us');
  assert.equal(contact.name, 'Test Contact');
  assert.equal(contact.short_name, 'R');
  assert.equal(contact.push_name, 'Push');
});

test('jidLocal strips WhatsApp multi-device suffixes', () => {
  assert.equal(jidLocal('15551234567:12@c.us'), '15551234567');
  assert.equal(jidLocal('140063262396533:99@lid'), '140063262396533');
});
