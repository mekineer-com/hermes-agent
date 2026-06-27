import { atomicWriteJson, readJson } from './bridge_fs.js';

const nullLogger = {
  warn() {},
};

export class KnownState {
  constructor({
    knownChatsPath,
    knownContactsPath,
    normalizeId,
    identity,
    debug = false,
    logger = nullLogger,
  }) {
    this.knownChatsPath = knownChatsPath;
    this.knownContactsPath = knownContactsPath;
    this.normalizeId = normalizeId;
    this.identity = identity;
    this.debug = debug;
    this.logger = logger;
    this.knownChats = new Map();
    this.pushNameCache = new Map();
    this.unresolvedDmNameLogged = new Set();
  }

  persistKnownChats() {
    const chats = [];
    for (const row of this.knownChats.values()) {
      if (!row?.chatId) continue;
      chats.push({
        id: String(row.chatId),
        is_group: !!row.isGroup,
        name: String(row.name || ''),
        last_sender_name: String(row.lastSenderName || ''),
        updated_at_ms: Number(row.updatedAtMs || 0) || Date.now(),
      });
    }
    chats.sort((a, b) => String(a.id).localeCompare(String(b.id)));
    try {
      atomicWriteJson(this.knownChatsPath, {
        updated_at: new Date().toISOString(),
        chats,
      });
    } catch (err) {
      this.logger.warn({ err }, 'failed to persist known chats');
    }
  }

  persistKnownContacts() {
    const contacts = [];
    for (const [id, displayName] of this.pushNameCache.entries()) {
      if (!id || !displayName) continue;
      contacts.push({ id: String(id), display_name: String(displayName) });
    }
    contacts.sort((a, b) => String(a.id).localeCompare(String(b.id)));
    try {
      atomicWriteJson(this.knownContactsPath, {
        updated_at: new Date().toISOString(),
        contacts,
      });
    } catch (err) {
      this.logger.warn({ err }, 'failed to persist known contacts');
    }
  }

  load() {
    const chatsData = readJson(this.knownChatsPath);
    const chats = Array.isArray(chatsData?.chats) ? chatsData.chats : [];
    for (const row of chats) {
      const chatId = this.normalizeId(row?.id || '');
      if (!chatId) continue;
      this.knownChats.set(chatId, {
        chatId,
        isGroup: !!row.is_group,
        name: String(row?.name || '').trim(),
        lastSenderName: String(row?.last_sender_name || '').trim(),
        updatedAtMs: Number(row?.updated_at_ms || 0) || Date.now(),
      });
    }

    const contactsData = readJson(this.knownContactsPath);
    const contacts = Array.isArray(contactsData?.contacts) ? contactsData.contacts : [];
    for (const row of contacts) {
      const contactId = this.normalizeId(row?.id || '');
      const displayName = String(row?.display_name || '').trim();
      if (!contactId || !displayName) continue;
      this.pushNameCache.set(contactId, displayName);
    }
  }

  canonicalize() {
    let chatsChanged = false;
    let contactsChanged = false;

    this.identity.forEachPair((lid, phone) => {
      const lidJid = `${String(lid || '').trim()}@lid`;
      const phoneJid = `${String(phone || '').trim()}@s.whatsapp.net`;
      if (!lid || !phone) return;

      const lidChat = this.knownChats.get(lidJid);
      if (lidChat) {
        const phoneChat = this.knownChats.get(phoneJid) || {};
        this.knownChats.set(phoneJid, {
          chatId: phoneJid,
          isGroup: !!(phoneChat.isGroup || lidChat.isGroup),
          name: String(phoneChat.name || lidChat.name || '').trim(),
          lastSenderName: String(phoneChat.lastSenderName || lidChat.lastSenderName || '').trim(),
          updatedAtMs: Math.max(
            Number(phoneChat.updatedAtMs || 0) || 0,
            Number(lidChat.updatedAtMs || 0) || 0,
            Date.now(),
          ),
        });
        this.knownChats.delete(lidJid);
        chatsChanged = true;
      }

      const lidName = String(this.pushNameCache.get(lidJid) || '').trim();
      const phoneName = String(this.pushNameCache.get(phoneJid) || '').trim();
      if (lidName && !phoneName) {
        this.pushNameCache.set(phoneJid, lidName);
        contactsChanged = true;
      }
      if (lidName) {
        this.pushNameCache.delete(lidJid);
        contactsChanged = true;
      }
    });

    if (chatsChanged) {
      this.persistKnownChats();
    }
    if (contactsChanged) {
      this.persistKnownContacts();
    }
  }

