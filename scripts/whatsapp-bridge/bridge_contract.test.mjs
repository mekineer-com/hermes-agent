import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const bridgeSource = readFileSync(join(here, 'bridge.js'), 'utf8');

test('bridge preserves persist-only WhatsApp event field contract', () => {
  assert.match(bridgeSource, /event\.eventType\s*=\s*'history_message'/);
  assert.match(bridgeSource, /deliveryMode:\s*'persist_only'/);
});

test('bridge stamps explicit live and revoke delivery modes', () => {
  assert.match(bridgeSource, /deliveryMode:\s*mode\.deliveryMode/);
  assert.match(bridgeSource, /event\.deliveryMode\s*=\s*delivery\.deliveryMode/);
  assert.match(bridgeSource, /deliveryMode:\s*'revoke'/);
});

test('bridge keeps Baileys live path light and bounded', () => {
  assert.match(bridgeSource, /fireInitQueries:\s*false/);
  assert.match(bridgeSource, /syncFullHistory:\s*false/);
  assert.match(bridgeSource, /fetchLatestBaileysVersion\(\{\s*timeout:\s*timeoutMs\s*\}\)/);
});

test('bridge does not report connected until socket open', () => {
  assert.match(bridgeSource, /receivedPendingNotifications/);
  assert.match(bridgeSource, /markSocketReady\(socketId\)/);
  assert.match(bridgeSource, /openSocketGeneration\s*!==\s*socketId/);
  assert.match(bridgeSource, /status:\s*connectionState/);
  assert.match(bridgeSource, /connectionState\s*=\s*'connecting'/);
});
