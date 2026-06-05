'use strict';

const { timestampLabel } = require('./daemon-utils');
const {
  normalizeContactRow,
  sparseContactRow,
} = require('./normalization');

async function readContactSnapshot(page, scope) {
  const rows = await page.evaluate((snapshotScope) => {
    const serializeId = (value) => {
      if (!value) return null;
      if (typeof value === 'string') return value;
      if (value._serialized) return value._serialized;
      if (value.id && value.id._serialized) return value.id._serialized;
      if (value.user && value.server) return `${value.user}@${value.server}`;
      return null;
    };
    const localId = (value) => String(value || '').replace(/:.*@/, '@').split('@', 1)[0];
    const allowedIds = new Set(snapshotScope.contactIds || []);
    const allowedLocalIds = new Set(snapshotScope.contactLocalIds || []);
    const inScope = (id, isMe) => {
      if (isMe) return true;
      if (allowedIds.has(id)) return true;
      return allowedLocalIds.has(localId(id));
    };
    const pick = (model, keys) => {
      for (const key of keys) {
        const value = model && model[key];
        if (typeof value === 'string' && value.trim()) return value;
      }
      return null;
    };
    const requireFn = window.require || window.Store?.require;
    const collections = typeof requireFn === 'function' ? requireFn('WAWebCollections') : null;
    const contacts = collections?.Contact?.getModelsArray?.() || [];
    const out = [];
    for (const contact of contacts) {
      try {
        const id = serializeId(contact.id);
        if (!id) continue;
        const isMe = Boolean(contact.isMe);
        if (!inScope(id, isMe)) continue;
        const row = {
          id,
          name: pick(contact, ['name', 'formattedName']),
          shortName: pick(contact, ['shortName', 'displayName']),
          pushname: pick(contact, ['pushname', 'pushName', 'notifyName']),
          verifiedName: pick(contact, ['verifiedName', 'verifiedLevelName']),
          isMe,
          isUser: Boolean(contact.isUser),
          isGroup: Boolean(contact.isGroup),
        };
        out.push({ ...row, raw: row });
      } catch (_error) {
        // Internal WhatsApp models can include device WIDs that break higher-level APIs.
      }
    }
    return out;
  }, {
    contactIds: Array.from(scope.contactIds || []),
    contactLocalIds: Array.from(scope.contactLocalIds || []),
  });
  return rows.map(normalizeContactRow).filter(Boolean);
}

class ContactManager {
  constructor({
    enabled,
    intervalSeconds,
    activeSince,
    dbPath,
    store,
    client,
    page,
    status,
    isReady,
    dbWriteable,
    logger = console,
    now = () => Date.now(),
    makeTimestampLabel = timestampLabel,
    readSnapshot = readContactSnapshot,
  }) {
    this.enabled = enabled;
    this.intervalSeconds = intervalSeconds;
    this.activeSince = activeSince;
    this.dbPath = dbPath;
    this.store = store;
    this.client = client;
    this.page = page;
    this.status = status;
    this.isReady = isReady;
    this.dbWriteable = dbWriteable;
    this.logger = logger;
    this.now = now;
    this.makeTimestampLabel = makeTimestampLabel;
    this.readSnapshot = readSnapshot;
    this.running = false;
    this.timer = null;
  }

  async scope() {
    const result = await this.store.command('in_scope_contact_ids', { active_since: this.activeSince });
    return {
      contactIds: new Set(result.contact_ids || []),
      contactLocalIds: new Set(result.contact_local_ids || []),
    };
  }

  contactIdsFromMessageRow(row) {
    return [
      row.chat_id,
      row.from_id,
      row.to_id,
      row.author_id,
    ].filter(Boolean);
  }

  async rowForId(contactId) {
    try {
      const contact = await this.client.getContactById(contactId);
      return normalizeContactRow(contact) || sparseContactRow(contactId);
    } catch (_error) {
      return sparseContactRow(contactId);
    }
  }

  async persistForMessageRow(row, enrich) {
    const contactIds = Array.from(new Set(this.contactIdsFromMessageRow(row)));
    for (const contactId of contactIds) {
      const contactRow = enrich ? await this.rowForId(contactId) : sparseContactRow(contactId);
      if (contactRow) await this.store.command('upsert_contact', { row: contactRow });
    }
  }

  async snapshot() {
    if (!this.enabled) return;
    if (this.running) return;
    this.running = true;
    try {
      const scope = await this.scope();
      const rows = await this.readSnapshot(this.page(), scope);
      let persisted = 0;
      for (const row of rows) {
        await this.store.command('upsert_contact', { row });
        persisted += 1;
      }
      this.status.write({
        state: 'ready',
        wwebjs_ready: true,
        db_writeable: this.dbWriteable(),
        error: null,
        last_contact_snapshot_at: Math.floor(this.now() / 1000),
        last_contact_snapshot_rows: persisted,
        last_contact_snapshot_scope_ids: scope.contactIds.size,
      });
      this.logger.log(`contact snapshot: ${persisted} persisted from ${scope.contactIds.size} scoped ids`);
    } finally {
      this.running = false;
    }
  }

  snapshotFailed(error) {
    this.logger.error('contact snapshot failed:', error);
    this.status.write(
      {
        state: this.isReady() ? 'ready' : 'degraded',
        wwebjs_ready: this.isReady(),
        db_writeable: this.dbWriteable(),
        last_contact_snapshot_error: error.message,
        last_contact_snapshot_error_at: Math.floor(this.now() / 1000),
      },
      { immediate: true },
    );
  }

  async pruneScopeOnce() {
    if (this.activeSince <= 0) return;
    const metadataKey = `prune_scope:${this.activeSince}`;
    const existing = await this.store.command('get_metadata', { key: metadataKey });
    if (existing.value === '1') return;
    const backupPath = `${this.dbPath}.bak-prune-${this.makeTimestampLabel()}`;
    const result = await this.store.command('prune_scope', {
      active_since: this.activeSince,
      backup_path: backupPath,
    });
    await this.store.command('set_metadata', { key: metadataKey, value: '1' });
    this.status.write({
      state: 'ready',
      wwebjs_ready: true,
      db_writeable: true,
      error: null,
      last_prune_at: Math.floor(this.now() / 1000),
      last_prune_active_since: this.activeSince,
      last_prune_deleted_chats: result.deleted_chats,
      last_prune_deleted_contacts: result.deleted_contacts,
      last_prune_backup_path: result.backup_path,
    });
    this.logger.log(
      `scope prune: ${result.deleted_chats} chats and ${result.deleted_contacts} contacts deleted; ` +
      `backup ${result.backup_path}`,
    );
  }

  schedule() {
    if (!this.enabled || this.intervalSeconds <= 0 || this.timer) return;
    this.timer = setInterval(() => {
      if (!this.isReady()) return;
      this.snapshot().catch((error) => this.snapshotFailed(error));
    }, this.intervalSeconds * 1000);
    if (this.timer.unref) this.timer.unref();
  }

  stop() {
    if (!this.timer) return;
    clearInterval(this.timer);
    this.timer = null;
  }
}

module.exports = {
  ContactManager,
  readContactSnapshot,
};
