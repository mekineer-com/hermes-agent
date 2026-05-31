#!/usr/bin/env node
/**
 * Hermes Agent WhatsApp Bridge
 *
 * Standalone Node.js process that connects to WhatsApp via Baileys
 * and exposes HTTP endpoints for the Python gateway adapter.
 *
 * Endpoints (matches gateway/platforms/whatsapp.py expectations):
 *   GET  /messages       - Read pending incoming messages (non-destructive)
 *                         Optional query: limit=N (default 100)
 *   POST /ack            - Ack delivered messages { up_to_seq }
 *   POST /send           - Send a message { chatId, message, replyTo? }
 *   POST /edit           - Edit a sent message { chatId, messageId, message }
 *   POST /send-media     - Send media natively { chatId, filePath, mediaType?, caption?, fileName? }
 *   POST /typing         - Send typing indicator { chatId }
 *   GET  /chat/:id       - Get chat info
 *   GET  /health         - Health check
 *
 * Usage:
 *   node bridge.js --port 3000 --session ~/.hermes/whatsapp/session
 */

import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, downloadMediaMessage } from '@whiskeysockets/baileys';
import express from 'express';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import { appendFileSync, mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync, renameSync, unlinkSync } from 'fs';
import { randomBytes } from 'crypto';
import { execSync } from 'child_process';
import { tmpdir } from 'os';
import qrcode from 'qrcode-terminal';
import { matchesAllowedUser, parseAllowedUsers } from './allowlist.js';
import { DurableQueue } from './durable_queue.js';

// Parse CLI args
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const WHATSAPP_DEBUG =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_DEBUG === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_DEBUG.toLowerCase());

function envEnabled(name, defaultValue = true) {
  const raw = process.env?.[name];
  if (raw === undefined) return defaultValue;
  const value = String(raw).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(value)) return true;
  if (['0', 'false', 'no', 'off'].includes(value)) return false;
  return defaultValue;
}

const PORT = parseInt(getArg('port', '3000'), 10);
const SESSION_DIR = getArg('session', path.join(process.env.HOME || '~', '.hermes', 'whatsapp', 'session'));
const BRIDGE_STATE_DIR = path.resolve(SESSION_DIR, '..');
const KNOWN_CHATS_PATH = path.join(BRIDGE_STATE_DIR, 'known_chats.json');
const KNOWN_CONTACTS_PATH = path.join(BRIDGE_STATE_DIR, 'known_contacts.json');
const DISCOVERY_PROBE_LOG_PATH = path.join(BRIDGE_STATE_DIR, 'discovery_probe.log');
const SYNC_EVENT_LOG_PATH = path.join(BRIDGE_STATE_DIR, 'sync_events.jsonl');

function logSyncEvent(eventName, payload) {
  try {
    const entry = JSON.stringify({
      ts: new Date().toISOString(),
      event: eventName,
      ...payload,
    });
    appendFileSync(SYNC_EVENT_LOG_PATH, entry + '\n', 'utf8');
  } catch {}
}
const IMAGE_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'image_cache');
const DOCUMENT_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'document_cache');
const AUDIO_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'audio_cache');
const PAIR_ONLY = args.includes('--pair-only');
const WHATSAPP_MODE = getArg('mode', process.env.WHATSAPP_MODE || 'self-chat'); // "bot" or "self-chat"
const ALLOWED_USERS = parseAllowedUsers(process.env.WHATSAPP_ALLOWED_USERS || '');
const PRESERVE_UNREAD_ON_SEND = envEnabled('WHATSAPP_PRESERVE_UNREAD_ON_SEND', true);
const SEND_UNAVAILABLE_AFTER_ACTIVITY = envEnabled('WHATSAPP_SEND_UNAVAILABLE_AFTER_ACTIVITY', true);
const ENABLE_TYPING_INDICATOR = envEnabled('WHATSAPP_ENABLE_TYPING_INDICATOR', true);
const DEFAULT_REPLY_PREFIX = '⚕ *Hermes Agent*\n────────────\n';
const HAS_CUSTOM_REPLY_PREFIX = process.env.WHATSAPP_REPLY_PREFIX !== undefined;
const REPLY_PREFIX = HAS_CUSTOM_REPLY_PREFIX
  ? process.env.WHATSAPP_REPLY_PREFIX.replace(/\\n/g, '\n')
  : DEFAULT_REPLY_PREFIX;
const MAX_MESSAGE_LENGTH = parseInt(process.env.WHATSAPP_MAX_MESSAGE_LENGTH || '4096', 10);
const CHUNK_DELAY_MS = parseInt(process.env.WHATSAPP_CHUNK_DELAY_MS || '300', 10);
const DM_ALIAS_EVENT_TTL_MS = 5 * 60 * 1000;
// Per-call timeout for sock.sendMessage(). Baileys occasionally hangs forever
// when uploading media to WhatsApp servers (and, less often, on text sends),
// which pins the bridge's HTTP handler until the upstream aiohttp timeout
// fires. Fail fast instead so the gateway can surface a real error and retry.
const SEND_TIMEOUT_MS = parseInt(process.env.WHATSAPP_SEND_TIMEOUT_MS || '60000', 10);

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function sendWithTimeout(chatId, payload, timeoutMs = SEND_TIMEOUT_MS) {
  let timer;
  const timeoutPromise = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`sendMessage timed out after ${timeoutMs / 1000}s`)),
      timeoutMs,
    );
  });
  return Promise.race([sock.sendMessage(chatId, payload), timeoutPromise])
    .finally(() => clearTimeout(timer));
}

function formatOutgoingMessage(message) {
  // Bot mode normally skips prefix (sender identity is already clear), but
  // honor an explicit user-configured WHATSAPP_REPLY_PREFIX from config.yaml.
  if (WHATSAPP_MODE !== 'self-chat' && !HAS_CUSTOM_REPLY_PREFIX) return message;
  return REPLY_PREFIX ? `${REPLY_PREFIX}${message}` : message;
}

