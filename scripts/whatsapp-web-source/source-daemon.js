#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { BackfillManager } = require('./backfill-manager');
const { buildClientOptions } = require('./browser-options');
const { ContactManager } = require('./contact-manager');
const { MemoryDiagnostics, memoryStatsMb } = require('./memory-diagnostics');
const { configureResourceBlocking, installRemoveMessageHook } = require('./page-hooks');
const { StatusWriter } = require('./status-writer');
const { StoreWriter } = require('./store-writer');
const {
  defaultUserAgent,
  ensureDir,
  expandPath,
  parseArgs,
} = require('./daemon-utils');
const {
  messageKey,
  normalizeMessage,
} = require('./normalization');

function loadWWebJS() {
  const localPath = path.resolve(__dirname, '../../..', 'wwebjs');
  if (process.env.HERMES_WWEBJS_LOCAL === '1' && fs.existsSync(path.join(localPath, 'index.js'))) {
    return require(localPath);
  }
  return require('whatsapp-web.js');
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

  const client = new Client(buildClientOptions({
    LocalAuth,
    clientId,
    authPath,
    userAgent,
    headless,
    executablePath,
    disableServiceWorkers,
  }));
  const memoryDiagnostics = new MemoryDiagnostics({
    intervalSeconds: memoryDiagnosticsInterval,
    isReady: () => wwebjsReady,
    rootPid: () => client.pupBrowser?.process?.()?.pid || null,
    page: () => client.pupPage,
    dbWriteable: () => !store.exitedError,
    status,
  });
  const contacts = new ContactManager({
    enabled: contactSnapshotEnabled,
    intervalSeconds: contactSnapshotInterval,
    activeSince,
    dbPath,
    store,
    client,
    page: () => client.pupPage,
    status,
    isReady: () => wwebjsReady,
    dbWriteable: () => !store.exitedError,
  });
  const backfill = new BackfillManager({
    client,
    store,
    status,
    backfillLimit,
    persistMessage,
    dbWriteable: () => !store.exitedError,
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
    contacts.stop();
    memoryDiagnostics.stop();
    await client.destroy().catch(() => {});
    store.close();
    status.flush();
    process.exit(1);
  }

  process.once('uncaughtException', shutdownFatal);
  process.once('unhandledRejection', shutdownFatal);

  async function persistMessage(message, source, options = {}) {
    const row = normalizeMessage(message, source);
    const result = await store.command('upsert_message', { row });
    await contacts.persistForMessageRow(row, Boolean(options.enrichContacts)).catch((error) => {
      console.warn(`contact persist for ${row.msg_key} failed:`, error.message);
    });
    status.write({
      state: 'ready',
      wwebjs_ready: true,
      db_writeable: !store.exitedError,
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
      db_writeable: !store.exitedError,
      error: null,
      last_remove_at: Math.floor(Date.now() / 1000),
      last_removed_msg_key: msgKey,
    });
  }

  function persistFailed(label, error) {
    console.error(`${label} failed:`, error);
    status.write(
      { state: 'degraded', wwebjs_ready: wwebjsReady, db_writeable: !store.exitedError, error: error.message },
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
    status.write({ state: 'auth_failure', wwebjs_ready: false, db_writeable: !store.exitedError, error: String(message) }, { immediate: true });
    contacts.stop();
    memoryDiagnostics.stop();
    client.destroy().catch(() => {});
    store.close();
    status.flush();
    process.exit(1);
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
        const result = await backfill.chatsSince(backfillSince);
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
        const result = await backfill.chatMessages(chat, backfillChat, backfillSince);
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

    await contacts.snapshot().catch((error) => contacts.snapshotFailed(error));
    await contacts.pruneScopeOnce().catch((error) => persistFailed('scope prune', error));
    contacts.schedule();

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
    console.error('WhatsApp Web source disconnected:', reason);
    wwebjsReady = false;
    status.write({ state: 'disconnected', wwebjs_ready: false, db_writeable: !store.exitedError, error: String(reason) }, { immediate: true });
    contacts.stop();
    memoryDiagnostics.stop();
    // The library calls destroy() itself before emitting this event, so no recovery is possible.
    // Exit so the process death is visible and the gateway can restart.
    store.close();
    status.flush();
    process.exit(1);
  });

  process.on('SIGINT', async () => {
    status.write({ state: 'stopping', wwebjs_ready: false, db_writeable: !store.exitedError }, { immediate: true });
    contacts.stop();
    memoryDiagnostics.stop();
    await client.destroy().catch(() => {});
    store.close();
    status.flush();
    process.exit(0);
  });
  process.on('SIGTERM', async () => {
    status.write({ state: 'stopping', wwebjs_ready: false, db_writeable: !store.exitedError }, { immediate: true });
    contacts.stop();
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
