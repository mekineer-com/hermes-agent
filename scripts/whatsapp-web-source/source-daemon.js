#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync, spawn } = require('child_process');

function loadWWebJS() {
  const localPath = path.resolve(__dirname, '../../..', 'wwebjs');
  if (process.env.HERMES_WWEBJS_LOCAL === '1' && fs.existsSync(path.join(localPath, 'index.js'))) {
    return require(localPath);
  }
  return require('whatsapp-web.js');
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith('--')) continue;
    const key = arg.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) {
      out[key] = true;
    } else {
      out[key] = next;
      i += 1;
    }
  }
  return out;
}

function expandPath(value) {
  const input = String(value || '');
  if (input === '~') return os.homedir();
  if (input.startsWith('~/')) return path.join(os.homedir(), input.slice(2));
  return input.replace(/^\$HOME(?=\/|$)/, os.homedir());
}

function ensureDir(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function jidLocal(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  return raw.replace(/:.*@/, '@').split('@', 1)[0];
}

function idSerialized(value) {
  if (!value) return null;
  if (typeof value === 'string') return value;
  if (value._serialized) return value._serialized;
  if (value.id?._serialized) return value.id._serialized;
  return String(value);
}

function defaultUserAgent() {
  if (process.env.HERMES_WWEBJS_USER_AGENT) return process.env.HERMES_WWEBJS_USER_AGENT;
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
  if (!executablePath) return undefined;
  try {
    const version = execFileSync(executablePath, ['--version'], { encoding: 'utf8', timeout: 5000 }).trim();
    const match = version.match(/(?:Chromium|Chrome) (\d+)\./);
    if (match) {
      return `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${match[1]}.0.0.0 Safari/537.36`;
    }
  } catch (error) {
    return undefined;
  }
  return undefined;
}

function messageKey(message) {
  return message?.id?._serialized || message?.rawData?.id?._serialized || null;
}

function messageChatId(message) {
  if (message.fromMe) return idSerialized(message.to);
  return idSerialized(message.from);
}

function mediaPlaceholder(message) {
  if (!message.hasMedia) return null;
  switch (message.type) {
    case 'image': return '[image]';
    case 'video': return '[video]';
    case 'ptt': return '[voice note]';
    case 'audio': return '[audio]';
    case 'document': return '[document]';
    case 'sticker': return '[sticker]';
    default: return `[${message.type || 'media'}]`;
  }
}

function normalizeMessage(message, source) {
  const msgKey = messageKey(message);
  const chatId = messageChatId(message);
  if (!msgKey) throw new Error('message has no serialized id');
  if (!chatId) throw new Error(`message ${msgKey} has no chat id`);

  const fromId = idSerialized(message.from);
  const toId = idSerialized(message.to);
  const authorId = idSerialized(message.author);
  const body = message.body || '';
  return {
    msg_key: msgKey,
    chat_id: chatId,
    chat_local_id: jidLocal(chatId),
    from_me: Boolean(message.fromMe),
    timestamp: Number(message.timestamp || message.rawData?.t || 0),
    type: String(message.type || 'unknown'),
    body,
    author_id: authorId,
    author_local_id: jidLocal(authorId),
    from_id: fromId,
    from_local_id: jidLocal(fromId),
    to_id: toId,
    to_local_id: jidLocal(toId),
    has_media: Boolean(message.hasMedia),
    media_placeholder: mediaPlaceholder(message),
    ack: message.ack ?? null,
    revoked: message.type === 'revoked',
    revoke_source: message.type === 'revoked' ? source : null,
    source,
    raw: message.rawData || {},
  };
}

function normalizeChat(chat) {
  const chatId = idSerialized(chat.id);
  return {
    chat_id: chatId,
    chat_local_id: jidLocal(chatId),
    name: chat.name || null,
    is_group: Boolean(chat.isGroup),
    last_timestamp: chat.timestamp || null,
    raw: chat.rawData || {},
  };
}

function normalizeContactRow(contact) {
  const contactId = idSerialized(contact.id || contact.contactId || contact);
  if (!contactId) return null;
  return {
    contact_id: contactId,
    contact_local_id: jidLocal(contactId),
    name: contact.name || null,
    short_name: contact.shortName || contact.short_name || null,
    push_name: contact.pushname || contact.pushName || contact.push_name || null,
    verified_name: contact.verifiedName || contact.verified_name || null,
    is_me: Boolean(contact.isMe),
    is_user: Boolean(contact.isUser),
    is_group: Boolean(contact.isGroup),
    raw: contact.raw || contact,
  };
}

async function readContactSnapshot(page) {
  const rows = await page.evaluate(() => {
    const serializeId = (value) => {
      if (!value) return null;
      if (typeof value === 'string') return value;
      if (value._serialized) return value._serialized;
      if (value.id && value.id._serialized) return value.id._serialized;
      if (value.user && value.server) return `${value.user}@${value.server}`;
      return null;
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
        const row = {
          id,
          name: pick(contact, ['name', 'formattedName']),
          shortName: pick(contact, ['shortName', 'displayName']),
          pushname: pick(contact, ['pushname', 'pushName', 'notifyName']),
          verifiedName: pick(contact, ['verifiedName', 'verifiedLevelName']),
          isMe: Boolean(contact.isMe),
          isUser: Boolean(contact.isUser),
          isGroup: Boolean(contact.isGroup),
        };
        out.push({ ...row, raw: row });
      } catch (_error) {
        // Internal WhatsApp models can include device WIDs that break higher-level APIs.
      }
    }
    return out;
  });
  return rows.map(normalizeContactRow).filter(Boolean);
}