function splitLongMessage(message, maxLength = MAX_MESSAGE_LENGTH) {
  const text = String(message || '');
  if (!text) return [];
  if (!Number.isFinite(maxLength) || maxLength < 1 || text.length <= maxLength) {
    return [text];
  }

  const chunks = [];
  let remaining = text;
  while (remaining.length > maxLength) {
    let splitAt = remaining.lastIndexOf('\n', maxLength);
    if (splitAt < Math.floor(maxLength / 2)) {
      splitAt = remaining.lastIndexOf(' ', maxLength);
    }
    if (splitAt < 1) splitAt = maxLength;

    chunks.push(remaining.slice(0, splitAt).trimEnd());
    remaining = remaining.slice(splitAt).trimStart();
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

function trackSentMessageId(sent) {
  if (sent?.key?.id) {
    recentlySentIds.add(sent.key.id);
    if (recentlySentIds.size > MAX_RECENT_IDS) {
      recentlySentIds.delete(recentlySentIds.values().next().value);
    }
  }
}

function normalizeWhatsAppId(value) {
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
    const mappedPhone = String(lidToPhone[local] || '').trim();
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

function extractJidLocal(id) {
  return String(id || '').trim().replace(/:.*@/, '@').split('@', 1)[0];
}

function extractJidDomain(id) {
  const normalized = normalizeWhatsAppId(id);
  const atIndex = normalized.indexOf('@');
  if (atIndex < 0) return '';
  return normalized.slice(atIndex + 1).toLowerCase();
}

function getMessageContent(msg) {
  const content = msg?.message || {};
  if (content.ephemeralMessage?.message) return content.ephemeralMessage.message;
  if (content.viewOnceMessage?.message) return content.viewOnceMessage.message;
  if (content.viewOnceMessageV2?.message) return content.viewOnceMessageV2.message;
  if (content.documentWithCaptionMessage?.message) return content.documentWithCaptionMessage.message;
  if (content.templateMessage?.hydratedTemplate) return content.templateMessage.hydratedTemplate;
  if (content.buttonsMessage) return content.buttonsMessage;
  if (content.listMessage) return content.listMessage;
  return content;
}

function getContextInfo(messageContent) {
  if (!messageContent || typeof messageContent !== 'object') return {};
  for (const value of Object.values(messageContent)) {
    if (value && typeof value === 'object' && value.contextInfo) {
      return value.contextInfo;
    }
  }
  return {};
}

mkdirSync(SESSION_DIR, { recursive: true });
mkdirSync(BRIDGE_STATE_DIR, { recursive: true });

// Build LID → phone reverse map from session files (lid-mapping-{phone}.json)
// and creds.json self-identity (me.id / me.lid).
function buildLidMap() {
  const map = {};
  try {
    for (const f of readdirSync(SESSION_DIR)) {
      const m = f.match(/^lid-mapping-(\d+)\.json$/);
      if (!m) continue;
      const phone = m[1];
      const lid = JSON.parse(readFileSync(path.join(SESSION_DIR, f), 'utf8'));
      if (lid) map[String(lid)] = phone;
    }
  } catch {}
  // Self-identity fallback: creds.json stores our own phone/LID pair even
  // when no lid-mapping-*.json file exists for it.
  try {
    const creds = JSON.parse(readFileSync(path.join(SESSION_DIR, 'creds.json'), 'utf8'));
    const meId = String(creds?.me?.id || '').replace(/:.*@/, '@').split('@')[0];
    const meLid = String(creds?.me?.lid || '').replace(/:.*@/, '@').split('@')[0];
    if (meId && meLid && meId !== meLid) {
      map[meLid] = meId;
    }
  } catch {}
  return map;
}
let lidToPhone = buildLidMap();

const logger = pino({ level: 'warn' });

// Durable queue for inbound events.
const durableQueue = new DurableQueue({
  queueDir: path.resolve(SESSION_DIR, '..'),
  defaultLimit: parseInt(process.env.WHATSAPP_QUEUE_READ_LIMIT || '100', 10),
  compactionEveryAcks: parseInt(process.env.WHATSAPP_QUEUE_COMPACT_EVERY_ACKS || '100', 10),
});

// Track recently sent message IDs to prevent echo-back loops with media
const recentlySentIds = new Set();
const MAX_RECENT_IDS = 50;
const chatUnreadCounts = new Map();
const lastInboundMessageByChat = new Map();
const pushNameCache = new Map();
const groupNameCache = new Map();
const sentMessageStore = new Map();
const MAX_SENT_STORE = 200;
const knownChats = new Map();
const unresolvedDmNameLogged = new Set();
const recentDmMessageById = new Map();

function _atomicWriteJson(filePath, payload) {
  const tmpPath = `${filePath}.tmp`;
  writeFileSync(tmpPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  renameSync(tmpPath, filePath);
}

function appendDiscoveryProbe(payload) {
  try {
    appendFileSync(
      DISCOVERY_PROBE_LOG_PATH,
      `${JSON.stringify({ ts: new Date().toISOString(), ...payload })}\n`,
      'utf8',
    );
  } catch {}
}

function _readJson(filePath) {
  if (!existsSync(filePath)) return null;
  try {
    return JSON.parse(readFileSync(filePath, 'utf8'));
  } catch {
    return null;
  }
}

function persistKnownChats() {
  const chats = [];
  for (const row of knownChats.values()) {
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
    _atomicWriteJson(KNOWN_CHATS_PATH, {
      updated_at: new Date().toISOString(),
      chats,
    });
  } catch (err) {
    logger.warn({ err }, 'failed to persist known chats');
  }
}

function persistKnownContacts() {
  const contacts = [];
  for (const [id, displayName] of pushNameCache.entries()) {
    if (!id || !displayName) continue;
    contacts.push({ id: String(id), display_name: String(displayName) });
  }
  contacts.sort((a, b) => String(a.id).localeCompare(String(b.id)));
  try {
    _atomicWriteJson(KNOWN_CONTACTS_PATH, {
      updated_at: new Date().toISOString(),
      contacts,
    });
  } catch (err) {
    logger.warn({ err }, 'failed to persist known contacts');
  }
}

function loadKnownState() {
  const chatsData = _readJson(KNOWN_CHATS_PATH);
  const chats = Array.isArray(chatsData?.chats) ? chatsData.chats : [];
  for (const row of chats) {
    const chatId = normalizeWhatsAppId(row?.id || '');
    if (!chatId) continue;
    knownChats.set(chatId, {
      chatId,
      isGroup: !!row.is_group,
      name: String(row?.name || '').trim(),
      lastSenderName: String(row?.last_sender_name || '').trim(),
      updatedAtMs: Number(row?.updated_at_ms || 0) || Date.now(),
    });
  }

  const contactsData = _readJson(KNOWN_CONTACTS_PATH);
  const contacts = Array.isArray(contactsData?.contacts) ? contactsData.contacts : [];
  for (const row of contacts) {
    const contactId = normalizeWhatsAppId(row?.id || '');
    const displayName = String(row?.display_name || '').trim();
    if (!contactId || !displayName) continue;
    pushNameCache.set(contactId, displayName);
  }

}

function canonicalizeKnownStateWithLidMap() {
  let chatsChanged = false;
  let contactsChanged = false;

  for (const [lid, phone] of Object.entries(lidToPhone)) {
    const lidJid = `${String(lid || '').trim()}@lid`;
    const phoneJid = `${String(phone || '').trim()}@s.whatsapp.net`;
    if (!lid || !phone) continue;

    const lidChat = knownChats.get(lidJid);
    if (lidChat) {
      const phoneChat = knownChats.get(phoneJid) || {};
      knownChats.set(phoneJid, {
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
      knownChats.delete(lidJid);
      chatsChanged = true;
    }

    const lidName = String(pushNameCache.get(lidJid) || '').trim();
    const phoneName = String(pushNameCache.get(phoneJid) || '').trim();
    if (lidName && !phoneName) {
      pushNameCache.set(phoneJid, lidName);
      contactsChanged = true;
    }
    if (lidName) {
      pushNameCache.delete(lidJid);
      contactsChanged = true;
    }
  }

  if (chatsChanged) {
    persistKnownChats();
  }
  if (contactsChanged) {
    persistKnownContacts();
  }
}

function rememberKnownChat(chatId, { isGroup = false, name = '', lastSenderName = '' } = {}) {
  const normalizedChatId = normalizeWhatsAppId(chatId);
  if (!normalizedChatId) return;
  const existing = knownChats.get(normalizedChatId) || {};
  const merged = {
    chatId: normalizedChatId,
    isGroup: !!(isGroup || existing.isGroup),
    name: String(name || existing.name || '').trim(),
    lastSenderName: String(lastSenderName || existing.lastSenderName || '').trim(),
    updatedAtMs: Date.now(),
  };
  knownChats.set(normalizedChatId, merged);
  persistKnownChats();
}

function storeSentMessage(sent, content) {
  if (!sent?.key?.id || !sent?.key?.remoteJid) return;
  const k = `${sent.key.remoteJid}:${sent.key.id}:${sent.key.fromMe ? '1' : '0'}`;
  sentMessageStore.set(k, { content, ts: Date.now() });
  if (sentMessageStore.size > MAX_SENT_STORE) {
    sentMessageStore.delete(sentMessageStore.keys().next().value);
  }
  const cutoff = Date.now() - 86400000;
  for (const [key, val] of sentMessageStore) {
    if (val.ts < cutoff) sentMessageStore.delete(key);
    else break;
  }
}

let sock = null;
let connectionState = 'disconnected';

function rememberPushName(senderId, pushName) {
  const sid = normalizeWhatsAppId(senderId);
  const name = String(pushName || '').trim();
  if (!sid || !name) return;
  if (String(pushNameCache.get(sid) || '') === name) return;
  pushNameCache.set(sid, name);
  persistKnownContacts();
}

function rememberKnownChatsFromSnapshot(chats) {
  if (!Array.isArray(chats)) return;
  for (const chat of chats) {
    const chatId = normalizeWhatsAppId(chat?.id || chat?.jid || '');
    if (!chatId || chatId.toLowerCase().includes('status@broadcast')) continue;
    const isGroup = chatId.endsWith('@g.us') || chat?.isGroup === true || String(chat?.type || '').toLowerCase() === 'group';
    const name = String(chat?.name || chat?.subject || '').trim();
    rememberKnownChat(chatId, { isGroup, name });
  }
}

function rememberKnownContactsFromSnapshot(contacts) {
  if (!Array.isArray(contacts)) return;
  for (const contact of contacts) {
    if (contact?.lid && contact?.jid) {
      learnLidPhoneShare(contact.lid, contact.jid);
    }
    const contactId = normalizeWhatsAppId(contact?.id || '');
    const displayName = String(
      contact?.notify || contact?.name || contact?.verifiedName || ''
    ).trim();
    if (contactId && displayName) {
      rememberPushName(contactId, displayName);
    }
  }
}

function learnLidPhoneShare(lidValue, jidValue) {
  const lidLocal = String(lidValue || '').trim().replace(/:.*@/, '@').split('@', 1)[0];
  const phoneLocal = String(jidValue || '').trim().replace(/:.*@/, '@').split('@', 1)[0];
  if (!lidLocal || !phoneLocal || lidLocal === phoneLocal) return;
  if (String(lidToPhone[lidLocal] || '') === phoneLocal) return;
  lidToPhone[lidLocal] = phoneLocal;
  canonicalizeKnownStateWithLidMap();
}

function rememberPhoneNumberShares(payload) {
  if (Array.isArray(payload)) {
    for (const row of payload) {
      if (!row || typeof row !== 'object') continue;
      learnLidPhoneShare(row.lid, row.jid);
    }
    return;
  }
  if (payload && typeof payload === 'object') {
    learnLidPhoneShare(payload.lid, payload.jid);
  }
}

function pruneRecentDmMessageCache(nowMs) {
  for (const [key, row] of recentDmMessageById.entries()) {
    if ((nowMs - Number(row?.ts || 0)) > DM_ALIAS_EVENT_TTL_MS) {
      recentDmMessageById.delete(key);
    }
  }
}

function learnAliasFromMirroredDmMessage({ chatId, messageId, fromMe, isGroup }) {
  if (isGroup) return;
  const normalizedChatId = normalizeWhatsAppId(chatId);
  const id = String(messageId || '').trim();
  if (!normalizedChatId || !id) return;
  const domain = extractJidDomain(normalizedChatId);
  if (domain !== 'lid' && domain !== 's.whatsapp.net') return;

  const nowMs = Date.now();
  pruneRecentDmMessageCache(nowMs);
  const key = `${fromMe ? '1' : '0'}:${id}`;
  const previous = recentDmMessageById.get(key);
  recentDmMessageById.set(key, { chatId: normalizedChatId, ts: nowMs });
  if (!previous || previous.chatId === normalizedChatId) return;

  const previousDomain = extractJidDomain(previous.chatId);
  if (previousDomain === domain) return;

  const lidLocal = domain === 'lid'
    ? extractJidLocal(normalizedChatId)
    : extractJidLocal(previous.chatId);
  const phoneLocal = domain === 's.whatsapp.net'
    ? extractJidLocal(normalizedChatId)
    : extractJidLocal(previous.chatId);
  if (!lidLocal || !phoneLocal || lidLocal === phoneLocal) return;
  if (String(lidToPhone[lidLocal] || '') === phoneLocal) return;

  learnLidPhoneShare(`${lidLocal}@lid`, `${phoneLocal}@s.whatsapp.net`);
  if (WHATSAPP_DEBUG) {
    try {
      console.log(JSON.stringify({
        event: 'discovery_alias_learned',
        source: 'mirrored_dm_message_id',
        messageId: id,
        lid: lidLocal,
        phone: phoneLocal,
      }));
    } catch {}
  }
}

function resolveDmDisplayName(chatId, row) {
  const fromCache = String(pushNameCache.get(chatId) || '').trim();
  if (fromCache) return fromCache;
  const fromRow = String(row?.name || row?.lastSenderName || '').trim();
  if (fromRow) return fromRow;
  if (WHATSAPP_DEBUG && !unresolvedDmNameLogged.has(chatId)) {
    unresolvedDmNameLogged.add(chatId);
    try {
      console.log(JSON.stringify({
        event: 'dm_name_unresolved',
        chatId,
        hadRowName: !!String(row?.name || '').trim(),
        hadLastSenderName: !!String(row?.lastSenderName || '').trim(),
      }));
    } catch {}
  }
  return chatId.split('@')[0];
}

function extractPossibleSenderName(msg) {
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
    // Ignore obvious non-name placeholders/noise.
    if (/^\[.*\]$/.test(name)) continue;
    if (/^(image|video|audio|document)\s+received$/i.test(name)) continue;
    return name;
  }
  return '';
}

async function resolveGroupChatName(chatId) {
  const normalizedChatId = normalizeWhatsAppId(chatId);
  if (!normalizedChatId) return '';
  const cached = String(groupNameCache.get(normalizedChatId) || '').trim();
  if (cached) return cached;
  if (!sock || !normalizedChatId.endsWith('@g.us')) return '';
  try {
    const metadata = await sock.groupMetadata(normalizedChatId);
    const subject = String(metadata?.subject || '').trim();
    if (subject) {
      groupNameCache.set(normalizedChatId, subject);
      return subject;
    }
  } catch {}
  return '';
}

function updateUnreadCountSnapshot(chats) {
  if (!Array.isArray(chats)) return;
  for (const chat of chats) {
    const chatId = normalizeWhatsAppId(chat?.id || chat?.jid || '');
    if (!chatId) continue;
    if (chat?.unreadCount === undefined || chat?.unreadCount === null) continue;
    const unreadCount = Number(chat.unreadCount);
    if (Number.isFinite(unreadCount) && unreadCount >= 0) {
      chatUnreadCounts.set(chatId, unreadCount);
    }
  }
}

function rememberInboundLastMessage(msg) {
  const chatId = normalizeWhatsAppId(msg?.key?.remoteJid || '');
  if (!chatId) return;
  if (msg?.key?.fromMe) return;
  const messageId = String(msg?.key?.id || '');
  if (!messageId) return;
  const ts = Number(msg?.messageTimestamp);
  if (!Number.isFinite(ts) || ts <= 0) return;

  const key = {
    remoteJid: chatId,
    id: messageId,
    fromMe: false,
  };
  if (msg.key.participant) {
    key.participant = normalizeWhatsAppId(msg.key.participant);
  }

  lastInboundMessageByChat.set(chatId, {
    key,
    messageTimestamp: ts,
  });
}

function chatHasUnreadMessages(chatId) {
  const unread = Number(chatUnreadCounts.get(normalizeWhatsAppId(chatId)));
  return Number.isFinite(unread) && unread > 0;
}

async function postSendPresenceAndUnreadRestore(chatId, hadUnreadBeforeSend) {
  if (!sock || connectionState !== 'connected') return;

  if (SEND_UNAVAILABLE_AFTER_ACTIVITY) {
    try {
      await sock.sendPresenceUpdate('unavailable');
    } catch (err) {
      if (WHATSAPP_DEBUG) {
        try {
          console.log(JSON.stringify({
            event: 'warn',
            reason: 'set_unavailable_failed',
            chatId,
            error: err?.message || String(err),
          }));
        } catch {}
      }
    }
  }

  if (!PRESERVE_UNREAD_ON_SEND || !hadUnreadBeforeSend) return;
  const normalizedChatId = normalizeWhatsAppId(chatId);
  const lastInbound = lastInboundMessageByChat.get(normalizedChatId);
  if (!lastInbound?.key?.id || !lastInbound?.messageTimestamp) return;

  try {
    await sock.chatModify(
      { markRead: false, lastMessages: [lastInbound] },
      normalizedChatId,
    );
  } catch (err) {
    if (WHATSAPP_DEBUG) {
      try {
        console.log(JSON.stringify({
          event: 'warn',
          reason: 'preserve_unread_failed',
          chatId: normalizedChatId,
          error: err?.message || String(err),
        }));
      } catch {}
    }
  }
}

loadKnownState();
canonicalizeKnownStateWithLidMap();
persistKnownChats();
persistKnownContacts();

async function startSocket() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['Hermes Agent', 'Chrome', '120.0'],
    syncFullHistory: true,
    markOnlineOnConnect: false,
    // Required for Baileys 7.x: without this, incoming messages that need
    // E2EE session re-establishment are silently dropped (msg.message === null)
    getMessage: async (key) => {
      const k = `${key.remoteJid}:${key.id}:${key.fromMe ? '1' : '0'}`;
      const entry = sentMessageStore.get(k);
      if (entry) {
        logger.debug({ event: 'getMessage_hit', key }, 'retry served from cache');
        return entry.content;
      }
      // LID/phone duality: retry remoteJid may differ from send JID. Fall back to id-only scan.
      for (const [ck, cv] of sentMessageStore) {
        if (ck.includes(`:${key.id}:`)) {
          logger.debug({ event: 'getMessage_hit_id_fallback', key }, 'retry served from cache via id-only match');
          return cv.content;
        }
      }
      logger.warn({ event: 'getMessage_miss', remoteJid: key.remoteJid, id: key.id, fromMe: key.fromMe }, 'retry key not in cache');
      return { conversation: '' };
    },
  });

  sock.ev.on('creds.update', () => {
    saveCreds();
    lidToPhone = buildLidMap();
    canonicalizeKnownStateWithLidMap();
  });
  sock.ev.on('chats.phoneNumberShare', (payload) => {
    rememberPhoneNumberShares(payload);
  });
  sock.ev.on('chats.upsert', (chats) => {
    logSyncEvent('chats.upsert', { count: Array.isArray(chats) ? chats.length : 0, chats });
    updateUnreadCountSnapshot(chats);
    rememberKnownChatsFromSnapshot(chats);
  });
  sock.ev.on('chats.update', (chats) => {
    logSyncEvent('chats.update', { count: Array.isArray(chats) ? chats.length : 0, chats });
    updateUnreadCountSnapshot(chats);
    rememberKnownChatsFromSnapshot(chats);
  });
  sock.ev.on('contacts.upsert', (contacts) => {
    logSyncEvent('contacts.upsert', { count: Array.isArray(contacts) ? contacts.length : 0, contacts });
    rememberKnownContactsFromSnapshot(contacts);
  });
  sock.ev.on('contacts.update', (contacts) => {
    logSyncEvent('contacts.update', { count: Array.isArray(contacts) ? contacts.length : 0, contacts });
    rememberKnownContactsFromSnapshot(contacts);
  });
  sock.ev.on('messaging-history.set', ({ chats, contacts, messages, isLatest, progress, syncType }) => {
    logSyncEvent('messaging-history.set', {
      chatCount: Array.isArray(chats) ? chats.length : 0,
      contactCount: Array.isArray(contacts) ? contacts.length : 0,
      messageCount: Array.isArray(messages) ? messages.length : 0,
      isLatest, progress, syncType,
      chats, contacts,
    });
    rememberKnownChatsFromSnapshot(chats);
    rememberKnownContactsFromSnapshot(contacts);
  });

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log('\n📱 Scan this QR code with WhatsApp on your phone:\n');
      qrcode.generate(qr, { small: true });
      console.log('\nWaiting for scan...\n');
      const qrHtml = `<!DOCTYPE html><html><head><meta charset="utf-8"><title>WhatsApp QR</title>
<script src="https://cdn.jsdelivr.net/npm/qrcode@1/build/qrcode.min.js"></script></head>
<body style="display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#111">
<canvas id="qr"></canvas>
<script>QRCode.toCanvas(document.getElementById('qr'),${JSON.stringify(qr)},{width:400,margin:2})</script>
</body></html>`;
      const qrPath = path.join(BRIDGE_STATE_DIR, 'qr.html');
      try { writeFileSync(qrPath, qrHtml, 'utf8'); console.log(`QR saved to ${qrPath}`); } catch {}
    }

    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      connectionState = 'disconnected';

      if (reason === DisconnectReason.loggedOut) {
        console.log('❌ Logged out. Delete session and restart to re-authenticate.');
        process.exit(1);
      } else {
        // 515 = restart requested (common after pairing). Always reconnect.
        if (reason === 515) {
          console.log('↻ WhatsApp requested restart (code 515). Reconnecting...');
        } else {
          console.log(`⚠️  Connection closed (reason: ${reason}). Reconnecting in 3s...`);
        }
        setTimeout(startSocket, reason === 515 ? 1000 : 3000);
      }
    } else if (connection === 'open') {
      connectionState = 'connected';
      console.log('✅ WhatsApp connected!');
      if (PAIR_ONLY) {
        console.log('✅ Pairing complete. Credentials saved.');
        // Give Baileys a moment to flush creds, then exit cleanly
        setTimeout(() => process.exit(0), 2000);
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    // Discovery should observe all upsert types (WhatsApp/Baileys varies by
    // event kind). Forwarding into memU remains restricted to notify/append.
    const forwardableType = (type === 'notify' || type === 'append');

    const botIds = Array.from(new Set([
      normalizeWhatsAppId(sock.user?.id),
      normalizeWhatsAppId(sock.user?.lid),
    ].filter(Boolean)));

    for (const msg of messages) {
      const rawChatId = String(msg.key.remoteJid || '');
      const isStatusUpdate = rawChatId.toLowerCase() === 'status@broadcast';
      if (isStatusUpdate) {
        if (WHATSAPP_DEBUG) {
          try {
            console.log(JSON.stringify({
              event: 'ignored',
              reason: 'status_update',
              chatId: rawChatId,
              messageId: msg.key.id || '',
            }));
          } catch {}
        }
        continue;
      }
      const chatId = normalizeWhatsAppId(rawChatId);
      if (!chatId) continue;
      const senderId = normalizeWhatsAppId(msg.key.participant || rawChatId) || chatId;
      const isGroup = chatId.endsWith('@g.us');
      learnAliasFromMirroredDmMessage({
        chatId,
        messageId: msg.key.id,
        fromMe: !!msg.key.fromMe,
        isGroup,
      });
      const senderDisplayName = extractPossibleSenderName(msg);
      appendDiscoveryProbe({
        event: 'discovery_probe',
        type,
        rawChatId,
        normalizedChatId: chatId,
        rawParticipant: String(msg.key.participant || ''),
        normalizedSenderId: senderId,
        fromMe: !!msg.key.fromMe,
        pushName: String(msg.pushName || ''),
        notifyName: String(msg.notifyName || ''),
        participantName: String(msg.participantName || ''),
        extractedSenderDisplayName: senderDisplayName,
        hasMessagePayload: !!msg.message,
      });
      // Keep discovery populated even when WhatsApp event decryption fails and
      // msg.message is absent (observed as Bad MAC / missing session errors).
      if (!msg.key.fromMe) {
        rememberPushName(senderId, senderDisplayName);
      }
      rememberKnownChat(chatId, {
        isGroup,
        lastSenderName: (!isGroup && !msg.key.fromMe) ? senderDisplayName : '',
      });
      if (!msg.message) continue;
      rememberInboundLastMessage(msg);
      if (WHATSAPP_DEBUG) {
        try {
          console.log(JSON.stringify({
            event: 'upsert', type,
            fromMe: !!msg.key.fromMe, chatId,
            senderId,
            messageKeys: Object.keys(msg.message || {}),
          }));
        } catch {}
      }
      const senderNumber = senderId.replace(/@.*/, '');
      if (!forwardableType) continue;

      // Handle fromMe messages based on mode
      if (msg.key.fromMe) {
        if (chatId.includes('status')) continue;

        if (WHATSAPP_MODE === 'bot') {
          // Bot mode: echo-back filtering happens at line 935 (prefix +
          // recentlySentIds check). Phone-originated fromMe messages pass
          // through so Echo has full conversation context.
        }

        if (WHATSAPP_MODE === 'self-chat') {
          const myNumber = (sock.user?.id || '').replace(/:.*@/, '@').replace(/@.*/, '');
          const myLid = (sock.user?.lid || '').replace(/:.*@/, '@').replace(/@.*/, '');
          const chatNumber = chatId.replace(/@.*/, '');
          const isSelfChat = (myNumber && chatNumber === myNumber) || (myLid && chatNumber === myLid);
          if (!isSelfChat) continue;
        }
      }

      // Handle !fromMe messages (from other people) based on mode.
      // Self-chat mode only responds to the user's own messages to
      // themselves — stranger DMs / group pings must never reach the
      // Python gateway, otherwise a pairing-code reply fires in response
      // to arbitrary incoming messages (#8389).
      if (!msg.key.fromMe) {
        if (WHATSAPP_MODE === 'self-chat') {
          try {
            console.log(JSON.stringify({
              event: 'ignored',
              reason: 'self_chat_mode_rejects_non_self',
              chatId,
              senderId,
            }));
          } catch {}
          continue;
        }
        if (!matchesAllowedUser(senderId, ALLOWED_USERS, SESSION_DIR)) {
          try {
            console.log(JSON.stringify({
              event: 'ignored',
              reason: 'allowlist_mismatch',
              chatId,
              senderId,
            }));
          } catch {}
          continue;
        }
      }

      const messageContent = getMessageContent(msg);
      const contextInfo = getContextInfo(messageContent);
      const mentionedIds = Array.from(new Set((contextInfo?.mentionedJid || []).map(normalizeWhatsAppId).filter(Boolean)));
      const quotedMessageId = contextInfo?.stanzaId || null;
      const quotedParticipant = normalizeWhatsAppId(contextInfo?.participant || '') || null;
      const quotedRemoteJid = normalizeWhatsAppId(contextInfo?.remoteJid || '') || null;
      const hasQuotedMessage = !!contextInfo?.quotedMessage;

      // Extract message body
      let body = '';
      let hasMedia = false;
      let mediaType = '';
      const mediaUrls = [];

      if (messageContent.conversation) {
        body = messageContent.conversation;
      } else if (messageContent.extendedTextMessage?.text) {
        body = messageContent.extendedTextMessage.text;
      } else if (messageContent.imageMessage) {
        body = messageContent.imageMessage.caption || '';
        hasMedia = true;
        mediaType = 'image';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.imageMessage.mimetype || 'image/jpeg';
          const extMap = { 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif' };
          const ext = extMap[mime] || '.jpg';
          mkdirSync(IMAGE_CACHE_DIR, { recursive: true });
          const filePath = path.join(IMAGE_CACHE_DIR, `img_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download image:', err.message);
        }
      } else if (messageContent.videoMessage) {
        body = messageContent.videoMessage.caption || '';
        hasMedia = true;
        mediaType = 'video';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.videoMessage.mimetype || 'video/mp4';
          const ext = mime.includes('mp4') ? '.mp4' : '.mkv';
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const filePath = path.join(DOCUMENT_CACHE_DIR, `vid_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download video:', err.message);
        }
      } else if (messageContent.audioMessage || messageContent.pttMessage) {
        hasMedia = true;
        mediaType = messageContent.pttMessage ? 'ptt' : 'audio';
        try {
          const audioMsg = messageContent.pttMessage || messageContent.audioMessage;
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = audioMsg.mimetype || 'audio/ogg';
          const ext = mime.includes('ogg') ? '.ogg' : mime.includes('mp4') ? '.m4a' : '.ogg';
          mkdirSync(AUDIO_CACHE_DIR, { recursive: true });
          const filePath = path.join(AUDIO_CACHE_DIR, `aud_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download audio:', err.message);
        }
      } else if (messageContent.documentMessage) {
        body = messageContent.documentMessage.caption || '';
        hasMedia = true;
        mediaType = 'document';
        const fileName = messageContent.documentMessage.fileName || 'document';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const safeFileName = path.basename(fileName).replace(/[^a-zA-Z0-9._-]/g, '_');
          const filePath = path.join(DOCUMENT_CACHE_DIR, `doc_${randomBytes(6).toString('hex')}_${safeFileName}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download document:', err.message);
        }
      }

      // For media without caption, use a placeholder so the API message is never empty
      if (hasMedia && !body) {
        body = `[${mediaType} received]`;
      }

      // Ignore Hermes' own reply messages in self-chat mode to avoid loops.
      if (msg.key.fromMe && ((REPLY_PREFIX && body.startsWith(REPLY_PREFIX)) || recentlySentIds.has(msg.key.id))) {
        if (WHATSAPP_DEBUG) {
          try { console.log(JSON.stringify({ event: 'ignored', reason: 'agent_echo', chatId, messageId: msg.key.id })); } catch {}
        }
        continue;
      }

      // Skip empty messages
      if (!body && !hasMedia) {
        if (WHATSAPP_DEBUG) {
          try { 
            console.log(JSON.stringify({ event: 'ignored', reason: 'empty', chatId, messageKeys: Object.keys(msg.message || {}) })); 
          } catch (err) {
            console.error('Failed to log empty message event:', err);
          }
        }
        continue;
      }

      const resolvedSenderName = String(
        msg.pushName || pushNameCache.get(senderId) || senderNumber
      ).trim() || senderNumber;
      const resolvedChatName = isGroup
        ? (await resolveGroupChatName(chatId)) || chatId.split('@')[0]
        : msg.key.fromMe ? '' : resolvedSenderName;
      rememberKnownChat(chatId, {
        isGroup,
        name: resolvedChatName,
        lastSenderName: msg.key.fromMe ? '' : resolvedSenderName,
      });

      const event = {
        messageId: msg.key.id,
        chatId,
        senderId,
        senderName: resolvedSenderName,
        chatName: resolvedChatName,
        isGroup,
        body,
        hasMedia,
        mediaType,
        mediaUrls,
        mentionedIds,
        quotedMessageId,
        quotedParticipant,
        quotedRemoteJid,
        hasQuotedMessage,
        botIds,
        timestamp: msg.messageTimestamp,
      };

      const queued = durableQueue.enqueue(event);
      if (WHATSAPP_DEBUG && !queued) {
        try {
          console.log(JSON.stringify({
            event: 'ignored',
            reason: 'duplicate_event_uid',
            chatId: event.chatId,
            messageId: event.messageId,
            senderId: event.senderId,
          }));
        } catch {}
      }
    }
  });
}

// HTTP server
const app = express();
app.use(express.json());

// Host-header validation — defends against DNS rebinding.
// The bridge binds loopback-only (127.0.0.1) but a victim browser on
// the same machine could be tricked into fetching from an attacker
// hostname that TTL-flips to 127.0.0.1. Reject any request whose Host
// header doesn't resolve to a loopback alias.
// See GHSA-ppp5-vxwm-4cf7.
const _ACCEPTED_HOST_VALUES = new Set([
  'localhost',
  '127.0.0.1',
  '[::1]',
  '::1',
]);

app.use((req, res, next) => {
  const raw = (req.headers.host || '').trim();
  if (!raw) {
    return res.status(400).json({ error: 'Missing Host header' });
  }
  // Strip port suffix: "localhost:3000" → "localhost"
  const hostOnly = (raw.includes(':')
    ? raw.substring(0, raw.lastIndexOf(':'))
    : raw
  ).replace(/^\[|\]$/g, '').toLowerCase();
  if (!_ACCEPTED_HOST_VALUES.has(hostOnly)) {
    return res.status(400).json({
      error: 'Invalid Host header. Bridge accepts loopback hosts only.',
    });
  }
  next();
});

// Read pending messages (non-destructive)
app.get('/messages', (req, res) => {
  const limitRaw = req.query?.limit;
  const limit = Number.parseInt(String(limitRaw ?? ''), 10);
  const msgs = durableQueue.readUnacked(Number.isFinite(limit) && limit > 0 ? limit : undefined);
  res.json(msgs);
});

// Ack processed messages through an inclusive sequence boundary.
app.post('/ack', (req, res) => {
  const upToSeq = req.body?.up_to_seq;
  if (upToSeq === undefined || upToSeq === null) {
    return res.status(400).json({ error: 'up_to_seq is required' });
  }
  const parsed = Number.parseInt(String(upToSeq), 10);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return res.status(400).json({ error: 'up_to_seq must be a non-negative integer' });
  }
  const ack = durableQueue.ackThrough(parsed);
  return res.json({
    success: true,
    ackedUpToSeq: ack.ackedUpToSeq,
    removed: ack.removed,
  });
});

// Send a message
app.post('/send', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, message, replyTo } = req.body;
  if (!chatId || !message) {
    return res.status(400).json({ error: 'chatId and message are required' });
  }

  try {
    const hadUnreadBeforeSend = chatHasUnreadMessages(chatId);
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];
    for (let i = 0; i < chunks.length; i += 1) {
      const sent = await sendWithTimeout(chatId, { text: chunks[i] });
      trackSentMessageId(sent);
      storeSentMessage(sent, { conversation: chunks[i] });
      if (sent?.key?.id) messageIds.push(sent.key.id);
      if (chunks.length > 1 && i < chunks.length - 1) {
        await sleep(CHUNK_DELAY_MS);
      }
    }

    await postSendPresenceAndUnreadRestore(chatId, hadUnreadBeforeSend);

    res.json({
      success: true,
      messageId: messageIds[messageIds.length - 1],
      messageIds,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Edit a previously sent message
app.post('/edit', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, messageId, message } = req.body;
  if (!chatId || !messageId || !message) {
    return res.status(400).json({ error: 'chatId, messageId, and message are required' });
  }

  try {
    const hadUnreadBeforeSend = chatHasUnreadMessages(chatId);
    const key = { id: messageId, fromMe: true, remoteJid: chatId };
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];

    await sendWithTimeout(chatId, { text: chunks[0], edit: key });
    storeSentMessage({ key }, { conversation: chunks[0] });
    if (chunks.length > 1) {
      for (let i = 1; i < chunks.length; i += 1) {
        const sent = await sendWithTimeout(chatId, { text: chunks[i] });
        trackSentMessageId(sent);
        storeSentMessage(sent, { conversation: chunks[i] });
        if (sent?.key?.id) messageIds.push(sent.key.id);
        if (i < chunks.length - 1) {
          await sleep(CHUNK_DELAY_MS);
        }
      }
    }

    await postSendPresenceAndUnreadRestore(chatId, hadUnreadBeforeSend);
    res.json({ success: true, messageIds });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// MIME type map and media type inference for /send-media
const MIME_MAP = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
  webp: 'image/webp', gif: 'image/gif',
  mp4: 'video/mp4', mov: 'video/quicktime', avi: 'video/x-msvideo',
  mkv: 'video/x-matroska', '3gp': 'video/3gpp',
  pdf: 'application/pdf',
  doc: 'application/msword',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};

function inferMediaType(ext) {
  if (['jpg', 'jpeg', 'png', 'webp', 'gif'].includes(ext)) return 'image';
  if (['mp4', 'mov', 'avi', 'mkv', '3gp'].includes(ext)) return 'video';
  if (['ogg', 'opus', 'mp3', 'wav', 'm4a'].includes(ext)) return 'audio';
  return 'document';
}

// Send media (image, video, document) natively
app.post('/send-media', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, filePath, mediaType, caption, fileName } = req.body;
  if (!chatId || !filePath) {
    return res.status(400).json({ error: 'chatId and filePath are required' });
  }

  try {
    const hadUnreadBeforeSend = chatHasUnreadMessages(chatId);
    if (!existsSync(filePath)) {
      return res.status(404).json({ error: `File not found: ${filePath}` });
    }

    const buffer = readFileSync(filePath);
    const ext = filePath.toLowerCase().split('.').pop();
    const type = mediaType || inferMediaType(ext);
    let msgPayload;

    switch (type) {
      case 'image':
        msgPayload = { image: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'image/jpeg' };
        break;
      case 'video':
        msgPayload = { video: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'video/mp4' };
        break;
      case 'audio': {
        // WhatsApp only renders a native voice bubble (ptt) when the file is ogg/opus.
        // If the caller passes mp3, wav, m4a etc. (e.g. from Edge TTS / NeuTTS),
        // silently convert to ogg/opus via ffmpeg so ptt is always honoured.
        let audioBuffer = buffer;
        let audioExt = ext;
        const needsConversion = !['ogg', 'opus'].includes(ext);
        let tmpPath = null;
        if (needsConversion) {
          tmpPath = path.join(tmpdir(), `hermes_voice_${randomBytes(6).toString('hex')}.ogg`);
          try {
            execSync(
              `ffmpeg -y -i ${JSON.stringify(filePath)} -ar 48000 -ac 1 -c:a libopus ${JSON.stringify(tmpPath)}`,
              { timeout: 30000, stdio: 'pipe' }
            );
            audioBuffer = readFileSync(tmpPath);
            audioExt = 'ogg';
          } catch (convErr) {
            // ffmpeg not available or conversion failed — fall back to original format
            console.warn('[bridge] ffmpeg conversion failed, sending as file attachment:', convErr.message);
          } finally {
            try { if (tmpPath && existsSync(tmpPath)) unlinkSync(tmpPath); } catch (_) {}
          }
        }
        const audioMime = (audioExt === 'ogg' || audioExt === 'opus') ? 'audio/ogg; codecs=opus' : 'audio/mpeg';
        msgPayload = { audio: audioBuffer, mimetype: audioMime, ptt: audioExt === 'ogg' || audioExt === 'opus' };
        break;
      }
      case 'document':
      default:
        msgPayload = {
          document: buffer,
          fileName: fileName || path.basename(filePath),
          caption: caption || undefined,
          mimetype: MIME_MAP[ext] || 'application/octet-stream',
        };
        break;
    }

    const sent = await sendWithTimeout(chatId, msgPayload);

    trackSentMessageId(sent);

    await postSendPresenceAndUnreadRestore(chatId, hadUnreadBeforeSend);

    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Typing indicator
app.post('/typing', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected' });
  }

  const { chatId } = req.body;
  if (!chatId) return res.status(400).json({ error: 'chatId required' });
  if (!ENABLE_TYPING_INDICATOR) {
    return res.json({ success: true, skipped: true });
  }

  try {
    await sock.sendPresenceUpdate('composing', chatId);
    res.json({ success: true });
  } catch (err) {
    res.json({ success: false });
  }
});

// Chat info
app.get('/chat/:id', async (req, res) => {
  const chatId = normalizeWhatsAppId(req.params.id);
  const isGroup = chatId.endsWith('@g.us');

  if (isGroup && sock) {
    try {
      const metadata = await sock.groupMetadata(chatId);
      return res.json({
        name: metadata.subject,
        isGroup: true,
        participants: metadata.participants.map(p => p.id),
      });
    } catch {
      // Fall through to default
    }
  }

  res.json({
    name: resolveDmDisplayName(chatId, null),
    isGroup,
    participants: [],
  });
});

// Best-effort discovery list for local policy UIs.
// Includes chats seen in message events even when those messages are filtered
// out before enqueueing to the Python gateway.
app.get('/chats-known', (req, res) => {
  const out = [];
  for (const [chatId, row] of knownChats.entries()) {
    const isGroup = !!row.isGroup || chatId.endsWith('@g.us');
    const displayName = isGroup
      ? String(row.name || '').trim() || chatId.split('@')[0]
      : resolveDmDisplayName(chatId, row);
    out.push({
      id: chatId,
      name: displayName,
      type: isGroup ? 'group' : 'dm',
    });
  }
  out.sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)));
  res.json({ chats: out });
});

