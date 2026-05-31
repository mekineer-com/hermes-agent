import test from 'node:test';
import assert from 'node:assert/strict';

import { buildMediaRetryCachePayload } from './media_retry_cache.js';

test('buildMediaRetryCachePayload builds image/video caption payloads', () => {
  assert.deepEqual(
    buildMediaRetryCachePayload('image', { caption: 'hello' }),
    { image: { caption: 'hello' } },
  );
  assert.deepEqual(
    buildMediaRetryCachePayload('video', { caption: 'clip' }),
    { video: { caption: 'clip' } },
  );
});

test('buildMediaRetryCachePayload keeps audio metadata-only payload', () => {
  assert.deepEqual(buildMediaRetryCachePayload('audio', { caption: 'ignored' }), { audio: {} });
});

test('buildMediaRetryCachePayload defaults unknown to document payload', () => {
  assert.deepEqual(
    buildMediaRetryCachePayload('document', { fileName: 'x.pdf', caption: 'doc' }),
    { document: { fileName: 'x.pdf', caption: 'doc' } },
  );
  assert.deepEqual(
    buildMediaRetryCachePayload('unknown', { fileName: 'y.bin', caption: '' }),
    { document: { fileName: 'y.bin', caption: undefined } },
  );
});
