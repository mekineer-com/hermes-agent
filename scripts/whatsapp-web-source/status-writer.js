'use strict';

const fs = require('fs');

const { ensureDir } = require('./daemon-utils');

class StatusWriter {
  constructor(statusPath, options = {}) {
    this.statusPath = statusPath;
    this.stats = typeof options.stats === 'function' ? options.stats : () => ({});
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
      pid: process.pid,
      ...this.current,
      ...this.pending,
      updated_at: Math.floor(Date.now() / 1000),
      ...this.stats(),
    };
    fs.writeFileSync(this.statusPath, `${JSON.stringify(status, null, 2)}\n`, { mode: 0o600 });
    this.current = status;
    this.pending = {};
  }
}

module.exports = { StatusWriter };
