import assert from 'node:assert/strict';
import { test } from 'node:test';
import { browserArgs, buildClientOptions } from './browser-options.js';

class FakeLocalAuth {
  constructor(options) {
    this.options = options;
  }
}

test('browserArgs preserves baseline Chromium flags', () => {
  assert.deepEqual(browserArgs(false), [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-accelerated-2d-canvas',
    '--no-first-run',
    '--no-zygote',
    '--disable-gpu',
    '--disable-extensions',
    '--disable-software-rasterizer',
    '--mute-audio',
  ]);
});

test('browserArgs keeps service worker flag opt-in', () => {
  assert.ok(browserArgs(true).includes('--disable-features=ServiceWorker'));
});

test('buildClientOptions preserves LocalAuth, user agent, executable, and headless settings', () => {
  const options = buildClientOptions({
    LocalAuth: FakeLocalAuth,
    clientId: 'memu-web-source',
    authPath: '/tmp/auth',
    userAgent: 'ua',
    headless: false,
    executablePath: '/usr/bin/chromium',
    disableServiceWorkers: true,
  });

  assert.deepEqual(options.authStrategy.options, {
    clientId: 'memu-web-source',
    dataPath: '/tmp/auth',
  });
  assert.equal(options.userAgent, 'ua');
  assert.equal(options.puppeteer.headless, false);
  assert.equal(options.puppeteer.executablePath, '/usr/bin/chromium');
  assert.ok(options.puppeteer.args.includes('--disable-features=ServiceWorker'));
});