class StoreWriter {
  constructor(dbPath, onExit) {
    this.nextId = 1;
    this.pending = new Map();
    this.exitedError = null;
    this.closing = false;
    const scriptPath = path.join(__dirname, 'store.py');
    this.proc = spawn('python3', [scriptPath, '--db', dbPath], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.proc.stdout.setEncoding('utf8');
    this.proc.stderr.setEncoding('utf8');
    let stdout = '';
    this.proc.stdout.on('data', (chunk) => {
      stdout += chunk;
      let idx;
      while ((idx = stdout.indexOf('\n')) >= 0) {
        const line = stdout.slice(0, idx);
        stdout = stdout.slice(idx + 1);
        this._handleResponse(line);
      }
    });
    this.proc.stderr.on('data', (chunk) => process.stderr.write(`[store] ${chunk}`));
    this.proc.on('exit', (code, signal) => {
      const error = new Error(`store writer exited code=${code} signal=${signal}`);
      this.exitedError = error;
      for (const { reject } of this.pending.values()) reject(error);
      this.pending.clear();
      if (!this.closing && onExit) onExit(error);
    });
  }

  _handleResponse(line) {
    if (!line.trim()) return;
    let response;
    try {
      response = JSON.parse(line);
    } catch (error) {
      console.error('invalid store response', line);
      return;
    }
    const id = response.request_id;
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    if (response.status === 'error') pending.reject(new Error(response.error));
    else pending.resolve(response);
  }

  command(op, payload = {}) {
    if (this.exitedError) return Promise.reject(this.exitedError);
    if (this.closing) return Promise.reject(new Error('store writer is closing'));
    const requestId = this.nextId++;
    const command = { request_id: requestId, op, ...payload };
    return new Promise((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject });
      this.proc.stdin.write(`${JSON.stringify(command)}\n`, 'utf8', (error) => {
        if (!error) return;
        this.pending.delete(requestId);
        reject(error);
      });
    });
  }

  close() {
    this.closing = true;
    if (!this.exitedError) this.proc.stdin.end();
  }
}

function memoryStatsMb() {
  const mem = process.memoryUsage();
  return {
    rss_mb: Math.round(mem.rss / 1024 / 1024),
    heap_used_mb: Math.round(mem.heapUsed / 1024 / 1024),
  };
}

class StatusWriter {
  constructor(statusPath) {
    this.statusPath = statusPath;
    this.current = {};
    this.pending = {};
    this.timer = null;
  }

  write(patch, options = {}) {
    const stateChanged = patch.state && patch.state !== this.current.state;
    this.pending = { ...this.pending, ...patch };
    if (options.immediate || stateChanged) {
      this.flush();
      return;
    }
    if (!this.timer) {
      this.timer = setTimeout(() => this.flush(), 1000);
      if (this.timer.unref) this.timer.unref();
    }
  }

