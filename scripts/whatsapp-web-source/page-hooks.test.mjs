import assert from 'node:assert/strict';
import { test } from 'node:test';
import { configureResourceBlocking, installRemoveMessageHook } from './page-hooks.js';

function request(type) {
  const calls = [];
  return {
    calls,
    resourceType: () => type,
    abort: async () => calls.push('abort'),
    continue: async () => calls.push('continue'),
  };
}

test('configureResourceBlocking aborts heavy resources and allows other requests', async () => {
  let handler = null;
  const page = {
    intercepted: false,
    setRequestInterception: async (value) => { page.intercepted = value; },
    on: (event, callback) => {
      assert.equal(event, 'request');
      handler = callback;
    },
  };

  await configureResourceBlocking(page);
  const image = request('image');
  const script = request('script');
  handler(image);
  handler(script);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(page.intercepted, true);
  assert.deepEqual(image.calls, ['abort']);
  assert.deepEqual(script.calls, ['continue']);
});

test('installRemoveMessageHook injects one page evaluate hook', async () => {
  const calls = [];
  await installRemoveMessageHook({
    evaluate: async (callback) => {
      calls.push(callback);
    },
  });

  assert.equal(calls.length, 1);
  assert.equal(typeof calls[0], 'function');
});
