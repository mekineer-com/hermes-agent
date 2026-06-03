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

export function historyTimestampSeconds(value) {
  if (value === undefined || value === null || value === '') return 0;
  if (typeof value === 'object') {
    if (Number.isFinite(Number(value.low))) return Number(value.low);
    return 0;
  }
  const ts = Number(value);
  if (!Number.isFinite(ts) || ts <= 0) return 0;
  return ts > 10000000000 ? ts / 1000 : ts;
}

export function shouldTreatChatUpdateAsLive(value, {
  nowSeconds = Date.now() / 1000,
  liveWindowSeconds = 5 * 60,
} = {}) {
  const ts = historyTimestampSeconds(value);
  if (!ts) return false;
  const window = Number(liveWindowSeconds);
  if (!Number.isFinite(window) || window <= 0) return false;
  return Math.abs(Number(nowSeconds) - ts) <= window;
}
