import test from 'node:test';
import assert from 'node:assert/strict';

import { createMessageIngest } from './message_ingest.js';

function makeIngest({ sock = { user: { id: '111:1@s.whatsapp.net', lid: '111@lid', name: 'Me' } } } = {}) {
  const queued = [];
  const rememberedChats = [];
  const normalizeId = (value) => String(value || '').trim().replace(/:.*@/, '@');
  const ingest = createMessageIngest({
    durableQueue: {
      enqueue(event) {
        queued.push(event);
        return true;
      },
    },
    identity: {
      learnAliasFromDm() { return null; },
    },
    knownState: {
      extractPossibleSenderName(msg) { return String(msg?.pushName || ''); },
      getChat() { return null; },
      getPushName() { return ''; },
      rememberChat(chatId, row) { rememberedChats.push({ chatId, row }); },
      rememberPushName() {},
      resolveDmDisplayName(chatId) { return chatId.split('@')[0]; },
    },
    presence: {
      rememberInboundLastMessage() {},
    },
    sentStore: {
      isEcho() { return false; },
    },
    normalizeId,
    getSock: () => sock,
    resolveGroupChatName: async () => '',
    logger: {},
    config: {
      allowedUsers: new Set(),
      audioCacheDir: '/tmp',
      bridgeStartedAtSeconds: 1000,
      debug: false,
      documentCacheDir: '/tmp',
      imageCacheDir: '/tmp',
      replyPrefix: '',
      revokeStubType: 1,
      sessionDir: '/tmp',
      startupReplayGraceSeconds: 120,
      syncHistoryWindowDays: 14,
      whatsappMode: 'self-chat',
    },
  });
  return { ingest, queued, rememberedChats };
}

test('handleUpsert queues a self-chat text event with current delivery fields', async () => {
  const { ingest, queued, rememberedChats } = makeIngest();

  await ingest.handleUpsert({
    type: 'notify',
    messages: [{
      key: { remoteJid: '111@s.whatsapp.net', id: 'm1', fromMe: true },
      message: { conversation: 'hello' },
      messageTimestamp: 1000,
    }],
  });

  assert.equal(queued.length, 1);
  assert.deepEqual(queued[0], {
    deliveryMode: 'live',
    messageId: 'm1',
    chatId: '111@s.whatsapp.net',
    senderId: '111@s.whatsapp.net',
    senderName: 'Me',
    chatName: '111',
    isGroup: false,
    body: 'hello',
    hasMedia: false,
    mediaType: '',
    mediaUrls: [],
    mentionedIds: [],
    quotedMessageId: null,
    quotedParticipant: null,
    quotedRemoteJid: null,
    hasQuotedMessage: false,
    botIds: ['111@s.whatsapp.net', '111@lid'],
    timestamp: 1000,
    speakerRoleHint: 'user',
    speakerNameHint: '',
  });
  assert.deepEqual(rememberedChats, [{
    chatId: '111@s.whatsapp.net',
    row: { isGroup: false, name: '111', lastSenderName: '' },
  }]);
});

test('handleUpdate queues revokes only for delete updates', () => {
  const { ingest, queued } = makeIngest();

  ingest.handleUpdate([
    { key: { remoteJid: '222@s.whatsapp.net', id: 'keep' }, update: { messageStubType: 1, message: { conversation: 'still here' } } },
    { key: { remoteJid: '222@s.whatsapp.net', id: 'gone' }, update: { messageStubType: 1, message: null } },
  ]);

  assert.equal(queued.length, 1);
  assert.equal(queued[0].eventType, 'revoke');
  assert.equal(queued[0].deliveryMode, 'revoke');
  assert.equal(queued[0].messageId, 'gone');
  assert.equal(queued[0].chatId, '222@s.whatsapp.net');
});
