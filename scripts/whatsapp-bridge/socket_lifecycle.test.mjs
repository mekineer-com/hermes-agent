import test from 'node:test';
import assert from 'node:assert/strict';

import { SocketLifecycle } from './socket_lifecycle.js';

const quietLogger = {
  log() {},
  warn() {},
  error() {},
};

function makeLifecycle(overrides = {}) {
  return new SocketLifecycle({
    baileysVersionFetchTimeoutMs: 5000,
    baileysVersionFallback: [2, 3000, 1023223821],
    onStart: async () => {},
    logger: quietLogger,
    ...overrides,
  });
}

test('socket lifecycle does not report connected until socket open', () => {
  const lifecycle = makeLifecycle();
  const socketId = lifecycle.beginStart();

  lifecycle.markReady(socketId);
  assert.equal(lifecycle.getState(), 'connecting');
  assert.equal(lifecycle.isConnected(), false);

  lifecycle.markOpen(socketId);
  assert.equal(lifecycle.getState(), 'connected');
  assert.equal(lifecycle.isConnected(), true);
});

test('socket lifecycle ignores stale socket updates', () => {
  const lifecycle = makeLifecycle();
  const staleSocketId = lifecycle.beginStart();
  lifecycle.beginStart();

  lifecycle.markOpen(staleSocketId);
  assert.equal(lifecycle.getState(), 'connecting');
});

test('socket lifecycle falls back when Baileys version fetch is invalid', async () => {
  const lifecycle = makeLifecycle({
    fetchLatestBaileysVersionFn: async ({ timeout }) => {
      assert.equal(timeout, 5000);
      return { version: null };
    },
  });

  assert.deepEqual(await lifecycle.fetchVersion(), [2, 3000, 1023223821]);
});
