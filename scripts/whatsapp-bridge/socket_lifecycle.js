import { fetchLatestBaileysVersion } from '@whiskeysockets/baileys';

export class SocketLifecycle {
  constructor({
    baileysVersionFetchTimeoutMs,
    baileysVersionFallback,
    onStart,
    fetchLatestBaileysVersionFn = fetchLatestBaileysVersion,
    logger = console,
  }) {
    this.baileysVersionFetchTimeoutMs = baileysVersionFetchTimeoutMs;
    this.baileysVersionFallback = baileysVersionFallback;
    this.onStart = onStart;
    this.fetchLatestBaileysVersion = fetchLatestBaileysVersionFn;
    this.logger = logger;
    this.state = 'disconnected';
    this.generation = 0;
    this.openGeneration = 0;
    this.reconnectTimer = null;
  }

  beginStart() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.generation += 1;
    this.openGeneration = 0;
    this.state = 'connecting';
    return this.generation;
  }

  isCurrent(socketId) {
    return socketId === this.generation;
  }

  markOpen(socketId) {
    if (!this.isCurrent(socketId)) return;
    this.openGeneration = socketId;
    this.markReady(socketId);
  }

  markReady(socketId) {
    if (!this.isCurrent(socketId)) return;
    if (this.openGeneration !== socketId) return;
    if (this.state !== 'connected') {
      this.state = 'connected';
      this.logger.log('✅ WhatsApp connected!');
    }
  }

  markDisconnected(socketId = null) {
    if (socketId !== null && !this.isCurrent(socketId)) return;
    this.state = 'disconnected';
  }

  scheduleStart(delayMs) {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.startNow();
    }, delayMs);
  }

  startNow() {
    this.onStart().catch((err) => {
      this.state = 'disconnected';
      this.logger.error(`❌ WhatsApp socket start failed: ${err?.message || err}`);
      this.scheduleStart(3000);
    });
  }

  getState() {
    return this.state;
  }

  isConnected() {
    return this.state === 'connected';
  }

  async fetchVersion() {
    const timeoutMs = Number.isFinite(this.baileysVersionFetchTimeoutMs) && this.baileysVersionFetchTimeoutMs > 0
      ? this.baileysVersionFetchTimeoutMs
      : 5000;
    const result = await this.fetchLatestBaileysVersion({ timeout: timeoutMs });
    if (Array.isArray(result?.version) && result.version.length === 3) {
      if (result.error) {
        this.logger.warn(`⚠️  Using packaged Baileys version fallback after fetch failed: ${result.error?.message || result.error}`);
      }
      return result.version;
    }
    this.logger.warn('⚠️  Baileys version fetch returned invalid data; using bridge fallback version.');
    return this.baileysVersionFallback;
  }
}
