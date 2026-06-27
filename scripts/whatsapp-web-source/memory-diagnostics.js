'use strict';

const fs = require('fs');

function memoryStatsMb() {
  const mem = process.memoryUsage();
  return {
    rss_mb: Math.round(mem.rss / 1024 / 1024),
    heap_used_mb: Math.round(mem.heapUsed / 1024 / 1024),
  };
}

function parseProcStatus(pid) {
  const statusPath = `/proc/${pid}/status`;
  const cmdlinePath = `/proc/${pid}/cmdline`;
  const statPath = `/proc/${pid}/stat`;
  const status = fs.readFileSync(statusPath, 'utf8');
  const get = (name) => {
    const match = status.match(new RegExp(`^${name}:\\s+(.+)$`, 'm'));
    return match ? match[1].trim() : '';
  };
  const cmdline = fs.readFileSync(cmdlinePath, 'utf8').replace(/\0/g, ' ').trim();
  const stat = fs.readFileSync(statPath, 'utf8');
  const afterName = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
  const typeMatch = cmdline.match(/--type=([^ ]+)/);
  return {
    pid,
    ppid: Number(get('PPid') || 0),
    name: get('Name'),
    state: get('State').split(/\s+/, 1)[0] || '',
    rss_mb: Math.round((Number((get('VmRSS').match(/\d+/) || ['0'])[0]) || 0) / 1024),
    process_type: typeMatch ? typeMatch[1] : 'browser',
    cpu_ticks: (Number(afterName[11]) || 0) + (Number(afterName[12]) || 0),
  };
}

function chromiumProcessTree(rootPid, previous = new Map()) {
  let processes = [];
  try {
    processes = fs.readdirSync('/proc')
      .filter((name) => /^\d+$/.test(name))
      .map((name) => {
        try {
          return parseProcStatus(Number(name));
        } catch (_error) {
          return null;
        }
      })
      .filter(Boolean);
  } catch (_error) {
    return { total_rss_mb: 0, processes: [] };
  }

  const children = new Map();
  for (const proc of processes) {
    if (!children.has(proc.ppid)) children.set(proc.ppid, []);
    children.get(proc.ppid).push(proc);
  }
  const descendants = [];
  const stack = [...(children.get(rootPid) || [])];
  while (stack.length > 0) {
    const proc = stack.pop();
    if (!proc) continue;
    descendants.push(proc);
    stack.push(...(children.get(proc.pid) || []));
  }

  const nowMs = Date.now();
  let totalRssMb = 0;
  const rows = descendants
    .filter((proc) => proc.name === 'chromium' || proc.name === 'chrome' || proc.process_type !== 'browser')
    .map((proc) => {
      totalRssMb += proc.rss_mb;
      const prev = previous.get(proc.pid);
      const elapsedSeconds = prev ? Math.max((nowMs - prev.sample_ms) / 1000, 0.001) : null;
      const cpuTicksDelta = prev ? Math.max(proc.cpu_ticks - prev.cpu_ticks, 0) : null;
      return {
        pid: proc.pid,
        ppid: proc.ppid,
        type: proc.process_type,
        state: proc.state,
        rss_mb: proc.rss_mb,
        cpu_ticks: proc.cpu_ticks,
        cpu_ticks_delta: cpuTicksDelta,
        cpu_ticks_per_second: elapsedSeconds && cpuTicksDelta !== null
          ? Math.round(cpuTicksDelta / elapsedSeconds)
          : null,
      };
    })
    .sort((a, b) => b.rss_mb - a.rss_mb);

  return {
    total_rss_mb: totalRssMb,
    process_count: rows.length,
    processes: rows.slice(0, 8),
  };
}

async function pageDiagnostics(page) {
  const [metrics, collections] = await Promise.all([
    page.metrics().catch(() => null),
    page.evaluate(() => {
      const requireFn = window.require;
      const collections = typeof requireFn === 'function' ? requireFn('WAWebCollections') : null;
      return {
        msg: collections?.Msg?.getModelsArray?.().length ?? null,
        chat: collections?.Chat?.getModelsArray?.().length ?? null,
        contact: collections?.Contact?.getModelsArray?.().length ?? null,
      };
    }).catch(() => null),
  ]);
  return {
    page_metrics: metrics ? {
      js_heap_used_mb: Math.round(metrics.JSHeapUsedSize / 1024 / 1024),
      js_heap_total_mb: Math.round(metrics.JSHeapTotalSize / 1024 / 1024),
      nodes: metrics.Nodes,
      documents: metrics.Documents,
      js_event_listeners: metrics.JSEventListeners,
    } : null,
    wa_collection_counts: collections,
  };
}

class MemoryDiagnostics {
  constructor({
    intervalSeconds,
    isReady,
    rootPid,
    page,
    dbWriteable,
    status,
    logger = console,
    collectChromium = chromiumProcessTree,
    collectPage = pageDiagnostics,
    now = () => Date.now(),
  }) {
    this.intervalSeconds = intervalSeconds;
    this.isReady = isReady;
    this.rootPid = rootPid;
    this.page = page;
    this.dbWriteable = dbWriteable;
    this.status = status;
    this.logger = logger;
    this.collectChromium = collectChromium;
    this.collectPage = collectPage;
    this.now = now;
    this.running = false;
    this.timer = null;
    this.previousProcessStats = new Map();
  }

  async collect() {
    if (!this.isReady() || this.running) return;
    const rootPid = this.rootPid();
    if (!rootPid) return;
    this.running = true;
    try {
      const chromium = this.collectChromium(rootPid, this.previousProcessStats);
      const sampleMs = this.now();
      this.previousProcessStats = new Map(
        chromium.processes.map((proc) => [
          proc.pid,
          {
            cpu_ticks: proc.cpu_ticks,
            sample_ms: sampleMs,
          },
        ]),
      );
      const statusProcesses = chromium.processes.map(({ cpu_ticks, ...proc }) => proc);
      const page = await this.collectPage(this.page());
      this.status.write({
        state: 'ready',
        wwebjs_ready: true,
        db_writeable: this.dbWriteable(),
        memory_diagnostics_at: Math.floor(sampleMs / 1000),
        chromium_root_pid: rootPid,
        chromium_total_rss_mb: chromium.total_rss_mb,
        chromium_process_count: chromium.process_count,
        chromium_processes: statusProcesses,
        ...page,
      });
    } catch (error) {
      this.status.write({
        memory_diagnostics_error: error.message,
        memory_diagnostics_error_at: Math.floor(this.now() / 1000),
      });
    } finally {
      this.running = false;
    }
  }

  schedule() {
    if (this.intervalSeconds <= 0 || this.timer) return;
    this.collect().catch((error) => {
      this.logger.warn('memory diagnostics failed:', error.message);
    });
    this.timer = setInterval(() => {
      this.collect().catch((error) => {
        this.logger.warn('memory diagnostics failed:', error.message);
      });
    }, this.intervalSeconds * 1000);
    if (this.timer.unref) this.timer.unref();
  }

  stop() {
    if (!this.timer) return;
    clearInterval(this.timer);
    this.timer = null;
  }
}

module.exports = {
  MemoryDiagnostics,
  chromiumProcessTree,
  memoryStatsMb,
  pageDiagnostics,
};
