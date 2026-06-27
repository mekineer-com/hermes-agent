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

function timestampSeconds(value) {
  if (typeof value === 'object' && value !== null) {
    if (Number.isFinite(Number(value.low))) return Number(value.low);
    return 0;
  }
  const ts = Number(value);
  if (!Number.isFinite(ts) || ts <= 0) return 0;
  return ts > 10000000000 ? ts / 1000 : ts;
}

export function isStartupReplay({ timestamp, bridgeStartedAtSeconds, graceSeconds = 120 } = {}) {
  const ts = timestampSeconds(timestamp);
  const started = Number(bridgeStartedAtSeconds);
  const grace = Math.min(600, Math.max(0, Number(graceSeconds) || 0));
  return !!ts && Number.isFinite(started) && started > 0 && ts < started - grace;
}

export function upsertEventMode(type) {
  if (type === 'notify') {
    return {
      forwardable: true,
      persistOnly: false,
      deliveryMode: 'live',
    };
  }
  if (type === 'append') {
    return {
      forwardable: true,
      persistOnly: true,
      deliveryMode: 'persist_only',
    };
  }
  // chats.update can carry message rows, but Baileys does not guarantee it is
  // a live delivery signal. Ingest it for history/discovery only.
  return {
    forwardable: false,
    persistOnly: true,
    deliveryMode: 'persist_only',
  };
}

export function classifyUpsertEvent({
  type,
  isAgentEcho = false,
  timestamp,
  bridgeStartedAtSeconds,
  startupReplayGraceSeconds = 120,
} = {}) {
  const mode = upsertEventMode(type);
  if (!mode.forwardable) return mode;
  const startupReplay = (
    type === 'notify'
    && isStartupReplay({
      timestamp,
      bridgeStartedAtSeconds,
      graceSeconds: startupReplayGraceSeconds,
    })
  );
  if (isAgentEcho || mode.persistOnly || startupReplay) {
    return {
      forwardable: true,
      persistOnly: true,
      deliveryMode: 'persist_only',
    };
  }
  return mode;
}
