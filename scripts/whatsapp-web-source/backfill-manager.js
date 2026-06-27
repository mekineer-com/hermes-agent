'use strict';

const {
  idSerialized,
  isConversationChatId,
  jidLocal,
  messageKey,
  messageTimestamp,
  normalizeChat,
} = require('./normalization');

function oldestMessageTimestamp(messages) {
  let oldest = null;
  for (const message of messages) {
    const ts = messageTimestamp(message);
    if (!ts) continue;
    if (oldest === null || ts < oldest) oldest = ts;
  }
  return oldest;
}

class BackfillManager {
  constructor({
    client,
    store,
    status,
    backfillLimit,
    persistMessage,
    dbWriteable = () => true,
    logger = console,
    now = () => Date.now(),
  }) {
    this.client = client;
    this.store = store;
    this.status = status;
    this.backfillLimit = backfillLimit;
    this.persistMessage = persistMessage;
    this.dbWriteable = dbWriteable;
    this.logger = logger;
    this.now = now;
  }

  async persistMessages(messages, source, since, startedAt = Math.floor(this.now() / 1000)) {
    let inserted = 0;
    let updated = 0;
    let skippedBeforeSince = 0;
    const presentMsgKeys = [];
    let oldestPersistedTimestamp = null;
    for (const message of messages) {
      if (since > 0 && messageTimestamp(message) < since) {
        skippedBeforeSince += 1;
        continue;
      }
      const msgKey = messageKey(message);
      if (msgKey) presentMsgKeys.push(msgKey);
      const ts = messageTimestamp(message);
      if (ts && (oldestPersistedTimestamp === null || ts < oldestPersistedTimestamp)) {
        oldestPersistedTimestamp = ts;
      }
      const result = await this.persistMessage(message, source);
      if (result.action === 'insert') inserted += 1;
      else updated += 1;
    }
    return { inserted, updated, skippedBeforeSince, presentMsgKeys, oldestPersistedTimestamp, startedAt };
  }

  async chatMessages(chat, chatId, since) {
    const fetchStartedAt = Math.floor(this.now() / 1000);
    const messages = await chat.fetchMessages({ limit: this.backfillLimit });
    const result = await this.persistMessages(messages, 'backfill:fetchMessages', since, fetchStartedAt);
    let reconciledRevoked = 0;
    if (result.presentMsgKeys.length > 0 && result.oldestPersistedTimestamp !== null) {
      const reconcile = await this.store.command('mark_missing_in_chat_window', {
        row: {
          chat_id: chatId,
          chat_local_id: jidLocal(chatId),
          min_timestamp: result.oldestPersistedTimestamp,
          updated_before: result.startedAt,
          present_msg_keys: result.presentMsgKeys,
          source: 'reconcile:fetchMessages_missing',
        },
      });
      reconciledRevoked = Number(reconcile.matched || 0);
    }
    if (result.inserted || result.updated) {
      await this.store.command('upsert_chat', { row: normalizeChat(chat) });
    }
    const oldest = oldestMessageTimestamp(messages);
    const incomplete = since > 0 && messages.length >= this.backfillLimit && oldest !== null && oldest >= since;
    return { ...result, fetched: messages.length, chatId, incomplete, reconciledRevoked };
  }

  async chatsSince(since) {
    if (!since) return null;
    this.status.write({
      state: 'backfilling',
      wwebjs_ready: true,
      db_writeable: this.dbWriteable(),
      backfill_since: since,
    }, { immediate: true });
    const chats = await this.client.getChats();
    let scannedChats = 0;
    let backfilledChats = 0;
    let fetched = 0;
    let inserted = 0;
    let updated = 0;
    let skippedBeforeSince = 0;
    let reconciledRevoked = 0;
    const incompleteChatIds = [];
    for (const chat of chats) {
      const chatId = idSerialized(chat.id);
      if (!isConversationChatId(chatId)) continue;
      scannedChats += 1;
      try {
        const result = await this.chatMessages(chat, chatId, since);
        fetched += result.fetched;
        inserted += result.inserted;
        updated += result.updated;
        skippedBeforeSince += result.skippedBeforeSince;
        reconciledRevoked += result.reconciledRevoked;
        if (result.inserted || result.updated) backfilledChats += 1;
        if (result.incomplete) incompleteChatIds.push(chatId);
      } catch (error) {
        this.logger.error(`backfill ${chatId} failed:`, error);
      }
    }
    return {
      scannedChats,
      backfilledChats,
      fetched,
      inserted,
      updated,
      skippedBeforeSince,
      reconciledRevoked,
      incompleteChatIds,
    };
  }
}

module.exports = {
  BackfillManager,
  oldestMessageTimestamp,
};
