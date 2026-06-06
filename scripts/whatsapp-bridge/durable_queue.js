import path from 'path';
import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from 'fs';

const DEFAULT_LIMIT = 100;

function parsePositiveInt(value, fallback) {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  if (!Number.isFinite(parsed) || parsed < 1) return fallback;
  return parsed;
}

function ensureParentDir(filePath) {
  const dir = path.dirname(filePath);
  mkdirSync(dir, { recursive: true });
  return dir;
}

function atomicWriteText(filePath, content) {
  const dir = ensureParentDir(filePath);
  const tmpPath = `${filePath}.tmp`;
  const fd = openSync(tmpPath, 'w');
  try {
    writeFileSync(fd, content, { encoding: 'utf8' });
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(tmpPath, filePath);
  const dirFd = openSync(dir, 'r');
  try {
    fsyncSync(dirFd);
  } finally {
    closeSync(dirFd);
  }
}

function eventUidFor(event) {
  const chatId = String(event?.chatId || '').trim();
  const messageId = String(event?.messageId || '').trim();
  if (!chatId || !messageId) return '';
  const eventType = String(event?.eventType || '').trim().toLowerCase();
  const deliveryMode = String(event?.deliveryMode || '').trim().toLowerCase();
  if (eventType === 'revoke' || deliveryMode === 'revoke') {
    return `revoke:${chatId}:${messageId}`;
  }
  if (deliveryMode === 'persist_only') {
    return `persist:${chatId}:${messageId}`;
  }
  if (deliveryMode === 'live') {
    return `live:${chatId}:${messageId}`;
  }
  // messageId is already chat-scoped in WhatsApp. Including senderId here
  // causes false misses when the same participant surfaces as @lid vs
  // @s.whatsapp.net across reconnects/replays.
  return `${chatId}:${messageId}`;
}

export class DurableQueue {
  constructor({
    queueDir,
    defaultLimit = DEFAULT_LIMIT,
    compactionEveryAcks = 100,
  }) {
    if (!queueDir) throw new Error('queueDir is required');
    this.queueDir = queueDir;
    this.queuePath = path.join(queueDir, 'queue.jsonl');
    this.offsetPath = path.join(queueDir, 'queue.offset');
    this.seenPath = path.join(queueDir, 'queue.seen');
    this.defaultLimit = parsePositiveInt(defaultLimit, DEFAULT_LIMIT);
    this.compactionEveryAcks = parsePositiveInt(compactionEveryAcks, 100);
    this.ackedUpToSeq = 0;
    this.maxSeq = 0;
    this.nextSeq = 1;
    this.unacked = [];
    this.unackedUidSet = new Set();
    this.seenUidSet = new Set();
    this.ackSinceCompaction = 0;

    this._load();
  }

  _load() {
    mkdirSync(this.queueDir, { recursive: true });
    this.ackedUpToSeq = this._readAckedOffset();
    this._loadSeen();
    const seenFileExists = existsSync(this.seenPath);
    let seenDirty = !seenFileExists;

    if (!existsSync(this.queuePath)) {
      if (seenDirty) {
        this._persistSeen();
      }
      this.nextSeq = this.ackedUpToSeq + 1;
      return;
    }

    const raw = readFileSync(this.queuePath, 'utf8');
    const lines = raw.split('\n');
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let row;
      try {
        row = JSON.parse(trimmed);
      } catch {
        continue;
      }
      const seq = Number(row?.seq);
      if (!Number.isFinite(seq) || seq < 1) continue;
      if (seq > this.maxSeq) this.maxSeq = seq;
      const eventUid = eventUidFor(row);
      if (eventUid && !this.seenUidSet.has(eventUid)) {
        this.seenUidSet.add(eventUid);
        seenDirty = true;
      }
      if (seq <= this.ackedUpToSeq) continue;
      this.unacked.push(row);
      if (eventUid) this.unackedUidSet.add(eventUid);
    }
    if (seenDirty) {
      this._persistSeen();
    }
    this.nextSeq = Math.max(this.maxSeq + 1, this.ackedUpToSeq + 1);
  }

  _readAckedOffset() {
    if (!existsSync(this.offsetPath)) return 0;
    const raw = String(readFileSync(this.offsetPath, 'utf8') || '').trim();
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed) || parsed < 0) return 0;
    return parsed;
  }

  _appendRow(row) {
    const fd = openSync(this.queuePath, 'a');
    try {
      writeFileSync(fd, `${JSON.stringify(row)}\n`, { encoding: 'utf8' });
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
  }

  _loadSeen() {
    if (!existsSync(this.seenPath)) return;
    const raw = readFileSync(this.seenPath, 'utf8');
    const lines = raw.split('\n');
    for (const line of lines) {
      const uid = String(line || '').trim();
      if (uid) this.seenUidSet.add(uid);
    }
  }

  _appendSeenUid(eventUid) {
    const fd = openSync(this.seenPath, 'a');
    try {
      writeFileSync(fd, `${eventUid}\n`, { encoding: 'utf8' });
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
  }

  _persistSeen() {
    const lines = Array.from(this.seenUidSet);
    if (!lines.length) {
      atomicWriteText(this.seenPath, '');
      return;
    }
    atomicWriteText(this.seenPath, `${lines.join('\n')}\n`);
  }

  _persistOffset() {
    atomicWriteText(this.offsetPath, `${this.ackedUpToSeq}\n`);
  }

  _compact() {
    const tmpPath = `${this.queuePath}.tmp`;
    const fd = openSync(tmpPath, 'w');
    try {
      for (const row of this.unacked) {
        writeFileSync(fd, `${JSON.stringify(row)}\n`, { encoding: 'utf8' });
      }
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    renameSync(tmpPath, this.queuePath);
    const dirFd = openSync(this.queueDir, 'r');
    try {
      fsyncSync(dirFd);
    } finally {
      closeSync(dirFd);
    }
    this.ackSinceCompaction = 0;
  }

  enqueue(event) {
    const eventUid = eventUidFor(event);
    if (!eventUid) return null;
    if (this.seenUidSet.has(eventUid)) return null;
    if (this.unackedUidSet.has(eventUid)) return null;

    const seq = this.nextSeq;
    this.nextSeq += 1;
    if (seq > this.maxSeq) this.maxSeq = seq;
    const row = {
      seq,
      event_uid: eventUid,
      ...event,
    };
    this._appendRow(row);
    this._appendSeenUid(eventUid);
    this.unacked.push(row);
    this.unackedUidSet.add(eventUid);
    this.seenUidSet.add(eventUid);
    return row;
  }

  readUnacked(limit) {
    const n = parsePositiveInt(limit, this.defaultLimit);
    return this.unacked.slice(0, n);
  }

  ackThrough(upToSeq) {
    if (!Number.isInteger(upToSeq) || upToSeq < 0) {
      throw new TypeError(`upToSeq must be a non-negative integer, got ${upToSeq}`);
    }
    const target = Math.min(upToSeq, this.maxSeq);
    if (target <= this.ackedUpToSeq) {
      return { ackedUpToSeq: this.ackedUpToSeq, removed: 0 };
    }

    const prev = this.ackedUpToSeq;
    this.ackedUpToSeq = target;
    const kept = [];
    let removed = 0;
    this.unackedUidSet.clear();
    for (const row of this.unacked) {
      if (Number(row?.seq) <= target) {
        removed += 1;
        continue;
      }
      kept.push(row);
      const uid = String(row?.event_uid || '').trim();
      if (uid) this.unackedUidSet.add(uid);
    }
    this.unacked = kept;
    this.ackSinceCompaction += (target - prev);
    this._persistOffset();
    if (this.ackSinceCompaction >= this.compactionEveryAcks) {
      this._compact();
    }
    return { ackedUpToSeq: this.ackedUpToSeq, removed };
  }

  forceCompact() {
    this._compact();
  }

  getStats() {
    return {
      ackedUpToSeq: this.ackedUpToSeq,
      maxSeq: this.maxSeq,
      queueLength: this.unacked.length,
      seenCount: this.seenUidSet.size,
      nextSeq: this.nextSeq,
    };
  }
}
