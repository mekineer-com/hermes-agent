export function historyMessageSources({ chats, messages } = {}, normalizeId = (value) => String(value || '').trim()) {
  const rows = [];
  if (Array.isArray(chats)) {
    for (const chat of chats) {
      const chatFallback = normalizeId(chat?.id || chat?.jid || '');
      const chatMessages = Array.isArray(chat?.messages) ? chat.messages : [];
      for (const message of chatMessages) {
        rows.push({ message, chatFallback });
      }
    }
  }
  if (Array.isArray(messages)) {
    for (const message of messages) {
      rows.push({ message, chatFallback: '' });
    }
  }
  return rows;
}

export function canonicalizeMessageIds({
  chatId,
  participantId = '',
  selfSenderId = '',
  fromMe = false,
} = {}, normalizeId = (value) => String(value || '').trim()) {
  const normalizedChatId = normalizeId(chatId);
  const normalizedParticipantId = participantId ? normalizeId(participantId) : '';
  const normalizedSelfSenderId = selfSenderId ? normalizeId(selfSenderId) : '';
  const senderId = fromMe
    ? (normalizedSelfSenderId || normalizedParticipantId || normalizedChatId)
    : (normalizedParticipantId || normalizedChatId);
  return {
    chatId: normalizedChatId,
    participantId: normalizedParticipantId,
    selfSenderId: normalizedSelfSenderId,
    senderId,
    isGroup: normalizedChatId.endsWith('@g.us'),
  };
}

export function isRecentlySentEcho({ fromMe = false, messageId = '' } = {}, recentlySentIds = new Set()) {
  return !!fromMe && !!String(messageId || '').trim() && recentlySentIds.has(messageId);
}

export function upsertEventMode(type) {
  if (type === 'notify') {
    return {
      forwardable: true,
      persistOnly: false,
      sourceSurface: 'messages.upsert',
    };
  }
  if (type === 'append') {
    return {
      forwardable: true,
      persistOnly: true,
      sourceSurface: 'messages.upsert:append',
    };
  }
  return {
    forwardable: false,
    persistOnly: false,
    sourceSurface: `messages.upsert:${String(type || 'unknown')}`,
  };
}
