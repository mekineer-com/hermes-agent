import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  MemoryDiagnostics,
  memoryStatsMb,
  pageDiagnostics,
} from './memory-diagnostics.js';

test('memoryStatsMb reports process memory in megabytes', () => {
  const stats = memoryStatsMb();
  assert.equal(typeof stats.rss_mb, 'number');
  assert.equal(typeof stats.heap_used_mb, 'number');
  assert.ok(stats.rss_mb > 0);
});

test('pageDiagnostics keeps the status JSON shape', async () => {
  const diagnostics = await pageDiagnostics({
    metrics: async () => ({
      JSHeapUsedSize: 2 * 1024 * 1024,
      JSHeapTotalSize: 4 * 1024 * 1024,
      Nodes: 3,
      Documents: 1,
      JSEventListeners: 5,
    }),
    evaluate: async () => ({ msg: 7, chat: 2, contact: 9 }),
  });

  assert.deepEqual(diagnostics, {
    page_metrics: {
      js_heap_used_mb: 2,
      js_heap_total_mb: 4,
      nodes: 3,
      documents: 1,
      js_event_listeners: 5,
    },
    wa_collection_counts: { msg: 7, chat: 2, contact: 9 },
  });
});

test('MemoryDiagnostics writes Chromium and page fields without raw cpu_ticks', async () => {
  const writes = [];
  const diagnostics = new MemoryDiagnostics({
    intervalSeconds: 0,
    isReady: () => true,
    rootPid: () => 42,
    page: () => ({}),
    dbWriteable: () => true,
    status: { write: (row) => writes.push(row) },
    collectChromium: () => ({
      total_rss_mb: 33,
      process_count: 1,
      processes: [{
        pid: 43,
        ppid: 42,
        type: 'renderer',
        state: 'S',
        rss_mb: 33,
        cpu_ticks: 100,
        cpu_ticks_delta: null,
        cpu_ticks_per_second: null,
      }],
    }),
    collectPage: async () => ({
      page_metrics: null,
      wa_collection_counts: { msg: 1, chat: 1, contact: 1 },
    }),
    now: () => 1234000,
  });

  await diagnostics.collect();

  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0], {
    state: 'ready',
    wwebjs_ready: true,
    db_writeable: true,
    memory_diagnostics_at: 1234,
    chromium_root_pid: 42,
    chromium_total_rss_mb: 33,
    chromium_process_count: 1,
    chromium_processes: [{
      pid: 43,
      ppid: 42,
      type: 'renderer',
      state: 'S',
      rss_mb: 33,
      cpu_ticks_delta: null,
      cpu_ticks_per_second: null,
    }],
    page_metrics: null,
    wa_collection_counts: { msg: 1, chat: 1, contact: 1 },
  });
});
