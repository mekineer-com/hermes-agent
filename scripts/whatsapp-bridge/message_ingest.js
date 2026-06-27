import { downloadMediaMessage } from '@whiskeysockets/baileys';
import path from 'path';
import { mkdirSync, writeFileSync } from 'fs';
import { randomBytes } from 'crypto';

import { matchesAllowedUser } from './allowlist.js';
import {
  canonicalizeMessageIds,
  classifyUpsertEvent,
  historyMessageSources,
  isStartupReplay,
  upsertEventMode,
} from './history_ingest.js';

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

function syncTimestampAllowed(value, windowDays) {
  if (!Number.isFinite(windowDays) || windowDays <= 0) return true;
  const ts = timestampSeconds(value);
  if (!ts) return false;
  const cutoff = Date.now() / 1000 - (windowDays * 24 * 60 * 60);
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

function parseDecoratedAssistantBody(body, replyPrefix) {
  const text = String(body || '');
  if (!text) return null;
  if (replyPrefix && text.startsWith(replyPrefix)) {
    return {
      body: text.slice(replyPrefix.length).trimStart(),
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

export function createMessageIngest(ctx) {
  const {
    durableQueue,
    identity,
    knownState,
    presence,
    sentStore,
    normalizeId,
    getSock,
    resolveGroupChatName,
    logger,
    config,
  } = ctx;

  async function enqueueHistoryMessage(rawMsg, { chatFallback = '', surface = 'sync' } = {}) {
    const sock = getSock();
    const msg = rawMsg?.message && rawMsg?.key === undefined ? rawMsg.message : rawMsg;
    if (!msg?.key) return false;
    const messageId = String(msg.key.id || '').trim();
    let chatId = normalizeId(msg.key.remoteJid || chatFallback || '');
    if (!chatId || !messageId || chatId.toLowerCase() === 'status@broadcast') return false;

    const timestamp = msg.messageTimestamp || msg.messageC2STimestamp || rawMsg?.messageTimestamp;
    if (!syncTimestampAllowed(timestamp, config.syncHistoryWindowDays)) return false;
    if (
      surface === 'chats.update'
      && !isStartupReplay({
        timestamp,
        bridgeStartedAtSeconds: config.bridgeStartedAtSeconds,
        graceSeconds: config.startupReplayGraceSeconds,
      })
    ) {
      return false;
    }

    const isGroup = chatId.endsWith('@g.us');
    const targetRevokeId = Array.isArray(msg.messageStubParameters)
      ? String(msg.messageStubParameters[0] || '').trim()
      : '';
    if (Number(msg.messageStubType) === config.revokeStubType && targetRevokeId) {
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

    let participantId = normalizeId(msg.key.participant || '');
    const selfSenderId = normalizeId(sock?.user?.id || sock?.user?.lid || '');
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
      if (config.debug) {
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
    }, normalizeId);
    chatId = ids.chatId;
    participantId = ids.participantId;
    const senderId = ids.senderId;
    const { body: extractedBody, hasMedia, mediaType } = extractTextAndMedia(messageContent);
    let body = extractedBody;
    let speakerRoleHint = 'user';
    let speakerNameHint = '';
    if (msg.key.fromMe) {
      if (sentStore.isEcho(messageId)) return false;
      const parsed = parseDecoratedAssistantBody(body, config.replyPrefix);
      if (parsed) {
        speakerRoleHint = 'assistant';
        speakerNameHint = parsed?.speakerName || '';
        body = parsed ? parsed.body : body;
      }
    }
    if (!body && !hasMedia) return false;

    const senderNumber = senderId.replace(/@.*/, '');
    const senderDisplayName = knownState.extractPossibleSenderName(msg);
    if (!msg.key.fromMe) {
      knownState.rememberPushName(senderId, senderDisplayName);
    }
    const resolvedSenderName = msg.key.fromMe
      ? (String(knownState.getPushName(senderId) || sock?.user?.name || '').trim() || senderNumber)
      : (String(msg.pushName || knownState.getPushName(senderId) || senderDisplayName || senderNumber).trim() || senderNumber);
    const resolvedChatName = chatId.endsWith('@g.us')
      ? (await resolveGroupChatName(chatId)) || chatId.split('@')[0]
      : knownState.resolveDmDisplayName(chatId, knownState.getChat(chatId));
    knownState.rememberChat(chatId, {
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

  async function enqueueHistoryMessages(payload, surface) {
    for (const row of historyMessageSources(payload, normalizeId)) {
      await enqueueHistoryMessage(row.message, { chatFallback: row.chatFallback, surface });
    }
  }

  async function enqueueHistoryMessagesFromChats(chats, surface) {
    await enqueueHistoryMessages({ chats }, surface);
  }

  async function handleUpsert({ messages, type }) {
    const sock = getSock();
    const mode = upsertEventMode(type);

    const botIds = Array.from(new Set([
      normalizeId(sock.user?.id),
      normalizeId(sock.user?.lid),
    ].filter(Boolean)));

    for (const msg of messages) {
      const rawChatId = String(msg.key.remoteJid || '');
      const isStatusUpdate = rawChatId.toLowerCase() === 'status@broadcast';
      if (isStatusUpdate) {
        if (config.debug) {
          console.log(JSON.stringify({
            event: 'ignored',
            reason: 'status_update',
            chatId: rawChatId,
            messageId: msg.key.id || '',
          }));
        }
        continue;
      }
      let chatId = normalizeId(rawChatId);
      if (!chatId) {
        continue;
      }
      const selfSenderId = normalizeId(sock.user?.id || sock.user?.lid || '');
      let participantId = normalizeId(msg.key.participant || '');
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
      }, normalizeId);
      chatId = ids.chatId;
      participantId = ids.participantId;
      const senderId = ids.senderId;
      const isGroup = ids.isGroup;
      const senderDisplayName = knownState.extractPossibleSenderName(msg);
      if (!msg.key.fromMe) {
        knownState.rememberPushName(senderId, senderDisplayName);
      }
      if (!msg.message) {
        knownState.rememberChat(chatId, {
          isGroup,
          lastSenderName: (!isGroup && !msg.key.fromMe) ? senderDisplayName : '',
        });
        continue;
      }
      presence.rememberInboundLastMessage(msg);
      if (config.debug) {
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

      if (!msg.key.fromMe) {
        if (config.whatsappMode === 'self-chat') {
          console.log(JSON.stringify({
            event: 'ignored',
            reason: 'self_chat_mode_rejects_non_self',
            chatId,
            senderId,
          }));
          continue;
        }
        if (!matchesAllowedUser(senderId, config.allowedUsers, config.sessionDir)) {
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
      const contextInfo = getContextInfo(messageContent);
      const mentionedIds = Array.from(new Set((contextInfo?.mentionedJid || []).map(normalizeId).filter(Boolean)));
      const quotedMessageId = contextInfo?.stanzaId || null;
      const quotedParticipant = normalizeId(contextInfo?.participant || '') || null;
      const quotedRemoteJid = normalizeId(contextInfo?.remoteJid || '') || null;
      const hasQuotedMessage = !!contextInfo?.quotedMessage;

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
          mkdirSync(config.imageCacheDir, { recursive: true });
          const filePath = path.join(config.imageCacheDir, `img_${randomBytes(6).toString('hex')}${ext}`);
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
          mkdirSync(config.documentCacheDir, { recursive: true });
          const filePath = path.join(config.documentCacheDir, `vid_${randomBytes(6).toString('hex')}${ext}`);
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
          mkdirSync(config.audioCacheDir, { recursive: true });
          const filePath = path.join(config.audioCacheDir, `aud_${randomBytes(6).toString('hex')}${ext}`);
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
          mkdirSync(config.documentCacheDir, { recursive: true });
          const safeFileName = path.basename(fileName).replace(/[^a-zA-Z0-9._-]/g, '_');
          const filePath = path.join(config.documentCacheDir, `doc_${randomBytes(6).toString('hex')}_${safeFileName}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download document:', err.message);
        }
      }

      if (hasMedia && !body) {
        body = `[${mediaType} received]`;
      }

      let speakerRoleHint = 'user';
      let speakerNameHint = '';
      const decoratedAssistant = msg.key.fromMe ? parseDecoratedAssistantBody(body, config.replyPrefix) : null;
      if (msg.key.fromMe && sentStore.isEcho(msg.key.id)) {
        if (config.debug) {
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
      } else if (msg.key.fromMe && config.whatsappMode === 'self-chat') {
        const myNumber = (sock.user?.id || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const myLid = (sock.user?.lid || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const chatNumber = chatId.replace(/@.*/, '');
        const isSelfChat = (myNumber && chatNumber === myNumber) || (myLid && chatNumber === myLid);
        if (!isSelfChat) {
          if (config.debug) {
            console.log(JSON.stringify({ event: 'ignored', reason: 'self_chat_mode_rejects_non_self_from_me', chatId, messageId: msg.key.id }));
          }
          continue;
        }
      }

      if (!body && !hasMedia) {
        if (config.debug) {
          console.log(JSON.stringify({ event: 'ignored', reason: 'empty', chatId, messageKeys: Object.keys(msg.message || {}) }));
        }
        continue;
      }

      const resolvedSenderName = msg.key.fromMe
        ? (
          String(knownState.getPushName(senderId) || sock.user?.name || '').trim()
          || senderNumber
        )
        : (
          String(msg.pushName || knownState.getPushName(senderId) || senderNumber).trim()
          || senderNumber
        );
      const resolvedChatName = isGroup
        ? (await resolveGroupChatName(chatId)) || chatId.split('@')[0]
        : knownState.resolveDmDisplayName(chatId, knownState.getChat(chatId));
      knownState.rememberChat(chatId, {
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
        bridgeStartedAtSeconds: config.bridgeStartedAtSeconds,
        startupReplayGraceSeconds: config.startupReplayGraceSeconds,
      });
      event.deliveryMode = delivery.deliveryMode;
      if (delivery.persistOnly) {
        event.eventType = 'history_message';
      }

      const queued = durableQueue.enqueue(event);
      if (!queued && config.debug) {
        console.log(JSON.stringify({
          event: 'ignored',
          reason: 'duplicate_event_uid',
          chatId: event.chatId,
          messageId: event.messageId,
          senderId: event.senderId,
        }));
      }
    }
  }

  function handleUpdate(updates) {
    if (!Array.isArray(updates)) return;
    for (const row of updates) {
      const key = row?.key;
      const update = row?.update;
      const remoteJid = normalizeId(key?.remoteJid || '');
      const messageId = String(key?.id || '').trim();
      if (!remoteJid || !messageId || !update || typeof update !== 'object') continue;
      const stubType = Number(update.messageStubType);
      if (stubType !== config.revokeStubType) continue;
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
  }

  return {
    enqueueHistoryMessage,
    enqueueHistoryMessages,
    enqueueHistoryMessagesFromChats,
    handleUpsert,
    handleUpdate,
  };
}
