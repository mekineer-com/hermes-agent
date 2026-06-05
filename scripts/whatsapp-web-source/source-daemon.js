#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { MemoryDiagnostics, memoryStatsMb } = require('./memory-diagnostics');
const { StatusWriter } = require('./status-writer');
const { StoreWriter } = require('./store-writer');
const {
  defaultUserAgent,
  ensureDir,
  expandPath,
  parseArgs,
  timestampLabel,
} = require('./daemon-utils');
const {
  idSerialized,
  isConversationChatId,
  jidLocal,
  messageKey,
  messageTimestamp,
  normalizeChat,
  normalizeContactRow,
  normalizeMessage,
  sparseContactRow,
} = require('./normalization');

function loadWWebJS() {
  const localPath = path.resolve(__dirname, '../../..', 'wwebjs');
  if (process.env.HERMES_WWEBJS_LOCAL === '1' && fs.existsSync(path.join(localPath, 'index.js'))) {
    return require(localPath);
  }
  return require('whatsapp-web.js');
}

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

async function configureResourceBlocking(page) {
  await page.setRequestInterception(true);
  page.on('request', (request) => {
    const type = request.resourceType();
    if (type === 'image' || type === 'media' || type === 'font') {
      request.abort().catch(() => {});
      return;
    }
    request.continue().catch(() => {});
  });
}