  flush() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (Object.keys(this.pending).length === 0) return;
    ensureDir(this.statusPath);
    const status = {
      service: 'whatsapp-web-source',
      ...this.current,
      ...this.pending,
      updated_at: Math.floor(Date.now() / 1000),
      ...memoryStatsMb(),
    };
    fs.writeFileSync(this.statusPath, `${JSON.stringify(status, null, 2)}\n`, { mode: 0o600 });
    this.current = status;
    this.pending = {};
  }
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

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || args.h) {
    console.log(`Usage: node source-daemon.js [options]\n\nOptions:\n  --db PATH                 SQLite projection DB (default ~/.hermes/whatsapp/web_source.db)\n  --status PATH             JSON status path (default ~/.hermes/whatsapp/web_source_status.json)\n  --auth PATH               LocalAuth data dir (default ~/.hermes/whatsapp/wwebjs_auth)\n  --client-id ID            LocalAuth client id (default memu-web-source)\n  --backfill-chat JID       Backfill one chat after ready\n  --backfill-limit N        Backfill limit (default 100, max 500)\n  --no-contact-snapshot     Do not snapshot WhatsApp contact/name models after ready\n  --contact-snapshot-interval SECONDS\n                            Refresh contacts periodically (default 900, 0 disables)\n  --user-agent UA           Override WhatsApp Web browser user-agent\n  --headful                 Show Chromium instead of running headless\n  --exit-after-backfill     Exit after bounded backfill\n  --no-resource-block       Do not block image/media/font requests after ready\n`);
    return;
  }
  const { Client, LocalAuth, Events } = loadWWebJS();

  const dbPath = path.resolve(expandPath(args.db || '~/.hermes/whatsapp/web_source.db'));
  const statusPath = path.resolve(expandPath(args.status || '~/.hermes/whatsapp/web_source_status.json'));
  const authPath = path.resolve(expandPath(args.auth || '~/.hermes/whatsapp/wwebjs_auth'));
  const clientId = String(args['client-id'] || 'memu-web-source');
  const backfillChat = args['backfill-chat'] ? String(args['backfill-chat']) : null;
  const backfillLimit = Math.min(Math.max(parseInt(args['backfill-limit'] || '100', 10) || 100, 1), 500);
  const contactSnapshotEnabled = args['no-contact-snapshot'] !== true;
  const contactSnapshotInterval = Math.max(parseInt(args['contact-snapshot-interval'] || '900', 10) || 0, 0);
  const exitAfterBackfill = Boolean(args['exit-after-backfill']);
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH || undefined;
  const userAgent = args['user-agent'] ? String(args['user-agent']) : defaultUserAgent();
  const headless = args.headful ? false : true;
  let wwebjsReady = false;
  let contactSnapshotRunning = false;
  let contactSnapshotTimer = null;

  ensureDir(dbPath);
  ensureDir(statusPath);
  const status = new StatusWriter(statusPath);
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
      ],
    },
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
    await client.destroy().catch(() => {});
    store.close();
    status.flush();
    process.exit(1);
  }

  process.once('uncaughtException', shutdownFatal);
  process.once('unhandledRejection', shutdownFatal);

  async function persistMessage(message, source) {
    const row = normalizeMessage(message, source);
    const result = await store.command('upsert_message', { row });
    status.write({
      state: 'ready',
      wwebjs_ready: true,
      db_writeable: true,
      last_event_at: Math.floor(Date.now() / 1000),
      last_msg_key: row.msg_key,
    });
    return result;
  }

  async function snapshotContacts() {
    if (!contactSnapshotEnabled) return;
    if (contactSnapshotRunning) return;
    contactSnapshotRunning = true;
    try {
      const rows = await readContactSnapshot(client.pupPage);
      let persisted = 0;
      for (const row of rows) {
        await store.command('upsert_contact', { row });
        persisted += 1;
      }
      status.write({
        state: 'ready',
        wwebjs_ready: true,
        db_writeable: true,
        last_contact_snapshot_at: Math.floor(Date.now() / 1000),
        last_contact_snapshot_rows: persisted,
      });
      console.log(`contact snapshot: ${persisted} rows`);
    } finally {
      contactSnapshotRunning = false;
    }
  }

  function scheduleContactSnapshots() {
    if (!contactSnapshotEnabled || contactSnapshotInterval <= 0 || contactSnapshotTimer) return;
    contactSnapshotTimer = setInterval(() => {
      if (!wwebjsReady) return;
      snapshotContacts().catch((error) => persistFailed('contact snapshot', error));
    }, contactSnapshotInterval * 1000);
    if (contactSnapshotTimer.unref) contactSnapshotTimer.unref();
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
    status.write({ state: 'auth_failure', wwebjs_ready: false, db_writeable: true, error: String(message) }, { immediate: true });
  });

  client.on('ready', async () => {
    console.log('WhatsApp Web source ready');
    wwebjsReady = true;
    status.write({ state: 'ready', wwebjs_ready: true, db_writeable: true }, { immediate: true });
    if (args['no-resource-block'] !== true) {
      try {
        await configureResourceBlocking(client.pupPage);
      } catch (error) {
        console.warn('resource blocking not enabled:', error.message);
      }
    }

    await snapshotContacts().catch((error) => persistFailed('contact snapshot', error));
    scheduleContactSnapshots();

    if (backfillChat) {
      try {
        const chat = await client.getChatById(backfillChat);
        await store.command('upsert_chat', { row: normalizeChat(chat) });
        const messages = await chat.fetchMessages({ limit: backfillLimit });
        let inserted = 0;
        let updated = 0;
        for (const message of messages) {
          const result = await persistMessage(message, 'backfill:fetchMessages');
          if (result.action === 'insert') inserted += 1;
          else updated += 1;
        }
        console.log(`backfill ${backfillChat}: ${messages.length} rows (${inserted} inserted, ${updated} updated)`);
        status.write({
          state: 'ready',
          wwebjs_ready: true,
          db_writeable: true,
          last_backfill_at: Math.floor(Date.now() / 1000),
          last_backfill_chat: backfillChat,
          last_backfill_rows: messages.length,
        }, { immediate: true });
        if (exitAfterBackfill) {
          await client.destroy();
          store.close();
          status.flush();
          process.exit(0);
        }
      } catch (error) {
        console.error('backfill failed:', error);
        status.write(
          { state: 'degraded', wwebjs_ready: true, db_writeable: !store.exitedError, error: error.message },
          { immediate: true },
        );
      }
    }
  });

  client.on(Events.MESSAGE_CREATE, (message) => {
    persistMessage(message, 'event:message_create').catch((error) => persistFailed('persist message_create', error));
  });

  client.on(Events.MESSAGE_RECEIVED, (message) => {
    persistMessage(message, 'event:message').catch((error) => persistFailed('persist message', error));
  });

  client.on(Events.MESSAGE_EDIT, (message) => {
    persistMessage(message, 'event:message_edit').catch((error) => persistFailed('persist message_edit', error));
  });

  client.on(Events.MESSAGE_CIPHERTEXT, (message) => {
    persistMessage(message, 'event:message_ciphertext').catch((error) => persistFailed('persist ciphertext', error));
  });

  client.on(Events.MESSAGE_CIPHERTEXT_FAILED, (message) => {
    persistMessage(message, 'event:message_ciphertext_failed').catch((error) => persistFailed('persist ciphertext_failed', error));
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
    status.write({ state: 'disconnected', wwebjs_ready: false, db_writeable: !store.exitedError, error: String(reason) }, { immediate: true });
  });

  process.on('SIGINT', async () => {
    status.write({ state: 'stopping', wwebjs_ready: false, db_writeable: !store.exitedError }, { immediate: true });
    if (contactSnapshotTimer) clearInterval(contactSnapshotTimer);
    await client.destroy().catch(() => {});
    store.close();
    status.flush();
    process.exit(0);
  });
  process.on('SIGTERM', async () => {
    status.write({ state: 'stopping', wwebjs_ready: false, db_writeable: !store.exitedError }, { immediate: true });
    if (contactSnapshotTimer) clearInterval(contactSnapshotTimer);
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
