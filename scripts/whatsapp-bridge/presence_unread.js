export class PresenceUnread {
  constructor({
    normalizeId,
    getSock,
    isConnected,
    preserveUnreadOnSend,
    sendUnavailableAfterActivity,
    debug = false,
  }) {
    this.normalizeId = normalizeId;
    this.getSock = getSock;
    this.isConnected = isConnected;
    this.preserveUnreadOnSend = preserveUnreadOnSend;
    this.sendUnavailableAfterActivity = sendUnavailableAfterActivity;
    this.debug = debug;
    this.chatUnreadCounts = new Map();
    this.lastInboundMessageByChat = new Map();
  }

  updateUnreadCountSnapshot(chats) {
    if (!Array.isArray(chats)) return;
    for (const chat of chats) {
      const chatId = this.normalizeId(chat?.id || chat?.jid || '');
      if (!chatId) continue;
      if (chat?.unreadCount === undefined || chat?.unreadCount === null) continue;
      const unreadCount = Number(chat.unreadCount);
      if (Number.isFinite(unreadCount) && unreadCount >= 0) {
        this.chatUnreadCounts.set(chatId, unreadCount);
      }
    }
  }

  rememberInboundLastMessage(msg) {
    const chatId = this.normalizeId(msg?.key?.remoteJid || '');
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
      key.participant = this.normalizeId(msg.key.participant);
    }

    this.lastInboundMessageByChat.set(chatId, {
      key,
      messageTimestamp: ts,
    });
  }

  hasUnreadMessages(chatId) {
    const unread = Number(this.chatUnreadCounts.get(this.normalizeId(chatId)));
    return Number.isFinite(unread) && unread > 0;
  }

  async postSendPresenceAndUnreadRestore(chatId, hadUnreadBeforeSend) {
    const sock = this.getSock();
    if (!sock || !this.isConnected()) return;

    if (this.sendUnavailableAfterActivity) {
      try {
        await sock.sendPresenceUpdate('unavailable');
      } catch (err) {
        if (this.debug) {
          console.log(JSON.stringify({
            event: 'warn',
            reason: 'set_unavailable_failed',
            chatId,
            error: err?.message || String(err),
          }));
        }
      }
    }

    if (!this.preserveUnreadOnSend || !hadUnreadBeforeSend) return;
    const normalizedChatId = this.normalizeId(chatId);
    const lastInbound = this.lastInboundMessageByChat.get(normalizedChatId);
    if (!lastInbound?.key?.id || !lastInbound?.messageTimestamp) return;

    try {
      await sock.chatModify(
        { markRead: false, lastMessages: [lastInbound] },
        normalizedChatId,
      );
    } catch (err) {
      if (this.debug) {
        console.log(JSON.stringify({
          event: 'warn',
          reason: 'preserve_unread_failed',
          chatId: normalizedChatId,
          error: err?.message || String(err),
        }));
      }
    }
  }
}
