import path from 'path';
import { readdirSync } from 'fs';
import { readJson } from './bridge_fs.js';

const nullLogger = {
  warn() {},
};

function jidLocal(id) {
  return String(id || '').trim().replace(/:.*@/, '@').split('@', 1)[0];
}

export class LidIdentity {
  constructor({
    sessionDir,
    aliasTtlMs,
    debug = false,
    logger = nullLogger,
  }) {
    this.sessionDir = sessionDir;
    this.aliasTtlMs = aliasTtlMs;
    this.debug = debug;
    this.logger = logger;
    this.lidToPhone = this.buildMap();
    this.lidKeyStore = null;
    this.recentDmMessageById = new Map();
    this.onPairLearned = null;
  }

  setKeyStore(keyStore) {
    this.lidKeyStore = keyStore;
  }

  setOnPairLearned(callback) {
    this.onPairLearned = callback;
  }

  rebuildFromDisk() {
    this.lidToPhone = this.buildMap();
    for (const [lid, phone] of Object.entries(this.lidToPhone)) {
      this.onPairLearned?.(lid, phone);
    }
  }

  forEachPair(callback) {
    for (const [lid, phone] of Object.entries(this.lidToPhone)) {
      callback(lid, phone);
    }
  }

  normalizeId(value) {
    const raw = String(value || '').trim();
    if (!raw) return '';

    const collapsed = raw.replace(/:.*@/, '@');
    const atIndex = collapsed.indexOf('@');
    if (atIndex < 0) {
      return collapsed;
    }

    const local = collapsed.slice(0, atIndex);
    const domain = collapsed.slice(atIndex + 1).toLowerCase();
    if (!local) {
      return '';
    }

    if (domain === 'lid') {
      const mappedPhone = String(this.lidToPhone[local] || '').trim();
      if (mappedPhone) {
        return `${mappedPhone}@s.whatsapp.net`;
      }
      return `${local}@lid`;
    }
    if (domain === 's.whatsapp.net') {
      return `${local}@s.whatsapp.net`;
    }
    return collapsed;
  }

  learnPair(lidValue, jidValue, { persistBatch = null } = {}) {
    const lidLocal = jidLocal(lidValue);
    const phoneLocal = jidLocal(jidValue);
    if (!lidLocal || !phoneLocal || lidLocal === phoneLocal) return;
    if (String(this.lidToPhone[lidLocal] || '') === phoneLocal) return;
    this.lidToPhone[lidLocal] = phoneLocal;
    if (persistBatch) {
      this.addPairToPersistBatch(persistBatch, phoneLocal, lidLocal);
    } else {
      const immediateBatch = {};
      this.addPairToPersistBatch(immediateBatch, phoneLocal, lidLocal);
      this.persistBatch(immediateBatch);
    }
    this.onPairLearned?.(lidLocal, phoneLocal);
  }

  rememberPhoneShares(payload) {
    if (Array.isArray(payload)) {
      const persistBatch = {};
      for (const row of payload) {
        if (!row || typeof row !== 'object') continue;
        this.learnPair(row.lid, row.jid, { persistBatch });
      }
      this.persistBatch(persistBatch);
      return;
    }
    if (payload && typeof payload === 'object') {
      this.learnPair(payload.lid, payload.jid);
    }
  }

  learnAliasFromDm({ chatId, messageId, fromMe, isGroup }) {
    if (isGroup) return { duplicate: false };
    const normalizedChatId = this.normalizeId(chatId);
    const id = String(messageId || '').trim();
    if (!normalizedChatId || !id) return { duplicate: false };
    const domain = this.jidDomain(normalizedChatId);
    if (domain !== 'lid' && domain !== 's.whatsapp.net') return { duplicate: false };

    const nowMs = Date.now();
    this.pruneRecentDmMessageCache(nowMs);
    const key = `${fromMe ? '1' : '0'}:${id}`;
    const previous = this.recentDmMessageById.get(key);
    this.recentDmMessageById.set(key, { chatId: normalizedChatId, ts: nowMs });
    if (!previous || previous.chatId === normalizedChatId) return { duplicate: false };

    const previousDomain = this.jidDomain(previous.chatId);
    if (previousDomain === domain) return { duplicate: false };

    const lidLocal = domain === 'lid'
      ? jidLocal(normalizedChatId)
      : jidLocal(previous.chatId);
    const phoneLocal = domain === 's.whatsapp.net'
      ? jidLocal(normalizedChatId)
      : jidLocal(previous.chatId);
    if (!lidLocal || !phoneLocal || lidLocal === phoneLocal) return { duplicate: false };

    if (String(this.lidToPhone[lidLocal] || '') !== phoneLocal) {
      this.learnPair(`${lidLocal}@lid`, `${phoneLocal}@s.whatsapp.net`);
    }
    if (this.debug) {
      console.log(JSON.stringify({
        event: 'discovery_alias_learned',
        source: 'mirrored_dm_message_id',
        messageId: id,
        lid: lidLocal,
        phone: phoneLocal,
      }));
    }
    return { duplicate: true, previousChatId: previous.chatId };
  }

  jidDomain(id) {
    const normalized = this.normalizeId(id);
    const atIndex = normalized.indexOf('@');
    if (atIndex < 0) return '';
    return normalized.slice(atIndex + 1).toLowerCase();
  }

  buildMap() {
    const map = {};
    try {
      for (const f of readdirSync(this.sessionDir)) {
        const m = f.match(/^lid-mapping-(\d+)\.json$/);
        if (!m) continue;
        const phone = m[1];
        const lid = readJson(path.join(this.sessionDir, f));
        if (lid) map[String(lid)] = phone;
      }
    } catch {}
    const creds = readJson(path.join(this.sessionDir, 'creds.json'));
    const meId = String(creds?.me?.id || '').replace(/:.*@/, '@').split('@')[0];
    const meLid = String(creds?.me?.lid || '').replace(/:.*@/, '@').split('@')[0];
    if (meId && meLid && meId !== meLid) {
      map[meLid] = meId;
    }
    return map;
  }

  addPairToPersistBatch(persistBatch, phoneLocal, lidLocal) {
    persistBatch[phoneLocal] = lidLocal;
    persistBatch[`${lidLocal}_reverse`] = phoneLocal;
  }

  persistBatch(persistBatch) {
    if (!this.lidKeyStore) return;
    const keys = Object.keys(persistBatch);
    if (keys.length === 0) return;
    void this.lidKeyStore
      .set({ 'lid-mapping': persistBatch })
      .catch((err) => this.logger.warn({ err }, 'failed to persist lid-mapping batch'));
  }

  pruneRecentDmMessageCache(nowMs) {
    for (const [key, row] of this.recentDmMessageById.entries()) {
      if ((nowMs - Number(row?.ts || 0)) > this.aliasTtlMs) {
        this.recentDmMessageById.delete(key);
      }
    }
  }
}
