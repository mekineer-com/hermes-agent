import { atomicWriteJson, readJson } from './bridge_fs.js';

const MAX_SENT_STORE = 200;
const SENT_MESSAGE_RETENTION_MS = 24 * 60 * 60 * 1000;

const nullLogger = {
  warn() {},
};

function keyForBaileysKey(key) {
  return `${key.remoteJid}:${key.id}:${key.fromMe ? '1' : '0'}`;
}

export class SentMessageStore {
  constructor({
    recentlySentIdsPath,
    sentMessageStorePath,
    recentlySentRetentionMs,
    maxRecentIds,
    logger = nullLogger,
  }) {
    this.recentlySentIdsPath = recentlySentIdsPath;
    this.sentMessageStorePath = sentMessageStorePath;
    this.recentlySentRetentionMs = recentlySentRetentionMs;
    this.maxRecentIds = maxRecentIds;
    this.logger = logger;
    this.recentlySentIds = new Set();
    this.recentlySentAt = new Map();
    this.sentMessageStore = new Map();
  }

  load() {
    this.loadRecentlySentIds();
    this.loadSentMessages();
  }

  trackSent(id) {
    const messageId = String(id || '').trim();
    if (!messageId) return;
    this.recentlySentIds.add(messageId);
    this.recentlySentAt.set(messageId, Date.now());
    if (this.recentlySentIds.size > this.maxRecentIds) {
      const oldest = this.recentlySentAt.keys().next().value;
      this.recentlySentIds.delete(oldest);
      this.recentlySentAt.delete(oldest);
    }
    this.persistRecentlySentIds();
  }

  isEcho(id) {
    const messageId = String(id || '').trim();
    return !!messageId && this.recentlySentIds.has(messageId);
  }

  storeSent(sent, content) {
    if (!sent?.key?.id || !sent?.key?.remoteJid) return;
    const key = keyForBaileysKey(sent.key);
    const nowMs = Date.now();
    this.sentMessageStore.set(key, { content, ts: nowMs });
    if (this.sentMessageStore.size > MAX_SENT_STORE) {
      this.sentMessageStore.delete(this.sentMessageStore.keys().next().value);
    }
    const cutoff = nowMs - SENT_MESSAGE_RETENTION_MS;
    for (const [storeKey, val] of this.sentMessageStore) {
      if (val.ts < cutoff) this.sentMessageStore.delete(storeKey);
    }
    this.persistSentMessages();
  }

  getForBaileysKey(key) {
    return this.sentMessageStore.get(keyForBaileysKey(key))?.content;
  }

  getByMessageId(id) {
    const messageId = String(id || '').trim();
    if (!messageId) return undefined;
    for (const [storeKey, value] of this.sentMessageStore) {
      if (storeKey.includes(`:${messageId}:`)) return value.content;
    }
    return undefined;
  }

  persistRecentlySentIds() {
    const nowMs = Date.now();
    const cutoff = nowMs - this.recentlySentRetentionMs;
    for (const [messageId, ts] of this.recentlySentAt) {
      if (!this.recentlySentIds.has(messageId) || ts < cutoff) {
        this.recentlySentIds.delete(messageId);
        this.recentlySentAt.delete(messageId);
      }
    }
    while (this.recentlySentAt.size > this.maxRecentIds) {
      const oldest = this.recentlySentAt.keys().next().value;
      this.recentlySentIds.delete(oldest);
      this.recentlySentAt.delete(oldest);
    }
    const ids = [...this.recentlySentAt.entries()]
      .sort((a, b) => a[1] - b[1])
      .map(([id, ts]) => ({ id, ts }));
    try {
      atomicWriteJson(this.recentlySentIdsPath, {
        updated_at: new Date().toISOString(),
        ids,
      });
    } catch (err) {
      this.logger.warn({ err }, 'failed to persist recently sent ids');
    }
  }

  loadRecentlySentIds() {
    const data = readJson(this.recentlySentIdsPath);
    const rows = Array.isArray(data?.ids) ? data.ids : [];
    const cutoff = Date.now() - this.recentlySentRetentionMs;
    for (const row of rows) {
      const id = String(row?.id || '').trim();
      const ts = Number(row?.ts || 0);
      if (!id || !Number.isFinite(ts) || ts < cutoff) continue;
      this.recentlySentIds.add(id);
      this.recentlySentAt.set(id, ts);
    }
  }

  persistSentMessages() {
    const cutoff = Date.now() - SENT_MESSAGE_RETENTION_MS;
    const entries = [];
    for (const [k, v] of this.sentMessageStore) {
      if (v.ts >= cutoff) entries.push({ k, content: v.content, ts: v.ts });
    }
    try {
      atomicWriteJson(this.sentMessageStorePath, {
        updated_at: new Date().toISOString(),
        entries,
      });
    } catch (err) {
      this.logger.warn({ err }, 'failed to persist sent message store');
    }
  }

  loadSentMessages() {
    const data = readJson(this.sentMessageStorePath);
    const rows = Array.isArray(data?.entries) ? data.entries : [];
    const cutoff = Date.now() - SENT_MESSAGE_RETENTION_MS;
    for (const row of rows) {
      const k = String(row?.k || '').trim();
      const ts = Number(row?.ts || 0);
      if (!k || !Number.isFinite(ts) || ts < cutoff || !row.content) continue;
      this.sentMessageStore.set(k, { content: row.content, ts });
    }
    while (this.sentMessageStore.size > MAX_SENT_STORE) {
      this.sentMessageStore.delete(this.sentMessageStore.keys().next().value);
    }
  }
}
