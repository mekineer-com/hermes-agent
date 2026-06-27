import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

test('source daemon help preserves active-scope and diagnostics flags', () => {
  const output = execFileSync(process.execPath, [join(here, 'source-daemon.js'), '--help'], {
    encoding: 'utf8',
  });

  assert.match(output, /--active-since EPOCH/);
  assert.match(output, /--memory-diagnostics-interval SECONDS/);
  assert.match(output, /--backfill-since EPOCH/);
  assert.match(output, /--no-contact-snapshot/);
});