async function installRemoveMessageHook(page) {
  await page.evaluate(() => {
    if (window.__hermesWebSourceRemoveHookInstalled) return;
    const requireFn = window.require;
    const collections = typeof requireFn === 'function' ? requireFn('WAWebCollections') : null;
    const msgCollection = collections?.Msg;
    if (!msgCollection?.on) throw new Error('WAWebCollections.Msg remove hook unavailable');
    window.__hermesWebSourceRemoveHookInstalled = true;
    msgCollection.on('remove', (msg) => {
      const model = window.WWebJS?.getMessageModel ? window.WWebJS.getMessageModel(msg) : msg;
      window.__hermesWebSourceMessageRemoved(model);
    });
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || args.h) {
    console.log(`Usage: node source-daemon.js [options]\n\nOptions:\n  --db PATH                 SQLite projection DB (default ~/.hermes/whatsapp/web_source.db)\n  --status PATH             JSON status path (default ~/.hermes/whatsapp/web_source_status.json)\n  --auth PATH               LocalAuth data dir (default ~/.hermes/whatsapp/wwebjs_auth)\n  --client-id ID            LocalAuth client id (default memu-web-source)\n  --backfill-chat JID       Backfill one chat after ready\n  --backfill-since EPOCH    Backfill all chats at/after this Unix timestamp\n  --active-since EPOCH      Storage scope cutoff (defaults to --backfill-since)\n  --backfill-limit N        Backfill limit per chat (default 100, max 5000)\n  --no-contact-snapshot     Do not snapshot WhatsApp contact/name models after ready\n  --contact-snapshot-interval SECONDS\n                            Refresh contacts periodically (default 900, 0 disables)\n  --memory-diagnostics-interval SECONDS\n                            Refresh Chromium/page memory diagnostics (default 60, 0 disables)\n  --user-agent UA           Override WhatsApp Web browser user-agent\n  --disable-service-workers Experimental: launch Chromium with ServiceWorker disabled\n  --headful                 Show Chromium instead of running headless\n  --exit-after-backfill     Exit after bounded backfill\n  --no-resource-block       Do not block image/media/font requests after ready\n`);
    return;
  }
  const { Client, LocalAuth, Events } = loadWWebJS();

  const dbPath = path.resolve(expandPath(args.db || '~/.hermes/whatsapp/web_source.db'));
  const statusPath = path.resolve(expandPath(args.status || '~/.hermes/whatsapp/web_source_status.json'));
  const authPath = path.resolve(expandPath(args.auth || '~/.hermes/whatsapp/wwebjs_auth'));
  const clientId = String(args['client-id'] || 'memu-web-source');
  const backfillChat = args['backfill-chat'] ? String(args['backfill-chat']) : null;
  const backfillSince = Math.max(parseInt(args['backfill-since'] || '0', 10) || 0, 0);
  const activeSince = Math.max(parseInt(args['active-since'] || String(backfillSince), 10) || 0, 0);
  const backfillLimit = Math.min(Math.max(parseInt(args['backfill-limit'] || '100', 10) || 100, 1), 5000);
  const contactSnapshotEnabled = args['no-contact-snapshot'] !== true;
  const contactSnapshotInterval = Math.max(parseInt(args['contact-snapshot-interval'] || '900', 10) || 0, 0);
  const memoryDiagnosticsInterval = Math.max(parseInt(args['memory-diagnostics-interval'] || '60', 10) || 0, 0);
  const exitAfterBackfill = Boolean(args['exit-after-backfill']);
  const disableServiceWorkers = Boolean(args['disable-service-workers']);
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH || undefined;
  const userAgent = args['user-agent'] ? String(args['user-agent']) : defaultUserAgent();
  const headless = args.headful ? false : true;
  let wwebjsReady = false;
  let contactSnapshotRunning = false;
  let contactSnapshotTimer = null;
  let removeMessageHookExposed = false;

  ensureDir(dbPath);
  ensureDir(statusPath);
  const status = new StatusWriter(statusPath, { stats: memoryStatsMb });
  status.write({ state: 'starting', wwebjs_ready: false, db_writeable: false }, { immediate: true });

  const store = new StoreWriter(dbPath, (error) => {
    console.error(error.message);
    status.write(
      { state: 'degraded', wwebjs_ready: wwebjsReady, db_writeable: false, error: error.message },
      { immediate: true },
    );
  });
  await store.command('ping');
  status.write({ state: 'starting', wwebjs_ready: false, db_writeable: true }, { immediate: true });

  const client = new Client({
    authStrategy: new LocalAuth({ clientId, dataPath: authPath }),
    ...(userAgent ? { userAgent } : {}),
    puppeteer: {
      headless,
      ...(executablePath ? { executablePath } : {}),
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-accelerated-2d-canvas',
        '--no-first-run',
        '--no-zygote',
        '--disable-gpu',
        '--disable-extensions',
        '--disable-software-rasterizer',
        '--mute-audio',
        ...(disableServiceWorkers ? ['--disable-features=ServiceWorker'] : []),
      ],
    },
  });
  const memoryDiagnostics = new MemoryDiagnostics({
    intervalSeconds: memoryDiagnosticsInterval,
    isReady: () => wwebjsReady,
    rootPid: () => client.pupBrowser?.process?.()?.pid || null,
    page: () => client.pupPage,
    dbWriteable: () => !store.exitedError,
    status,
  });
  let fatalHandled = false;

  async function shutdownFatal(error) {
    if (fatalHandled) return;
    fatalHandled = true;
    const message = error?.stack || error?.message || String(error);
    console.error(message);
    status.write(
      { state: 'degraded', wwebjs_ready: wwebjsReady, db_writeable: !store.exitedError, error: error?.message || String(error) },
      { immediate: true },
    );
    if (contactSnapshotTimer) clearInterval(contactSnapshotTimer);
    memoryDiagnostics.stop();
    await client.destroy().catch(() => {});
    store.close();
    status.flush();
    process.exit(1);
  }

  process.once('uncaughtException', shutdownFatal);
  process.once('unhandledRejection', shutdownFatal);

  async function contactScope() {
    const result = await store.command('in_scope_contact_ids', { active_since: activeSince });
    return {
      contactIds: new Set(result.contact_ids || []),
      contactLocalIds: new Set(result.contact_local_ids || []),
    };
  }

  function contactIdsFromMessageRow(row) {
    return [
      row.chat_id,
      row.from_id,
      row.to_id,
      row.author_id,
    ].filter(Boolean);
  }

  async function contactRowForId(contactId) {
    try {
      const contact = await client.getContactById(contactId);
      return normalizeContactRow(contact) || sparseContactRow(contactId);
    } catch (_error) {
      return sparseContactRow(contactId);
    }
  }

  async function persistContactsForMessageRow(row, enrich) {
    const contactIds = Array.from(new Set(contactIdsFromMessageRow(row)));
    for (const contactId of contactIds) {
      const contactRow = enrich ? await contactRowForId(contactId) : sparseContactRow(contactId);
      if (contactRow) await store.command('upsert_contact', { row: contactRow });
    }
  }

  async function persistMessage(message, source, options = {}) {
    const row = normalizeMessage(message, source);
    const result = await store.command('upsert_message', { row });
    await persistContactsForMessageRow(row, Boolean(options.enrichContacts)).catch((error) => {
      console.warn(`contact persist for ${row.msg_key} failed:`, error.message);
    });
    status.write({
      state: 'ready',
      wwebjs_ready: true,
      db_writeable: true,
      error: null,
      last_event_at: Math.floor(Date.now() / 1000),
      last_msg_key: row.msg_key,
    });
    return result;
  }

  async function markRemovedMessage(raw) {
    const msgKey = messageKey(raw);
    if (!msgKey) return;
    await store.command('mark_revoked', {
      row: {
        msg_key: msgKey,
        source: 'event:message_remove',
        raw: raw || {},
      },
    });
    status.write({
      state: 'ready',
      wwebjs_ready: true,
      db_writeable: true,
      error: null,
      last_remove_at: Math.floor(Date.now() / 1000),
      last_removed_msg_key: msgKey,
    });
  }

  async function snapshotContacts() {
    if (!contactSnapshotEnabled) return;
    if (contactSnapshotRunning) return;
    contactSnapshotRunning = true;
    try {
      const scope = await contactScope();
      const rows = await readContactSnapshot(client.pupPage, scope);
      let persisted = 0;
      for (const row of rows) {
        await store.command('upsert_contact', { row });
        persisted += 1;
      }
      status.write({
        state: 'ready',
        wwebjs_ready: true,
        db_writeable: true,
        error: null,
        last_contact_snapshot_at: Math.floor(Date.now() / 1000),
        last_contact_snapshot_rows: persisted,
        last_contact_snapshot_scope_ids: scope.contactIds.size,
      });
      console.log(`contact snapshot: ${persisted} persisted from ${scope.contactIds.size} scoped ids`);
    } finally {
      contactSnapshotRunning = false;
    }
  }

  async function pruneScopeOnce() {
    if (activeSince <= 0) return;
    const metadataKey = `prune_scope:${activeSince}`;
    const existing = await store.command('get_metadata', { key: metadataKey });
    if (existing.value === '1') return;
    const backupPath = `${dbPath}.bak-prune-${timestampLabel()}`;
    const result = await store.command('prune_scope', {
      active_since: activeSince,
      backup_path: backupPath,
    });
    await store.command('set_metadata', { key: metadataKey, value: '1' });
    status.write({
      state: 'ready',
      wwebjs_ready: true,
      db_writeable: true,
      error: null,
      last_prune_at: Math.floor(Date.now() / 1000),
      last_prune_active_since: activeSince,
      last_prune_deleted_chats: result.deleted_chats,
      last_prune_deleted_contacts: result.deleted_contacts,
      last_prune_backup_path: result.backup_path,
    });
    console.log(
      `scope prune: ${result.deleted_chats} chats and ${result.deleted_contacts} contacts deleted; ` +
      `backup ${result.backup_path}`,
    );
  }

  function scheduleContactSnapshots() {
    if (!contactSnapshotEnabled || contactSnapshotInterval <= 0 || contactSnapshotTimer) return;
    contactSnapshotTimer = setInterval(() => {
      if (!wwebjsReady) return;
      snapshotContacts().catch((error) => contactSnapshotFailed(error));
    }, contactSnapshotInterval * 1000);
    if (contactSnapshotTimer.unref) contactSnapshotTimer.unref();
  }

  async function persistBackfillMessages(messages, source, since) {
    const startedAt = Math.floor(Date.now() / 1000);
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
      const result = await persistMessage(message, source);
      if (result.action === 'insert') inserted += 1;
      else updated += 1;
    }
    return { inserted, updated, skippedBeforeSince, presentMsgKeys, oldestPersistedTimestamp, startedAt };
  }

  function oldestMessageTimestamp(messages) {
    let oldest = null;
    for (const message of messages) {
      const ts = messageTimestamp(message);
      if (!ts) continue;
      if (oldest === null || ts < oldest) oldest = ts;
    }
    return oldest;
  }

  async function backfillChatMessages(chat, chatId, since) {
    const messages = await chat.fetchMessages({ limit: backfillLimit });
    const result = await persistBackfillMessages(messages, 'backfill:fetchMessages', since);
    let reconciledRevoked = 0;
    if (result.presentMsgKeys.length > 0 && result.oldestPersistedTimestamp !== null) {
      const reconcile = await store.command('mark_missing_in_chat_window', {
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
      await store.command('upsert_chat', { row: normalizeChat(chat) });
    }
    const oldest = oldestMessageTimestamp(messages);
    const incomplete = since > 0 && messages.length >= backfillLimit && oldest !== null && oldest >= since;
    return { ...result, fetched: messages.length, chatId, incomplete, reconciledRevoked };
  }

  async function backfillChatsSince(since) {
    if (!since) return null;
    status.write({
      state: 'backfilling',
      wwebjs_ready: true,
      db_writeable: true,
      backfill_since: since,
    }, { immediate: true });
    const chats = await client.getChats();
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
        const result = await backfillChatMessages(chat, chatId, since);
        fetched += result.fetched;
        inserted += result.inserted;
        updated += result.updated;
        skippedBeforeSince += result.skippedBeforeSince;
        reconciledRevoked += result.reconciledRevoked;
        if (result.inserted || result.updated) backfilledChats += 1;
        if (result.incomplete) incompleteChatIds.push(chatId);
      } catch (error) {
        console.error(`backfill ${chatId} failed:`, error);
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

  function persistFailed(label, error) {
    console.error(`${label} failed:`, error);
    status.write(
      { state: 'degraded', wwebjs_ready: wwebjsReady, db_writeable: !store.exitedError, error: error.message },
      { immediate: true },
    );
  }

  function contactSnapshotFailed(error) {
    console.error('contact snapshot failed:', error);
    status.write(
      {
        state: wwebjsReady ? 'ready' : 'degraded',
        wwebjs_ready: wwebjsReady,
        db_writeable: !store.exitedError,
        last_contact_snapshot_error: error.message,
        last_contact_snapshot_error_at: Math.floor(Date.now() / 1000),
      },
      { immediate: true },
    );
  }

  client.on('qr', (qr) => {
    console.log('Pair WhatsApp Web with this QR payload:');
    console.log(qr);
    status.write({ state: 'pairing', wwebjs_ready: false, db_writeable: true }, { immediate: true });
  });

  client.on('authenticated', () => {
    console.log('WhatsApp Web source authenticated');
    status.write({ state: 'authenticated', wwebjs_ready: false, db_writeable: true }, { immediate: true });
  });

  client.on('auth_failure', (message) => {
    console.error('WhatsApp Web source auth failure:', message);
    status.write({ state: 'auth_failure', wwebjs_ready: false, db_writeable: true, error: String(message) }, { immediate: true });
  });

  client.on('ready', async () => {
    console.log('WhatsApp Web source ready');
    wwebjsReady = true;
    status.write({ state: 'ready', wwebjs_ready: true, db_writeable: true, error: null }, { immediate: true });
    if (args['no-resource-block'] !== true) {
      try {
        await configureResourceBlocking(client.pupPage);
      } catch (error) {
        console.warn('resource blocking not enabled:', error.message);
      }
    }
    try {
      if (!removeMessageHookExposed) {
        await client.pupPage.exposeFunction('__hermesWebSourceMessageRemoved', markRemovedMessage);
        removeMessageHookExposed = true;
      }
      await installRemoveMessageHook(client.pupPage);
    } catch (error) {
      console.warn('message remove hook not enabled:', error.message);
      status.write({
        last_remove_hook_error: error.message,
        last_remove_hook_error_at: Math.floor(Date.now() / 1000),
      });
    }
    memoryDiagnostics.schedule();

    if (backfillSince > 0) {
      try {
        const result = await backfillChatsSince(backfillSince);
        console.log(
          `backfill since ${backfillSince}: ${result.scannedChats} chats, ${result.fetched} fetched ` +
          `(${result.inserted} inserted, ${result.updated} updated)`,
        );
        const backfillStatus = {
          state: 'ready',
          wwebjs_ready: true,
          db_writeable: true,
          error: null,
          last_backfill_at: Math.floor(Date.now() / 1000),
          last_backfill_since: backfillSince,
          last_backfill_chats: result.backfilledChats,
          last_backfill_rows: result.fetched,
          last_backfill_inserted: result.inserted,
          last_backfill_updated: result.updated,
          last_backfill_skipped_before_since: result.skippedBeforeSince,
          last_backfill_reconciled_revoked: result.reconciledRevoked,
        };
        if (result.incompleteChatIds.length > 0) {
          backfillStatus.state = 'degraded';
          backfillStatus.error = (
            `backfill incomplete for ${result.incompleteChatIds.length} chat(s); ` +
            'increase --backfill-limit to reach --backfill-since'
          );
          backfillStatus.last_backfill_incomplete_chats = result.incompleteChatIds.length;
          backfillStatus.last_backfill_incomplete_chat_ids = result.incompleteChatIds.slice(0, 10);
        }
        status.write(backfillStatus, { immediate: true });
      } catch (error) {
        console.error('backfill failed:', error);
        status.write(
          { state: 'degraded', wwebjs_ready: true, db_writeable: !store.exitedError, error: error.message },
          { immediate: true },
        );
      }
    }

    if (backfillChat) {
      try {
        const chat = await client.getChatById(backfillChat);
        const result = await backfillChatMessages(chat, backfillChat, backfillSince);
        console.log(
          `backfill ${backfillChat}: ${result.fetched} rows ` +
          `(${result.inserted} inserted, ${result.updated} updated)`,
        );
        status.write({
          state: 'ready',
          wwebjs_ready: true,
          db_writeable: true,
          error: null,
          last_backfill_at: Math.floor(Date.now() / 1000),
          last_backfill_chat: backfillChat,
          last_backfill_rows: result.fetched,
          last_backfill_inserted: result.inserted,
          last_backfill_updated: result.updated,
        }, { immediate: true });
      } catch (error) {
        console.error('backfill failed:', error);
        status.write(
          { state: 'degraded', wwebjs_ready: true, db_writeable: !store.exitedError, error: error.message },
          { immediate: true },
        );
      }
    }

    await snapshotContacts().catch((error) => contactSnapshotFailed(error));
    await pruneScopeOnce().catch((error) => persistFailed('scope prune', error));
    scheduleContactSnapshots();

    if (exitAfterBackfill && (backfillSince > 0 || backfillChat)) {
      await client.destroy();
      store.close();
      status.flush();
      process.exit(0);
    }
  });

  client.on(Events.MESSAGE_CREATE, (message) => {
    persistMessage(message, 'event:message_create', { enrichContacts: true })
      .catch((error) => persistFailed('persist message_create', error));
  });

  client.on(Events.MESSAGE_RECEIVED, (message) => {
    persistMessage(message, 'event:message', { enrichContacts: true })
      .catch((error) => persistFailed('persist message', error));
  });

  client.on(Events.MESSAGE_EDIT, (message) => {
    persistMessage(message, 'event:message_edit', { enrichContacts: true })
      .catch((error) => persistFailed('persist message_edit', error));
  });

  client.on(Events.MESSAGE_CIPHERTEXT, (message) => {
    persistMessage(message, 'event:message_ciphertext', { enrichContacts: true })
      .catch((error) => persistFailed('persist ciphertext', error));
  });

  client.on(Events.MESSAGE_CIPHERTEXT_FAILED, (message) => {
    persistMessage(message, 'event:message_ciphertext_failed', { enrichContacts: true })
      .catch((error) => persistFailed('persist ciphertext_failed', error));
  });

  client.on(Events.MESSAGE_ACK, (message, ack) => {
    const msgKey = messageKey(message);
    if (!msgKey) return;
    store.command('update_ack', { row: { msg_key: msgKey, ack } }).catch((error) => persistFailed('ack update', error));
  });

  client.on(Events.MESSAGE_REVOKED_ME, (message) => {
    const msgKey = messageKey(message);
    if (!msgKey) return;
    store.command('mark_revoked', { row: { msg_key: msgKey, source: 'event:message_revoke_me', raw: message.rawData || {} } })
      .catch((error) => persistFailed('revoke_me update', error));
  });

  client.on(Events.MESSAGE_REVOKED_EVERYONE, (message, revokedMessage) => {
    const target = revokedMessage || message;
    const msgKey = messageKey(target);
    if (!msgKey) return;
    store.command('mark_revoked', { row: { msg_key: msgKey, source: 'event:message_revoke_everyone', raw: message.rawData || {} } })
      .catch((error) => persistFailed('revoke_everyone update', error));
  });

  client.on('disconnected', (reason) => {
    console.warn('WhatsApp Web source disconnected:', reason);
    wwebjsReady = false;
    if (contactSnapshotTimer) {
      clearInterval(contactSnapshotTimer);
      contactSnapshotTimer = null;
    }
    memoryDiagnostics.stop();
    status.write({ state: 'disconnected', wwebjs_ready: false, db_writeable: !store.exitedError, error: String(reason) }, { immediate: true });
  });

  process.on('SIGINT', async () => {
    status.write({ state: 'stopping', wwebjs_ready: false, db_writeable: !store.exitedError }, { immediate: true });
    if (contactSnapshotTimer) clearInterval(contactSnapshotTimer);
    memoryDiagnostics.stop();
    await client.destroy().catch(() => {});
    store.close();
    status.flush();
    process.exit(0);
  });
  process.on('SIGTERM', async () => {
    status.write({ state: 'stopping', wwebjs_ready: false, db_writeable: !store.exitedError }, { immediate: true });
    if (contactSnapshotTimer) clearInterval(contactSnapshotTimer);
    memoryDiagnostics.stop();
    await client.destroy().catch(() => {});
    store.close();
    status.flush();
    process.exit(0);
  });

  await client.initialize().catch(shutdownFatal);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
