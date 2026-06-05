'use strict';

function jidLocal(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  return raw.replace(/:.*@/, '@').split('@', 1)[0];
}

function idSerialized(value) {
  if (!value) return null;
  if (typeof value === 'string') return value;
  if (value._serialized) return value._serialized;
  if (value.id?._serialized) return value.id._serialized;
  return String(value);
}

function messageKey(message) {
  return message?.id?._serialized || message?.rawData?.id?._serialized || null;
}

function messageChatId(message) {
  if (message.fromMe) return idSerialized(message.to);
  return idSerialized(message.from);
}

function mediaPlaceholder(message) {
  if (!message.hasMedia) return null;
  switch (message.type) {
    case 'image': return '[image]';
    case 'video': return '[video]';
    case 'ptt': return '[voice note]';
    case 'audio': return '[audio]';
    case 'document': return '[document]';
    case 'sticker': return '[sticker]';
    default: return `[${message.type || 'media'}]`;
  }
}

function messageTimestamp(message) {
  return Number(message.timestamp || message.rawData?.t || 0);
}

function normalizeMessage(message, source) {
  const msgKey = messageKey(message);
  const chatId = messageChatId(message);
  if (!msgKey) throw new Error('message has no serialized id');
  if (!chatId) throw new Error(`message ${msgKey} has no chat id`);

  const fromId = idSerialized(message.from);
  const toId = idSerialized(message.to);
  const authorId = idSerialized(message.author);
  const body = message.body || '';
  return {
    msg_key: msgKey,
    chat_id: chatId,
    chat_local_id: jidLocal(chatId),
    from_me: Boolean(message.fromMe),
    timestamp: messageTimestamp(message),
    type: String(message.type || 'unknown'),
    body,
    author_id: authorId,
    author_local_id: jidLocal(authorId),
    from_id: fromId,
    from_local_id: jidLocal(fromId),
    to_id: toId,
    to_local_id: jidLocal(toId),
    has_media: Boolean(message.hasMedia),
    media_placeholder: mediaPlaceholder(message),
    ack: message.ack ?? null,
    revoked: message.type === 'revoked',
    revoke_source: message.type === 'revoked' ? source : null,
    source,
    raw: message.rawData || {},
  };
}

function normalizeChat(chat) {
  const chatId = idSerialized(chat.id);
  return {
    chat_id: chatId,
    chat_local_id: jidLocal(chatId),
    name: chat.name || null,
    is_group: Boolean(chat.isGroup),
    last_timestamp: chat.timestamp || null,
    raw: chat.rawData || {},
  };
}

function isConversationChatId(chatId) {
  const value = String(chatId || '').trim().toLowerCase();
  if (!value) return false;
  if (value === 'status@broadcast') return false;
  if (value.endsWith('@newsletter')) return false;
  return true;
}

function normalizeContactRow(contact) {
  const contactId = idSerialized(contact.id || contact.contactId || contact);
  if (!contactId) return null;
  return {
    contact_id: contactId,
    contact_local_id: jidLocal(contactId),
    name: contact.name || null,
    short_name: contact.shortName || contact.short_name || null,
    push_name: contact.pushname || contact.pushName || contact.push_name || null,
    verified_name: contact.verifiedName || contact.verified_name || null,
    is_me: Boolean(contact.isMe),
    is_user: Boolean(contact.isUser),
    is_group: Boolean(contact.isGroup),
    raw: contact.raw || contact,
  };
}

function sparseContactRow(contactId) {
  if (!contactId) return null;
  return normalizeContactRow({ id: contactId, raw: { id: contactId } });
}

module.exports = {
  idSerialized,
  isConversationChatId,
  jidLocal,
  messageKey,
  messageTimestamp,
  normalizeChat,
  normalizeContactRow,
  normalizeMessage,
  sparseContactRow,
};
