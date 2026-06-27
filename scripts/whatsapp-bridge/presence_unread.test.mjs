import test from 'node:test';
import assert from 'node:assert/strict';

import { PresenceUnread } from './presence_unread.js';

function makePresence(sock, connected = true) {
  const normalizeId = (value) => String(value || '').trim().replace(/:.*@/, '@');
  return new PresenceUnread({
    normalizeId,
    getSock: () => sock,
    isConnected: () => connected,
    preserveUnreadOnSend: true,
    sendUnavailableAfterActivity: true,
  });
}

test('restores unread state after sending into an unread chat', async () => {
  const calls = [];
  const sock = {
    sendPresenceUpdate: async (...args) => calls.push(['presence', ...args]),
    chatModify: async (...args) => calls.push(['modify', ...args]),
  };
  const presence = makePresence(sock);

  presence.updateUnreadCountSnapshot([{ id: '111:7@s.whatsapp.net', unreadCount: 2 }]);
  presence.rememberInboundLastMessage({
    key: {
      remoteJid: '111:7@s.whatsapp.net',
      id: 'msg-1',
      fromMe: false,
      participant: '222:4@s.whatsapp.net',
    },
    messageTimestamp: 123,
  });

  assert.equal(presence.hasUnreadMessages('111@s.whatsapp.net'), true);
  await presence.postSendPresenceAndUnreadRestore('111@s.whatsapp.net', true);

  assert.deepEqual(calls, [
    ['presence', 'unavailable'],
    ['modify', {
      markRead: false,
      lastMessages: [{
        key: {
          remoteJid: '111@s.whatsapp.net',
          id: 'msg-1',
          fromMe: false,
          participant: '222@s.whatsapp.net',
        },
        messageTimestamp: 123,
      }],
    }, '111@s.whatsapp.net'],
  ]);
});

test('does nothing when socket is disconnected', async () => {
  const calls = [];
  const sock = {
    sendPresenceUpdate: async (...args) => calls.push(['presence', ...args]),
    chatModify: async (...args) => calls.push(['modify', ...args]),
  };
  const presence = makePresence(sock, false);

  presence.updateUnreadCountSnapshot([{ id: '111@s.whatsapp.net', unreadCount: 1 }]);
  await presence.postSendPresenceAndUnreadRestore('111@s.whatsapp.net', true);

  assert.deepEqual(calls, []);
});
