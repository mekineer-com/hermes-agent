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

import { makeWASocket, useMultiFileAuthState, DisconnectReason, downloadMediaMessage } from '@whiskeysockets/baileys';
import express from 'express';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import { mkdirSync, readFileSync, writeFileSync, existsSync, unlinkSync } from 'fs';
import { randomBytes } from 'crypto';
import { execSync } from 'child_process';
import { tmpdir } from 'os';
import qrcode from 'qrcode-terminal';
import { matchesAllowedUser, parseAllowedUsers } from './allowlist.js';
import { atomicWriteJson, readJson } from './bridge_fs.js';
import { DurableQueue } from './durable_queue.js';
import { LidIdentity } from './lid_identity.js';
import { buildMediaRetryCachePayload } from './media_retry_cache.js';
import { PresenceUnread } from './presence_unread.js';
import { SentMessageStore } from './sent_message_store.js';
import { SocketLifecycle } from './socket_lifecycle.js';
import {
  canonicalizeMessageIds,
  classifyUpsertEvent,
  historyMessageSources,
  isStartupReplay,
  upsertEventMode,
} from './history_ingest.js';

// Parse CLI args
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const WHATSAPP_DEBUG =
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
const RECENTLY_SENT_IDS_PATH = path.join(BRIDGE_STATE_DIR, 'recently_sent_ids.json');
const SENT_MESSAGE_STORE_PATH = path.join(BRIDGE_STATE_DIR, 'sent_message_store.json');

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
const BAILEYS_VERSION_FETCH_TIMEOUT_MS = parseInt(process.env.WHATSAPP_BAILEYS_VERSION_FETCH_TIMEOUT_MS || '5000', 10);
const BAILEYS_VERSION_FALLBACK = [2, 3000, 1023223821];
const SYNC_HISTORY_WINDOW_DAYS = parseFloat(process.env.WHATSAPP_SYNC_HISTORY_WINDOW_DAYS || '14');
const BRIDGE_STARTED_AT_SECONDS = Math.floor(Date.now() / 1000);
const STARTUP_REPLAY_GRACE_SECONDS = Math.max(
  0,
  Math.min(600, parseInt(process.env.WHATSAPP_STARTUP_REPLAY_GRACE_SECONDS || '120', 10) || 120),
);
const DM_ALIAS_EVENT_TTL_MS = 5 * 60 * 1000;
const RECENTLY_SENT_RETENTION_DAYS = parseFloat(process.env.WHATSAPP_RECENTLY_SENT_RETENTION_DAYS || '30');
const RECENTLY_SENT_RETENTION_MS = Math.max(
  24 * 60 * 60 * 1000,
  (Number.isFinite(RECENTLY_SENT_RETENTION_DAYS) ? RECENTLY_SENT_RETENTION_DAYS : 30) * 24 * 60 * 60 * 1000,
);
const MAX_RECENT_IDS = Math.max(
  1,
  parseInt(process.env.WHATSAPP_MAX_RECENT_IDS || '500', 10) || 500,
);
// Per-call timeout for sock.sendMessage(). Baileys occasionally hangs forever
// when uploading media to WhatsApp servers (and, less often, on text sends),
// which pins the bridge's HTTP handler until the upstream aiohttp timeout
// fires. Fail fast instead so the gateway can surface a real error and retry.
const SEND_TIMEOUT_MS = parseInt(process.env.WHATSAPP_SEND_TIMEOUT_MS || '60000', 10);
const WHATSAPP_REVOKE_STUB_TYPE = 1;

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