// Health check
app.get('/health', (req, res) => {
  const stats = durableQueue.getStats();
  res.json({
    status: connectionState,
    mode: WHATSAPP_MODE,
    replyPrefix: REPLY_PREFIX,
    queueLength: stats.queueLength,
    ackedUpToSeq: stats.ackedUpToSeq,
    maxSeq: stats.maxSeq,
    uptime: process.uptime(),
  });
});

// Start
if (PAIR_ONLY) {
  // Pair-only mode: just connect, show QR, save creds, exit. No HTTP server.
  console.log('📱 WhatsApp pairing mode');
  console.log(`📁 Session: ${SESSION_DIR}`);
  console.log();
  startSocket();
} else {
  app.listen(PORT, '127.0.0.1', () => {
    console.log(`🌉 WhatsApp bridge listening on port ${PORT} (mode: ${WHATSAPP_MODE})`);
    console.log(`📁 Session stored in: ${SESSION_DIR}`);
    if (ALLOWED_USERS.size > 0) {
      console.log(`🔒 Allowed users: ${Array.from(ALLOWED_USERS).join(', ')}`);
    } else if (WHATSAPP_MODE === 'self-chat') {
      console.log(`🔒 Self-chat mode — only your own messages to yourself are processed.`);
    } else {
      console.log(`🔒 No WHATSAPP_ALLOWED_USERS set — incoming messages are rejected.`);
      console.log(`   Set WHATSAPP_ALLOWED_USERS=<phone> to authorize specific users,`);
      console.log(`   or WHATSAPP_ALLOWED_USERS=* for an explicit open bot.`);
    }
    console.log();
    startSocket();
  });
}
