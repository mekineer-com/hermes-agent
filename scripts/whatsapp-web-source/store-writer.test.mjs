import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { StoreWriter } from './store-writer.js';

test('StoreWriter round-trips commands through the Python store process', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'web-source-store-'));
  const writer = new StoreWriter(join(dir, 'web_source.db'));
  try {
    const pong = await writer.command('ping');
    assert.equal(pong.status, 'ok');

    await writer.command('set_metadata', { key: 'test:key', value: 'value' });
    const row = await writer.command('get_metadata', { key: 'test:key' });
    assert.equal(row.value, 'value');
  } finally {
    writer.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