function normalizeWhatsAppId(value) {
  return identity.normalizeId(value);
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

function timestampSeconds(value) {
  if (value === undefined || value === null || value === '') return 0;
  if (typeof value === 'object') {
    if (Number.isFinite(Number(value.low))) return Number(value.low);
    return 0;
  }
  const ts = Number(value);
  if (!Number.isFinite(ts) || ts <= 0) return 0;
  return ts > 10000000000 ? ts / 1000 : ts;
}

function syncTimestampAllowed(value) {
  if (!Number.isFinite(SYNC_HISTORY_WINDOW_DAYS) || SYNC_HISTORY_WINDOW_DAYS <= 0) return true;
  const ts = timestampSeconds(value);
  if (!ts) return false;
  const cutoff = Date.now() / 1000 - (SYNC_HISTORY_WINDOW_DAYS * 24 * 60 * 60);
  return ts >= cutoff;
}

function extractTextAndMedia(messageContent) {
  let body = '';
  let hasMedia = false;
  let mediaType = '';
  if (messageContent.conversation) {
    body = messageContent.conversation;
  } else if (messageContent.extendedTextMessage?.text) {
    body = messageContent.extendedTextMessage.text;
  } else if (messageContent.imageMessage) {
    body = messageContent.imageMessage.caption || '';
    hasMedia = true;
    mediaType = 'image';
  } else if (messageContent.videoMessage) {
    body = messageContent.videoMessage.caption || '';
    hasMedia = true;
    mediaType = 'video';
  } else if (messageContent.audioMessage) {
    hasMedia = true;
    mediaType = 'audio';
  } else if (messageContent.documentMessage) {
    body = messageContent.documentMessage.caption || '';
    hasMedia = true;
    mediaType = 'document';
  }
  if (hasMedia && !body) {
    body = `[${mediaType} received]`;
  }
  return { body, hasMedia, mediaType };
}

function parseDecoratedAssistantBody(body) {
  const text = String(body || '');
  if (!text) return null;
  if (REPLY_PREFIX && text.startsWith(REPLY_PREFIX)) {
    return {
      body: text.slice(REPLY_PREFIX.length).trimStart(),
      speakerName: '',
    };
  }
  const match = text.match(/^\s*✦\s*\*?([^*:\n]{1,80})\*?:\s*(.*)$/s);
  if (!match) return null;
  return {
    body: String(match[2] || '').trimStart(),
    speakerName: String(match[1] || '').trim(),
  };
}

mkdirSync(SESSION_DIR, { recursive: true });
mkdirSync(BRIDGE_STATE_DIR, { recursive: true });

const logger = pino({ level: 'warn' });

// Durable queue for inbound events.
const durableQueue = new DurableQueue({
  queueDir: path.resolve(SESSION_DIR, '..'),
  defaultLimit: parseInt(process.env.WHATSAPP_QUEUE_READ_LIMIT || '100', 10),
  compactionEveryAcks: parseInt(process.env.WHATSAPP_QUEUE_COMPACT_EVERY_ACKS || '100', 10),
});

const pushNameCache = new Map();
const groupNameCache = new Map();
const knownChats = new Map();
const unresolvedDmNameLogged = new Set();

const identity = new LidIdentity({
  sessionDir: SESSION_DIR,
  aliasTtlMs: DM_ALIAS_EVENT_TTL_MS,
  debug: WHATSAPP_DEBUG,
  logger,
});
const presence = new PresenceUnread({
  normalizeId: normalizeWhatsAppId,
  getSock: () => sock,
  isConnected: () => socketLifecycle.isConnected(),
  preserveUnreadOnSend: PRESERVE_UNREAD_ON_SEND,
  sendUnavailableAfterActivity: SEND_UNAVAILABLE_AFTER_ACTIVITY,
  debug: WHATSAPP_DEBUG,
});
const sentStore = new SentMessageStore({
  recentlySentIdsPath: RECENTLY_SENT_IDS_PATH,
  sentMessageStorePath: SENT_MESSAGE_STORE_PATH,
  recentlySentRetentionMs: RECENTLY_SENT_RETENTION_MS,
  maxRecentIds: MAX_RECENT_IDS,
  logger,
});

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
    atomicWriteJson(KNOWN_CHATS_PATH, {
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
    atomicWriteJson(KNOWN_CONTACTS_PATH, {
      updated_at: new Date().toISOString(),
      contacts,
    });
  } catch (err) {
    logger.warn({ err }, 'failed to persist known contacts');
  }
}

function loadKnownState() {
  const chatsData = readJson(KNOWN_CHATS_PATH);
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

  const contactsData = readJson(KNOWN_CONTACTS_PATH);
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

  identity.forEachPair((lid, phone) => {
    const lidJid = `${String(lid || '').trim()}@lid`;
    const phoneJid = `${String(phone || '').trim()}@s.whatsapp.net`;
    if (!lid || !phone) return;

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
  });

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

let sock = null;
const socketLifecycle = new SocketLifecycle({
  baileysVersionFetchTimeoutMs: BAILEYS_VERSION_FETCH_TIMEOUT_MS,
  baileysVersionFallback: BAILEYS_VERSION_FALLBACK,
  onStart: startSocket,
});

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
  const persistBatch = {};
  for (const contact of contacts) {
    if (contact?.lid && contact?.jid) {
      identity.learnPair(contact.lid, contact.jid, { persistBatch });
    }
    const contactId = normalizeWhatsAppId(contact?.id || '');
    const displayName = String(
      contact?.notify || contact?.name || contact?.verifiedName || ''
    ).trim();
    if (contactId && displayName) {
      rememberPushName(contactId, displayName);
    }
  }
  identity.persistBatch(persistBatch);
}

function resolveDmDisplayName(chatId, row) {
  const fromCache = String(pushNameCache.get(chatId) || '').trim();
  if (fromCache) return fromCache;
  const fromRow = String(row?.name || row?.lastSenderName || '').trim();
  if (fromRow) return fromRow;
  if (WHATSAPP_DEBUG && !unresolvedDmNameLogged.has(chatId)) {
    unresolvedDmNameLogged.add(chatId);
    console.log(JSON.stringify({
      event: 'dm_name_unresolved',
      chatId,
      hadRowName: !!String(row?.name || '').trim(),
      hadLastSenderName: !!String(row?.lastSenderName || '').trim(),
    }));
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

async function enqueueHistoryMessage(rawMsg, { chatFallback = '', surface = 'sync' } = {}) {
  const msg = rawMsg?.message && rawMsg?.key === undefined ? rawMsg.message : rawMsg;
  if (!msg?.key) return false;
  const messageId = String(msg.key.id || '').trim();
  let chatId = normalizeWhatsAppId(msg.key.remoteJid || chatFallback || '');
  if (!chatId || !messageId || chatId.toLowerCase() === 'status@broadcast') return false;

  const timestamp = msg.messageTimestamp || msg.messageC2STimestamp || rawMsg?.messageTimestamp;
  if (!syncTimestampAllowed(timestamp)) return false;
  if (
    surface === 'chats.update'
    && !isStartupReplay({
      timestamp,
      bridgeStartedAtSeconds: BRIDGE_STARTED_AT_SECONDS,
      graceSeconds: STARTUP_REPLAY_GRACE_SECONDS,
    })
  ) {
    return false;
  }

  const isGroup = chatId.endsWith('@g.us');
  const targetRevokeId = Array.isArray(msg.messageStubParameters)
    ? String(msg.messageStubParameters[0] || '').trim()
    : '';
  if (Number(msg.messageStubType) === WHATSAPP_REVOKE_STUB_TYPE && targetRevokeId) {
    return durableQueue.enqueue({
      eventType: 'revoke',
      deliveryMode: 'revoke',
      messageId: targetRevokeId,
      chatId,
      isGroup,
      timestamp,
    });
  }

  const messageContent = getMessageContent(msg);
  if (!messageContent || Object.keys(messageContent).length === 0) return false;

  let participantId = normalizeWhatsAppId(msg.key.participant || '');
  const selfSenderId = normalizeWhatsAppId(sock?.user?.id || sock?.user?.lid || '');
  if (
    msg.key.fromMe
    && participantId
    && selfSenderId
    && chatId === selfSenderId
    && participantId !== selfSenderId
  ) {
    chatId = participantId;
  }
  const mirrorInfo = identity.learnAliasFromDm({
    chatId,
    messageId,
    fromMe: !!msg.key.fromMe,
    isGroup: chatId.endsWith('@g.us'),
  });
  if (mirrorInfo?.duplicate) {
    if (WHATSAPP_DEBUG) {
      console.log(JSON.stringify({
        event: 'ignored',
        reason: 'mirrored_dm_history_duplicate',
        chatId,
        previousChatId: mirrorInfo.previousChatId,
        messageId,
      }));
    }
    return false;
  }

  const ids = canonicalizeMessageIds({
    chatId,
    participantId,
    selfSenderId,
    fromMe: !!msg.key.fromMe,
  }, normalizeWhatsAppId);
  chatId = ids.chatId;
  participantId = ids.participantId;
  const senderId = ids.senderId;
  const { body: extractedBody, hasMedia, mediaType } = extractTextAndMedia(messageContent);
  let body = extractedBody;
  let speakerRoleHint = 'user';
  let speakerNameHint = '';
  if (msg.key.fromMe) {
    if (sentStore.isEcho(messageId)) return false;
    const parsed = parseDecoratedAssistantBody(body);
    if (parsed) {
      speakerRoleHint = 'assistant';
      speakerNameHint = parsed?.speakerName || '';
      body = parsed ? parsed.body : body;
    }
  }
  if (!body && !hasMedia) return false;

  const senderNumber = senderId.replace(/@.*/, '');
  const senderDisplayName = extractPossibleSenderName(msg);
  if (!msg.key.fromMe) {
    rememberPushName(senderId, senderDisplayName);
  }
  const resolvedSenderName = msg.key.fromMe
    ? (String(pushNameCache.get(senderId) || sock?.user?.name || '').trim() || senderNumber)
    : (String(msg.pushName || pushNameCache.get(senderId) || senderDisplayName || senderNumber).trim() || senderNumber);
  const resolvedChatName = chatId.endsWith('@g.us')
    ? (await resolveGroupChatName(chatId)) || chatId.split('@')[0]
    : resolveDmDisplayName(chatId, knownChats.get(chatId));
  rememberKnownChat(chatId, {
    isGroup: chatId.endsWith('@g.us'),
    name: resolvedChatName,
    lastSenderName: (!chatId.endsWith('@g.us') && !msg.key.fromMe) ? resolvedSenderName : '',
  });

  const event = {
    eventType: 'history_message',
    deliveryMode: 'persist_only',
    messageId,
    chatId,
    senderId,
    senderName: resolvedSenderName,
    chatName: resolvedChatName,
    isGroup: chatId.endsWith('@g.us'),
    body,
    hasMedia,
    mediaType,
    mediaUrls: [],
    mentionedIds: [],
    quotedMessageId: '',
    quotedParticipant: '',
    quotedRemoteJid: '',
    hasQuotedMessage: false,
    botIds: [],
    timestamp,
    speakerRoleHint,
    speakerNameHint,
  };
  return durableQueue.enqueue(event);
}

async function enqueueHistoryMessagesFromChats(chats, surface) {
  await enqueueHistoryMessages({ chats }, surface);
}

async function enqueueHistoryMessages(payload, surface) {
  for (const row of historyMessageSources(payload, normalizeWhatsAppId)) {
    await enqueueHistoryMessage(row.message, { chatFallback: row.chatFallback, surface });
  }
}

identity.setOnPairLearned(canonicalizeKnownStateWithLidMap);
loadKnownState();
sentStore.load();
canonicalizeKnownStateWithLidMap();
persistKnownChats();
persistKnownContacts();

async function startSocket() {
  const socketId = socketLifecycle.beginStart();

  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  identity.setKeyStore(state.keys);
  const version = await socketLifecycle.fetchVersion();

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['Hermes Agent', 'Chrome', '120.0'],
    fireInitQueries: false,
    syncFullHistory: false,
    markOnlineOnConnect: false,
    // Required for Baileys 7.x: without this, incoming messages that need
    // E2EE session re-establishment are silently dropped (msg.message === null)
    getMessage: async (key) => {
      const entry = sentStore.getForBaileysKey(key);
      if (entry) {
        logger.debug({ event: 'getMessage_hit', key }, 'retry served from cache');
        return entry;
      }
      // LID/phone duality: retry remoteJid may differ from send JID. Fall back to id-only scan.
      const fallbackEntry = sentStore.getByMessageId(key.id);
      if (fallbackEntry) {
        logger.debug({ event: 'getMessage_hit_id_fallback', key }, 'retry served from cache via id-only match');
        return fallbackEntry;
      }
      logger.error({ event: 'getMessage_miss', remoteJid: key.remoteJid, id: key.id, fromMe: key.fromMe }, 'retry key not in cache — Baileys will skip re-delivery');
      return undefined;
    },
  });

  sock.ev.on('creds.update', () => {
    saveCreds();
    identity.rebuildFromDisk();
  });
  sock.ev.on('chats.phoneNumberShare', (payload) => {
    identity.rememberPhoneShares(payload);
  });
  sock.ev.on('chats.upsert', (chats) => {
    presence.updateUnreadCountSnapshot(chats);
    rememberKnownChatsFromSnapshot(chats);
  });
  sock.ev.on('chats.update', async (chats) => {
    presence.updateUnreadCountSnapshot(chats);
    rememberKnownChatsFromSnapshot(chats);
    await enqueueHistoryMessagesFromChats(chats, 'chats.update');
  });
  sock.ev.on('contacts.upsert', (contacts) => {
    rememberKnownContactsFromSnapshot(contacts);
  });
  sock.ev.on('contacts.update', (contacts) => {
    rememberKnownContactsFromSnapshot(contacts);
  });
  sock.ev.on('messaging-history.set', async ({ chats, contacts, messages }) => {
    rememberKnownChatsFromSnapshot(chats);
    rememberKnownContactsFromSnapshot(contacts);
    await enqueueHistoryMessages({ chats, messages }, 'messaging-history.set');
  });

  sock.ev.on('connection.update', (update) => {
    if (!socketLifecycle.isCurrent(socketId)) return;
    const { connection, lastDisconnect, qr, receivedPendingNotifications } = update;

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
      socketLifecycle.markDisconnected(socketId);

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
        socketLifecycle.scheduleStart(reason === 515 ? 1000 : 3000);
      }
    } else if (connection === 'open') {
      socketLifecycle.markOpen(socketId);
      if (PAIR_ONLY) {
        console.log('✅ Pairing complete. Credentials saved.');
        // Give Baileys a moment to flush creds, then exit cleanly
        setTimeout(() => process.exit(0), 2000);
      }
    }
    if (receivedPendingNotifications && connection !== 'close') {
      socketLifecycle.markReady(socketId);
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    // Discovery should observe all upsert types. Only notify is live; append is
    // Baileys history/backfill and must not trigger the agent on restart.
    const mode = upsertEventMode(type);

    const botIds = Array.from(new Set([
      normalizeWhatsAppId(sock.user?.id),
      normalizeWhatsAppId(sock.user?.lid),
    ].filter(Boolean)));

    for (const msg of messages) {
      const rawChatId = String(msg.key.remoteJid || '');
      const isStatusUpdate = rawChatId.toLowerCase() === 'status@broadcast';
      if (isStatusUpdate) {
        if (WHATSAPP_DEBUG) {
          console.log(JSON.stringify({
            event: 'ignored',
            reason: 'status_update',
            chatId: rawChatId,
            messageId: msg.key.id || '',
          }));
        }
        continue;
      }
      let chatId = normalizeWhatsAppId(rawChatId);
      if (!chatId) {
        continue;
      }
      const selfSenderId = normalizeWhatsAppId(sock.user?.id || sock.user?.lid || '');
      let participantId = normalizeWhatsAppId(msg.key.participant || '');
      if (
        msg.key.fromMe
        && participantId
        && selfSenderId
        && chatId === selfSenderId
        && participantId !== selfSenderId
      ) {
        chatId = participantId;
      }
      identity.learnAliasFromDm({
        chatId,
        messageId: msg.key.id,
        fromMe: !!msg.key.fromMe,
        isGroup: chatId.endsWith('@g.us'),
      });
      const ids = canonicalizeMessageIds({
        chatId,
        participantId,
        selfSenderId,
        fromMe: !!msg.key.fromMe,
      }, normalizeWhatsAppId);
      chatId = ids.chatId;
      participantId = ids.participantId;
      const senderId = ids.senderId;
      const isGroup = ids.isGroup;
      const senderDisplayName = extractPossibleSenderName(msg);
      if (!msg.key.fromMe) {
        rememberPushName(senderId, senderDisplayName);
      }
      if (!msg.message) {
        rememberKnownChat(chatId, {
          isGroup,
          lastSenderName: (!isGroup && !msg.key.fromMe) ? senderDisplayName : '',
        });
        continue;
      }
      presence.rememberInboundLastMessage(msg);
      if (WHATSAPP_DEBUG) {
        console.log(JSON.stringify({
          event: 'upsert', type,
          fromMe: !!msg.key.fromMe, chatId,
          senderId,
          messageKeys: Object.keys(msg.message || {}),
        }));
      }
      const senderNumber = senderId.replace(/@.*/, '');
      if (!mode.forwardable) {
        continue;
      }

      // Handle !fromMe messages (from other people) based on mode.
      // Self-chat mode only responds to the user's own messages to
      // themselves — stranger DMs / group pings must never reach the
      // Python gateway, otherwise a pairing-code reply fires in response
      // to arbitrary incoming messages (#8389).
      if (!msg.key.fromMe) {
        if (WHATSAPP_MODE === 'self-chat') {
          console.log(JSON.stringify({
            event: 'ignored',
            reason: 'self_chat_mode_rejects_non_self',
            chatId,
            senderId,
          }));
          continue;
        }
        if (!matchesAllowedUser(senderId, ALLOWED_USERS, SESSION_DIR)) {
          console.log(JSON.stringify({
            event: 'ignored',
            reason: 'allowlist_mismatch',
            chatId,
            senderId,
          }));
          continue;
        }
      }

      const messageContent = getMessageContent(msg);
      const messageType = Object.keys(messageContent || {})[0] || '';
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

      let speakerRoleHint = 'user';
      let speakerNameHint = '';
      const decoratedAssistant = msg.key.fromMe ? parseDecoratedAssistantBody(body) : null;
      if (msg.key.fromMe && sentStore.isEcho(msg.key.id)) {
        if (WHATSAPP_DEBUG) {
          console.log(JSON.stringify({
            event: 'ignored',
            reason: 'recently_sent_agent_echo',
            chatId,
            messageId: msg.key.id,
          }));
        }
        continue;
      }
      const isAgentEcho = msg.key.fromMe && !!decoratedAssistant;
      if (isAgentEcho) {
        speakerRoleHint = 'assistant';
        speakerNameHint = decoratedAssistant?.speakerName || '';
        body = decoratedAssistant ? decoratedAssistant.body : body;
      } else if (msg.key.fromMe && WHATSAPP_MODE === 'self-chat') {
        const myNumber = (sock.user?.id || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const myLid = (sock.user?.lid || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const chatNumber = chatId.replace(/@.*/, '');
        const isSelfChat = (myNumber && chatNumber === myNumber) || (myLid && chatNumber === myLid);
        if (!isSelfChat) {
          if (WHATSAPP_DEBUG) {
            console.log(JSON.stringify({ event: 'ignored', reason: 'self_chat_mode_rejects_non_self_from_me', chatId, messageId: msg.key.id }));
          }
          continue;
        }
      }

      // Skip empty messages
      if (!body && !hasMedia) {
        if (WHATSAPP_DEBUG) {
          console.log(JSON.stringify({ event: 'ignored', reason: 'empty', chatId, messageKeys: Object.keys(msg.message || {}) }));
        }
        continue;
      }

      const resolvedSenderName = msg.key.fromMe
        ? (
          String(pushNameCache.get(senderId) || sock.user?.name || '').trim()
          || senderNumber
        )
        : (
          String(msg.pushName || pushNameCache.get(senderId) || senderNumber).trim()
          || senderNumber
        );
      const resolvedChatName = isGroup
        ? (await resolveGroupChatName(chatId)) || chatId.split('@')[0]
        : resolveDmDisplayName(chatId, knownChats.get(chatId));
      rememberKnownChat(chatId, {
        isGroup,
        name: resolvedChatName,
        lastSenderName: msg.key.fromMe ? '' : resolvedSenderName,
      });

      const event = {
        deliveryMode: mode.deliveryMode,
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
        speakerRoleHint,
        speakerNameHint,
      };
      const delivery = classifyUpsertEvent({
        type,
        isAgentEcho,
        timestamp: msg.messageTimestamp,
        bridgeStartedAtSeconds: BRIDGE_STARTED_AT_SECONDS,
        startupReplayGraceSeconds: STARTUP_REPLAY_GRACE_SECONDS,
      });
      event.deliveryMode = delivery.deliveryMode;
      if (delivery.persistOnly) {
        event.eventType = 'history_message';
      }

      const queued = durableQueue.enqueue(event);
      if (!queued && WHATSAPP_DEBUG) {
        console.log(JSON.stringify({
          event: 'ignored',
          reason: 'duplicate_event_uid',
          chatId: event.chatId,
          messageId: event.messageId,
          senderId: event.senderId,
        }));
      }
    }
  });

  sock.ev.on('messages.update', (updates) => {
    if (!Array.isArray(updates)) return;
    for (const row of updates) {
      const key = row?.key;
      const update = row?.update;
      const remoteJid = normalizeWhatsAppId(key?.remoteJid || '');
      const messageId = String(key?.id || '').trim();
      if (!remoteJid || !messageId || !update || typeof update !== 'object') continue;
      const stubType = Number(update.messageStubType);
      if (stubType !== WHATSAPP_REVOKE_STUB_TYPE) continue;
      if (update.message !== null && update.message !== undefined) continue;
      durableQueue.enqueue({
        eventType: 'revoke',
        deliveryMode: 'revoke',
        messageId,
        chatId: remoteJid,
        isGroup: remoteJid.endsWith('@g.us'),
        timestamp: Date.now(),
      });
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
  if (!sock || !socketLifecycle.isConnected()) {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, message, replyTo } = req.body;
  if (!chatId || !message) {
    return res.status(400).json({ error: 'chatId and message are required' });
  }

  try {
    const hadUnreadBeforeSend = presence.hasUnreadMessages(chatId);
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];
    for (let i = 0; i < chunks.length; i += 1) {
      const sent = await sendWithTimeout(chatId, { text: chunks[i] });
      sentStore.trackSent(sent?.key?.id);
      sentStore.storeSent(sent, { conversation: chunks[i] });
      if (sent?.key?.id) messageIds.push(sent.key.id);
      if (chunks.length > 1 && i < chunks.length - 1) {
        await sleep(CHUNK_DELAY_MS);
      }
    }

    await presence.postSendPresenceAndUnreadRestore(chatId, hadUnreadBeforeSend);

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
  if (!sock || !socketLifecycle.isConnected()) {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, messageId, message } = req.body;
  if (!chatId || !messageId || !message) {
    return res.status(400).json({ error: 'chatId, messageId, and message are required' });
  }

  try {
    const hadUnreadBeforeSend = presence.hasUnreadMessages(chatId);
    const key = { id: messageId, fromMe: true, remoteJid: chatId };
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];

    await sendWithTimeout(chatId, { text: chunks[0], edit: key });
    sentStore.storeSent({ key }, { conversation: chunks[0] });
    if (chunks.length > 1) {
      for (let i = 1; i < chunks.length; i += 1) {
        const sent = await sendWithTimeout(chatId, { text: chunks[i] });
        sentStore.trackSent(sent?.key?.id);
        sentStore.storeSent(sent, { conversation: chunks[i] });
        if (sent?.key?.id) messageIds.push(sent.key.id);
        if (i < chunks.length - 1) {
          await sleep(CHUNK_DELAY_MS);
        }
      }
    }

    await presence.postSendPresenceAndUnreadRestore(chatId, hadUnreadBeforeSend);
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
  if (!sock || !socketLifecycle.isConnected()) {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, filePath, mediaType, caption, fileName } = req.body;
  if (!chatId || !filePath) {
    return res.status(400).json({ error: 'chatId and filePath are required' });
  }

  try {
    const hadUnreadBeforeSend = presence.hasUnreadMessages(chatId);
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

    sentStore.trackSent(sent?.key?.id);
    sentStore.storeSent(
      sent,
      buildMediaRetryCachePayload(type, {
        caption,
        fileName: fileName || path.basename(filePath),
      }),
    );

    await presence.postSendPresenceAndUnreadRestore(chatId, hadUnreadBeforeSend);

    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Typing indicator
app.post('/typing', async (req, res) => {
  if (!sock || !socketLifecycle.isConnected()) {
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

  const chatRow = knownChats.get(chatId) || null;
  res.json({
    name: resolveDmDisplayName(chatId, chatRow),
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
    status: socketLifecycle.getState(),
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
  socketLifecycle.startNow();
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
    socketLifecycle.startNow();
  });
}
