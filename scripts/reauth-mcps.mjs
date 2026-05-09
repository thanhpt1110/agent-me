#!/usr/bin/env node
//
// agent-me — MCP re-auth helper (full auto-open)
//
// Detects every MCP server flagged "! Needs authentication" by `claude mcp
// list`, spawns a persistent `claude` REPL via piped stdin (so the local
// OAuth callback listeners stay alive), instructs it to call the
// `mcp__<server>__authenticate` tool for each stale server, parses each
// printed auth URL out of stdout, and `open`s them all in your default
// browser. You sign in to NVIDIA SSO in each tab; redirects come back to
// the still-alive REPL on localhost; tokens land in `~/.claude.json`.
//
// When you're done in the browser, press Ctrl-C here. The helper sends
// `/exit` to claude and shuts down cleanly.
//
// Usage:  ~/agent-me/scripts/reauth-mcps.mjs

import { spawn, execSync } from 'node:child_process';
import process from 'node:process';

// ── 1. Detect stale servers ───────────────────────────────────────────────

let listOut;
try {
  listOut = execSync('claude mcp list 2>&1', { encoding: 'utf8' });
} catch (err) {
  console.error('ERROR: `claude mcp list` failed:', err.message);
  process.exit(2);
}

const stale = listOut
  .split('\n')
  .filter((l) => l.includes('Needs authentication'))
  .map((l) => l.split(':')[0].trim())
  .filter(Boolean);

if (stale.length === 0) {
  console.log('✓ All MCP servers authenticated. Nothing to do.');
  process.exit(0);
}

console.log('================================================================');
console.log('  agent-me — MCP re-auth helper (auto-open)');
console.log('================================================================\n');
console.log(`Detected ${stale.length} stale server(s):`);
stale.forEach((s) => console.log(`  - ${s}`));
console.log('');
console.log('Spawning persistent `claude` REPL so the OAuth callback');
console.log('listeners can capture each browser redirect. Keep this');
console.log('terminal open until all browser tabs finish authorizing.');
console.log('');
console.log('Press Ctrl-C when done — helper sends /exit to claude cleanly.\n');

// ── 2. Spawn persistent claude REPL ───────────────────────────────────────

const claude = spawn('claude', [], {
  stdio: ['pipe', 'pipe', 'inherit'],
  cwd: process.env.AGENT_ME_REPO_DIR || `${process.env.HOME}/agent-me`,
});

// stdin stays open until we explicitly write /exit on Ctrl-C.
claude.stdin.setDefaultEncoding('utf8');

// ── 3. After a short boot delay, send the multi-authenticate prompt ──────

const BOOT_DELAY_MS = 3000;
const prompt = [
  'Call the authenticate tool for EACH of these MCP servers, one at a time.',
  'For each, print exactly what the tool returns (it will include an',
  '`https://...nvidia.com/...` URL on its own line). Do NOT summarize, do',
  'NOT skip any, do NOT add commentary between calls. Just call each tool',
  'and let its output through verbatim.',
  '',
  'Servers (in order):',
  ...stale.map((s) => `- mcp__${s}__authenticate`),
].join('\n');

setTimeout(() => {
  claude.stdin.write(prompt + '\n');
}, BOOT_DELAY_MS);

// ── 4. Watch stdout, extract NVIDIA auth URLs, auto-open ──────────────────

let buffer = '';
const opened = new Set();

// Strip ANSI escape sequences before matching URLs.
function stripAnsi(s) {
  return s.replace(/\[[0-9;]*[a-zA-Z]/g, '');
}

claude.stdout.on('data', (chunk) => {
  const text = chunk.toString();
  process.stdout.write(text); // mirror to user
  buffer += stripAnsi(text);

  const re = /https:\/\/[^\s)\]"']+nvidia\.com[^\s)\]"']*/g;
  let m;
  while ((m = re.exec(buffer)) !== null) {
    const url = m[0];
    if (opened.has(url)) continue;
    // Be conservative: only auto-open URLs that look like an OAuth authorize
    // endpoint, not docs / status pages.
    if (
      url.includes('authorize') ||
      url.includes('/oauth/') ||
      url.includes('response_type=code')
    ) {
      opened.add(url);
      console.log(`\n[helper] auto-opening auth URL #${opened.size}\n`);
      spawn('open', [url], { detached: true, stdio: 'ignore' }).unref();
    }
  }
});

// ── 5. Cleanup on Ctrl-C ──────────────────────────────────────────────────

let shuttingDown = false;
async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`\n[helper] ${signal} received — sending /exit to claude...`);
  try {
    claude.stdin.write('/exit\n');
    claude.stdin.end();
  } catch {
    // ignore — child may already be gone
  }
  setTimeout(() => claude.kill('SIGTERM'), 4000);
  setTimeout(() => process.exit(0), 6000);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

claude.on('close', (code) => {
  console.log(
    `\n[helper] claude exited (code ${code}). Opened ${opened.size} auth URL(s).`,
  );
  console.log('[helper] Verify with:  claude mcp list');
  process.exit(code ?? 0);
});

claude.on('error', (err) => {
  console.error('[helper] claude failed to spawn:', err.message);
  process.exit(2);
});
