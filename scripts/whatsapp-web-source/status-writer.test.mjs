import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { StatusWriter } from './status-writer.js';

test('StatusWriter writes merged status with injected stats', () => {
  const dir = mkdtempSync(join(tmpdir(), 'web-source-status-'));
  try {
    const statusPath = join(dir, 'nested', 'status.json');
    const writer = new StatusWriter(statusPath, {
      stats: () => ({ rss_mb: 12 }),
    });

    writer.write({ state: 'starting' }, { immediate: true });
    writer.write({ wwebjs_ready: true, db_writeable: true }, { immediate: true });

    const status = JSON.parse(readFileSync(statusPath, 'utf8'));
    assert.equal(status.service, 'whatsapp-web-source');
    assert.equal(status.state, 'starting');
    assert.equal(status.wwebjs_ready, true);
    assert.equal(status.db_writeable, true);
    assert.equal(status.rss_mb, 12);
    assert.equal(typeof status.updated_at, 'number');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
