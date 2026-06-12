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

test('StoreWriter command timeout kills hung store and rejects with descriptive error', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'web-source-store-timeout-'));
  let exitError;
  const writer = new StoreWriter(join(dir, 'web_source.db'), (err) => { exitError = err; });
  try {
    // Confirm the process is up.
    await writer.command('ping');

    // Suspend the store process so it can't respond to the next command.
    writer.proc.kill('SIGSTOP');

    // Issue a command with a short timeout — the stopped process won't respond.
    await assert.rejects(
      () => writer.command('ping', {}, 50),
      (err) => {
        assert.match(err.message, /timed out/);
        assert.match(err.message, /op=ping/);
        return true;
      },
    );

    // The SIGKILL should have triggered the exit handler.
    await new Promise((resolve) => setTimeout(resolve, 200));
    assert.ok(exitError, 'onExit callback should have been called after SIGKILL');
    assert.ok(writer.exitedError, 'exitedError should be set after process death');

    // Subsequent commands must reject immediately with the exit error.
    await assert.rejects(() => writer.command('ping'));
  } finally {
    writer.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