  rememberChat(chatId, { isGroup = false, name = '', lastSenderName = '' } = {}) {
    const normalizedChatId = this.normalizeId(chatId);
    if (!normalizedChatId) return;
    const existing = this.knownChats.get(normalizedChatId) || {};
    const merged = {
      chatId: normalizedChatId,
      isGroup: !!(isGroup || existing.isGroup),
      name: String(name || existing.name || '').trim(),
      lastSenderName: String(lastSenderName || existing.lastSenderName || '').trim(),
      updatedAtMs: Date.now(),
    };
    this.knownChats.set(normalizedChatId, merged);
    this.persistKnownChats();
  }

  rememberPushName(senderId, pushName) {
    const sid = this.normalizeId(senderId);
    const name = String(pushName || '').trim();
    if (!sid || !name) return;
    if (String(this.pushNameCache.get(sid) || '') === name) return;
    this.pushNameCache.set(sid, name);
    this.persistKnownContacts();
  }

  rememberChatsFromSnapshot(chats) {
    if (!Array.isArray(chats)) return;
    for (const chat of chats) {
      const chatId = this.normalizeId(chat?.id || chat?.jid || '');
      if (!chatId || chatId.toLowerCase().includes('status@broadcast')) continue;
      const isGroup = chatId.endsWith('@g.us') || chat?.isGroup === true || String(chat?.type || '').toLowerCase() === 'group';
      const name = String(chat?.name || chat?.subject || '').trim();
      this.rememberChat(chatId, { isGroup, name });
    }
  }

  rememberContactsFromSnapshot(contacts) {
    if (!Array.isArray(contacts)) return;
    const persistBatch = {};
    for (const contact of contacts) {
      if (contact?.lid && contact?.jid) {
        this.identity.learnPair(contact.lid, contact.jid, { persistBatch });
      }
      const contactId = this.normalizeId(contact?.id || '');
      const displayName = String(
        contact?.notify || contact?.name || contact?.verifiedName || ''
      ).trim();
      if (contactId && displayName) {
        this.rememberPushName(contactId, displayName);
      }
    }
    this.identity.persistBatch(persistBatch);
  }

  resolveDmDisplayName(chatId, row) {
    const fromCache = String(this.pushNameCache.get(chatId) || '').trim();
    if (fromCache) return fromCache;
    const fromRow = String(row?.name || row?.lastSenderName || '').trim();
    if (fromRow) return fromRow;
    if (this.debug && !this.unresolvedDmNameLogged.has(chatId)) {
      this.unresolvedDmNameLogged.add(chatId);
      console.log(JSON.stringify({
        event: 'dm_name_unresolved',
        chatId,
        hadRowName: !!String(row?.name || '').trim(),
        hadLastSenderName: !!String(row?.lastSenderName || '').trim(),
      }));
    }
    return chatId.split('@')[0];
  }

  extractPossibleSenderName(msg) {
    const candidates = [
      msg?.pushName,
      msg?.verifiedBizName,
      msg?.notifyName,
      msg?.name,
      msg?.participantName,
      msg?.chatName,
    ];
    for (const raw of candidates) {
      const name = String(raw || '').trim();
      if (!name) continue;
      if (/^\[.*\]$/.test(name)) continue;
      if (/^(image|video|audio|document)\s+received$/i.test(name)) continue;
      return name;
    }
    return '';
  }

  getPushName(id) {
    return this.pushNameCache.get(id);
  }

  getChat(id) {
    return this.knownChats.get(id);
  }

  allChats() {
    return this.knownChats.entries();
  }
}
