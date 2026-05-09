#!/usr/bin/env node
//
// agent-me — MCP re-auth helper (full auto-open)
//
// Spawns a persistent `claude` REPL via piped stdin (so the local OAuth
// callback listeners stay alive), instructs it to call the
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

const log = (...args) => console.log('[helper]', ...args);
const repoDir = process.env.AGENT_ME_REPO_DIR || `${process.env.HOME}/agent-me`;

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
  log('All MCP servers authenticated. Nothing to do.');
  process.exit(0);
}

log(`detected ${stale.length} stale server(s):`);
stale.forEach((s) => console.log(`   - ${s}`));
console.log('');

// ── 2. Spawn persistent claude REPL with bypassPermissions ───────────────

log('spawning `claude --permission-mode bypassPermissions` (REPL)...');
const claude = spawn('claude', ['--permission-mode', 'bypassPermissions'], {
  stdio: ['pipe', 'pipe', 'inherit'],
  cwd: repoDir,
});

claude.stdin.setDefaultEncoding('utf8');

// Track URLs we've already opened so we don't double-fire.
const opened = new Set();
let buffer = '';

// Strip ANSI escapes (real escape char + bracket) before URL matching.
const ANSI_RE = /\x1b\[[0-9;]*[a-zA-Z]/g;
const URL_RE = /https:\/\/[^\s)\]"'`<>]+nvidia\.com[^\s)\]"'`<>]*/g;

claude.stdout.on('data', (chunk) => {
  const text = chunk.toString();
  process.stdout.write(text); // mirror to user
  buffer += text.replace(ANSI_RE, '');

  let m;
  while ((m = URL_RE.exec(buffer)) !== null) {
    const url = m[0];
    if (opened.has(url)) continue;
    if (
      url.includes('authorize') ||
      url.includes('/oauth/') ||
      url.includes('response_type=code')
    ) {
      opened.add(url);
      log(`>>> auto-opening URL #${opened.size} (${url.slice(0, 60)}...)`);
      spawn('open', [url], { detached: true, stdio: 'ignore' }).unref();
    }
  }
});

// ── 3. Send each authenticate call sequentially after boot delay ────────

const BOOT_DELAY_MS = 5000;
const PER_CALL_GAP_MS = 4000;

setTimeout(async () => {
  log(`sending ${stale.length} authenticate call(s) sequentially, ${PER_CALL_GAP_MS}ms apart...`);
  for (const server of stale) {
    const tool = `mcp__${server}__authenticate`;
    const line = `Call the tool ${tool} (no parameters). Print exactly what it returns; do not summarize.`;
    log(`  → asking claude to call ${tool}`);
    claude.stdin.write(line + '\n');
    await new Promise((r) => setTimeout(r, PER_CALL_GAP_MS));
  }
  log('all authenticate calls dispatched. Waiting for URLs to appear and browser tabs to open.');
  log('Sign in to NVIDIA SSO in each tab. When done, press Ctrl-C here.');
}, BOOT_DELAY_MS);

// ── 4. Cleanup on Ctrl-C ────────────────────────────────────────────────

let shuttingDown = false;
async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  log(`${signal} received — sending /exit to claude...`);
  try {
    claude.stdin.write('/exit\n');
    setTimeout(() => claude.stdin.end(), 1000);
  } catch {
    /* ignore */
  }
  setTimeout(() => claude.kill('SIGTERM'), 5000);
  setTimeout(() => process.exit(0), 7000);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

claude.on('close', (code) => {
  log(`claude exited (code ${code}). opened ${opened.size}/${stale.length} auth URL(s).`);
  log('verify with:  claude mcp list');
  process.exit(code ?? 0);
});

claude.on('error', (err) => {
  console.error('[helper] claude failed to spawn:', err.message);
  process.exit(2);
});
