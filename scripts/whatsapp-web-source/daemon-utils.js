'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith('--')) continue;
    const key = arg.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) {
      out[key] = true;
    } else {
      out[key] = next;
      i += 1;
    }
  }
  return out;
}

function expandPath(value) {
  const input = String(value || '');
  if (input === '~') return os.homedir();
  if (input.startsWith('~/')) return path.join(os.homedir(), input.slice(2));
  return input.replace(/^\$HOME(?=\/|$)/, os.homedir());
}

function ensureDir(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function timestampLabel() {
  return new Date().toISOString().replace(/[-:]/g, '').replace(/\..*/, 'Z');
}

function defaultUserAgent() {
  if (process.env.HERMES_WWEBJS_USER_AGENT) return process.env.HERMES_WWEBJS_USER_AGENT;
  const executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
  if (!executablePath) return undefined;
  try {
    const version = execFileSync(executablePath, ['--version'], { encoding: 'utf8', timeout: 5000 }).trim();
    const match = version.match(/(?:Chromium|Chrome) (\d+)\./);
    if (match) {
      return `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${match[1]}.0.0.0 Safari/537.36`;
    }
  } catch (error) {
    return undefined;
  }
  return undefined;
}

module.exports = {
  defaultUserAgent,
  ensureDir,
  expandPath,
  parseArgs,
  timestampLabel,
};
