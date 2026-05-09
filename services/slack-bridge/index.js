// agent-me slack-bridge — Socket Mode entrypoint.
//
// What this file owns:
//   - .env loading from ${AGENT_ME_REPO_DIR}/configs/.env
//   - Bolt App in Socket Mode
//   - SQLite state DB initialization (threads, messages, pending_approvals)
//   - Slack event/action handler registration
//   - Spawning `claude -p` headless with a read-only tool allowlist (Phase 2a)
//   - Posting/updating Slack messages with hybrid streaming (placeholder → final)
//   - Graceful shutdown
//
// Phase 2b (deferred):
//   - PreToolUse hook + file-system semaphore for review-by-default approval
//   - requestApproval() Block Kit prompt + button-click resolution
//   - Token-by-token chat.update progress (currently we only update once at end)
//
// Architecture spec: ../../design/slack-app-setup.md §8
// Decisions:         ../../discussions/2026-05-10-slack-decisions.md
// Approval design:   ../../design/approval-hook-design.md

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname, isAbsolute, resolve } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';

import boltPkg from '@slack/bolt';
import Database from 'better-sqlite3';
import dotenv from 'dotenv';
import pino from 'pino';

const { App, LogLevel } = boltPkg;

const __dirname = dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------------------
// 1. Resolve AGENT_ME_REPO_DIR and load .env
// ---------------------------------------------------------------------------

function resolveRepoDir() {
  const fromEnv = process.env.AGENT_ME_REPO_DIR;
  if (fromEnv) {
    return isAbsolute(fromEnv) ? fromEnv : resolve(process.cwd(), fromEnv);
  }
  const guessed = resolve(__dirname, '..', '..');
  if (existsSync(resolve(guessed, 'CLAUDE.md'))) return guessed;
  return '/home/agent/agent-me';
}

const repoDir = resolveRepoDir();
const envPath = resolve(repoDir, 'configs', '.env');

if (existsSync(envPath)) {
  dotenv.config({ path: envPath });
} else {
  dotenv.config();
}

process.env.AGENT_ME_REPO_DIR = repoDir;

// ---------------------------------------------------------------------------
// 2. Logger
// ---------------------------------------------------------------------------

function canRequire(mod) {
  try {
    return Boolean(import.meta.resolve(mod));
  } catch {
    return false;
  }
}

const log = pino({
  level: process.env.LOG_LEVEL ?? 'info',
  base: { service: 'slack-bridge' },
  transport:
    process.env.NODE_ENV !== 'production' && canRequire('pino-pretty')
      ? { target: 'pino-pretty', options: { colorize: true, translateTime: 'SYS:HH:MM:ss' } }
      : undefined,
});

// ---------------------------------------------------------------------------
// 3. Validate required env
// ---------------------------------------------------------------------------

const REQUIRED_ENV = ['SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'SLACK_SIGNING_SECRET'];
const missing = REQUIRED_ENV.filter((k) => !process.env[k] || process.env[k].includes('REPLACE-ME'));
if (missing.length > 0) {
  log.fatal({ missing, envPath }, 'missing required environment variables; populate configs/.env');
  process.exit(1);
}

log.info({ repoDir, envPath: existsSync(envPath) ? envPath : null }, 'env loaded');

// ---------------------------------------------------------------------------
// 4. Resolve state dir + open SQLite
// ---------------------------------------------------------------------------

function resolveStateDir() {
  if (process.env.AGENT_ME_STATE_DIR) return process.env.AGENT_ME_STATE_DIR;
  const xdg = process.env.XDG_STATE_HOME;
  if (xdg) return resolve(xdg, 'agent-me');
  return resolve(homedir(), '.local', 'state', 'agent-me');
}

const stateDir = resolveStateDir();
mkdirSync(stateDir, { recursive: true });
const dbPath = resolve(stateDir, 'state.db');

const db = new Database(dbPath);
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

const schemaPath = resolve(__dirname, 'db', 'schema.sql');
db.exec(readFileSync(schemaPath, 'utf8'));

log.info({ dbPath }, 'state db ready');

// Prepared statements (hot path).
const upsertThread = db.prepare(`
  INSERT INTO threads (thread_ts, channel, user_id, auto_approve, created_at, last_active_at)
  VALUES (?, ?, ?, 0, ?, ?)
  ON CONFLICT(thread_ts) DO UPDATE SET last_active_at = excluded.last_active_at
`);
const insertMessage = db.prepare(`
  INSERT INTO messages (thread_ts, role, content, slack_ts, created_at)
  VALUES (?, ?, ?, ?, ?)
`);

// ---------------------------------------------------------------------------
// 5. Phase 2a: Claude tool permissions + model selection
// ---------------------------------------------------------------------------
//
// Headless `claude -p` denies every tool that requires permission unless we
// explicitly allow it. The project's `.claude/settings.json` bypassPermissions
// flag is NOT honored in headless mode for MCP tools — empirically verified.
// Solution: per-server wildcards on the allow list + specific writes on the
// deny list. (Top-level `mcp__*` wildcard is NOT supported; per-server is.)
// Phase 2b will replace this hard split with a PreToolUse hook that posts a
// Slack approval prompt instead of denying outright.

const PHASE_2A_ALLOWED_TOOLS = [
  // Built-in read tools
  'Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch',
  // Per-server MCP wildcards — read tools come for free; we deny writes below.
  'mcp__maas-confluence__*',
  'mcp__maas-gdrive__*',
  'mcp__maas-gitlab__*',
  'mcp__maas-glean__*',
  'mcp__maas-ippsec__*',
  'mcp__maas-jama__*',
  'mcp__maas-jira__*',
  'mcp__maas-mysql__*',
  'mcp__maas-nsight-cuda__*',
  'mcp__maas-nvbugs__*',
  'mcp__maas-onedrive__*',
  'mcp__maas-sharepoint__*',
  // Skipped on purpose:
  //   - mcp__maas-playwright__* (browser automation, not useful in chat)
  //   - mcp__maas-pagerduty__* and mcp__maas-nvks-prometheus__* (need separate auth)
];

const PHASE_2A_DISALLOWED_TOOLS = [
  // Built-in writers / shell
  'Bash', 'Write', 'Edit', 'NotebookEdit',
  // MCP tools that mutate remote state (curated; expand as new MCPs are added).
  // Deny list takes precedence over the per-server wildcard above.
  'mcp__maas-jira__jira_create_issue',
  'mcp__maas-jira__jira_clone_issue',
  'mcp__maas-jira__jira_update_issue',
  'mcp__maas-jira__jira_transition_issue',
  'mcp__maas-nvbugs__nvbugs_update_bug_v2',
  'mcp__maas-nvbugs__nvbugs_update_bug',
  'mcp__maas-ippsec__register_repo',
  'mcp__maas-mysql__execute_sql', // read-shaped name but accepts arbitrary SQL incl. writes
  // GitLab "AI prompt" tools trigger remote AI runs against the repo — treat as write.
  'mcp__maas-gitlab__gitlab_coderabbit_ai_prompt',
  'mcp__maas-gitlab__gitlab_greptile_ai_suggestions',
];

const CLAUDE_TIMEOUT_MS = Number(process.env.CLAUDE_TIMEOUT_MS ?? 5 * 60 * 1000);
const MAX_SLACK_TEXT = 39000;     // Slack hard limit is 40k; leave headroom for footers.
const MAX_LOG_TEXT = 4000;        // Cap how much message text we drop into structured logs.

// ---------------------------------------------------------------------------
// 5b. Daily auto-update of "best Claude model"
// ---------------------------------------------------------------------------
//
// On startup + every 24h, fetch /v1/models from Anthropic if ANTHROPIC_API_KEY
// is set, pick the newest Opus by created_at, cache it. spawnClaude() reads
// `currentModel` and passes it via --model. If the API call fails or no key
// is set, we fall back to FALLBACK_MODEL.

const FALLBACK_MODEL = process.env.CLAUDE_MODEL || 'claude-opus-4-7';
let currentModel = FALLBACK_MODEL;

async function fetchBestModel() {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) return null;
  try {
    const res = await fetch('https://api.anthropic.com/v1/models?limit=100', {
      headers: { 'x-api-key': apiKey, 'anthropic-version': '2023-06-01' },
    });
    if (!res.ok) {
      log.warn({ status: res.status }, 'Anthropic /v1/models returned non-OK');
      return null;
    }
    const body = await res.json();
    const opus = (body.data ?? [])
      .filter((m) => m.id?.includes('opus') && m.type === 'model')
      .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')));
    return opus[0]?.id ?? null;
  } catch (err) {
    log.warn({ err: err.message }, 'failed to fetch latest model');
    return null;
  }
}

async function refreshModel() {
  const latest = await fetchBestModel();
  if (latest && latest !== currentModel) {
    log.info({ event: 'model_updated', from: currentModel, to: latest }, 'switched to newer Opus');
    currentModel = latest;
  } else if (!latest) {
    log.debug({ currentModel }, 'model refresh skipped (no API key or fetch failed)');
  }
}

// ---------------------------------------------------------------------------
// 6. Bolt App
// ---------------------------------------------------------------------------

const app = new App({
  token: process.env.SLACK_BOT_TOKEN,
  appToken: process.env.SLACK_APP_TOKEN,
  signingSecret: process.env.SLACK_SIGNING_SECRET,
  socketMode: true,
  logLevel: process.env.LOG_LEVEL === 'debug' ? LogLevel.DEBUG : LogLevel.INFO,
});

// ---------------------------------------------------------------------------
// 7. Helpers
// ---------------------------------------------------------------------------

function truncateForSlack(text) {
  if (!text) return '_(no output)_';
  if (text.length <= MAX_SLACK_TEXT) return text;
  return `${text.slice(0, MAX_SLACK_TEXT - 120)}\n\n_…[truncated; ${text.length - MAX_SLACK_TEXT} chars cut]_`;
}

/**
 * Spawn `claude -p` with cwd = AGENT_ME_REPO_DIR so it inherits CLAUDE.md +
 * the project's MCP config + auto-memory. Phase 2a runs once-and-done: we
 * wait for the process to exit and resolve with the full stdout. Phase 2b
 * will replace this with `--output-format stream-json` parsing for live
 * progress + per-tool approval.
 */
function spawnClaude({ prompt, cwd }) {
  return new Promise((resolvePromise, rejectPromise) => {
    const args = [
      '-p', prompt,
      '--model', currentModel,
      '--allowedTools', PHASE_2A_ALLOWED_TOOLS.join(' '),
      '--disallowedTools', PHASE_2A_DISALLOWED_TOOLS.join(' '),
    ];
    log.info(
      { event: 'claude_spawn', cwd, model: currentModel, promptLen: prompt.length },
      'spawning claude',
    );

    const child = spawn('claude', args, {
      cwd,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: process.env,
    });

    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d.toString(); });
    child.stderr.on('data', (d) => { stderr += d.toString(); });

    const timer = setTimeout(() => {
      log.warn({ event: 'claude_timeout', ms: CLAUDE_TIMEOUT_MS }, 'killing claude');
      child.kill('SIGTERM');
      // Give it 5s to die gracefully before SIGKILL.
      setTimeout(() => child.kill('SIGKILL'), 5_000);
    }, CLAUDE_TIMEOUT_MS);

    child.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) {
        resolvePromise(stdout.trim());
      } else {
        rejectPromise(new Error(`claude exited code ${code}: ${stderr.trim().slice(0, 500)}`));
      }
    });
    child.on('error', (err) => {
      clearTimeout(timer);
      rejectPromise(err);
    });
  });
}

async function postThinking(client, channel, threadTs) {
  const result = await client.chat.postMessage({
    channel,
    thread_ts: threadTs,
    text: '🔄 thinking…',
  });
  return result.ts;
}

async function updateProgress(client, channel, ts, text) {
  await client.chat.update({
    channel,
    ts,
    text: truncateForSlack(text),
  });
}

/**
 * Phase 2b stub. Will post a Block Kit approval prompt and resolve when the
 * user clicks Approve / Approve all in thread / Reject. For Phase 2a, the
 * hard `--disallowedTools` list short-circuits write attempts before Claude
 * ever asks; this function is unreachable today.
 */
async function requestApproval({ channel, threadTs, action, payload }) {
  log.info({ event: 'request_approval_called', channel, threadTs, action }, 'P2b stub');
  throw new Error('requestApproval: not implemented (Phase 2b)');
}

/**
 * Shared pipeline used by both DM and app_mention handlers.
 */
function clip(s, n = MAX_LOG_TEXT) {
  if (!s) return s;
  return s.length <= n ? s : `${s.slice(0, n)}…[+${s.length - n} chars]`;
}

async function handleUserQuery({ client, channel, threadTs, userId, text, eventTs }) {
  if (!text || text.trim().length === 0) {
    log.debug({ threadTs }, 'empty text; skipping');
    return;
  }

  log.info(
    { event: 'message_received', threadTs, channel, user: userId, prompt: clip(text) },
    'received',
  );

  const now = Date.now();
  upsertThread.run(threadTs, channel, userId ?? null, now, now);
  insertMessage.run(threadTs, 'user', text, eventTs ?? null, now);

  let placeholderTs;
  try {
    placeholderTs = await postThinking(client, channel, threadTs);
  } catch (err) {
    log.error({ err: err.message, threadTs }, 'failed to post thinking placeholder');
    return;
  }

  const start = Date.now();
  try {
    const answer = await spawnClaude({ prompt: text, cwd: repoDir });
    const final = answer && answer.trim().length > 0 ? answer : '_(no output)_';
    await updateProgress(client, channel, placeholderTs, final);
    insertMessage.run(threadTs, 'assistant', final, placeholderTs, Date.now());
    log.info(
      {
        event: 'query_handled',
        threadTs,
        ms: Date.now() - start,
        model: currentModel,
        prompt: clip(text),
        response: clip(final),
      },
      'ok',
    );
  } catch (err) {
    log.error(
      {
        event: 'query_failed',
        threadTs,
        ms: Date.now() - start,
        prompt: clip(text),
        err: err.message,
      },
      'claude failed',
    );
    await updateProgress(
      client,
      channel,
      placeholderTs,
      `⚠️ Error: \`${err.message.slice(0, 600)}\``,
    ).catch((e) => log.error({ err: e.message }, 'failed to post error message'));
  }
}

// ---------------------------------------------------------------------------
// 8. Event handlers
// ---------------------------------------------------------------------------

app.event('message', async ({ event, client }) => {
  if (event.subtype) return;                        // bot/system messages
  if (event.channel_type !== 'im') return;          // DMs only
  if (event.bot_id) return;                         // ignore other bots / self

  if (process.env.SLACK_ALLOWED_USER_ID && event.user !== process.env.SLACK_ALLOWED_USER_ID) {
    log.warn({ event: 'message_rejected_user', from: event.user }, 'unauthorized user');
    return;
  }

  const threadTs = event.thread_ts ?? event.ts;
  await handleUserQuery({
    client,
    channel: event.channel,
    threadTs,
    userId: event.user,
    text: event.text,
    eventTs: event.ts,
  });
});

app.event('app_mention', async ({ event, client }) => {
  const cleaned = (event.text || '').replace(/^<@[A-Z0-9]+>\s*/, '').trim();
  const threadTs = event.thread_ts ?? event.ts;
  await handleUserQuery({
    client,
    channel: event.channel,
    threadTs,
    userId: event.user,
    text: cleaned,
    eventTs: event.ts,
  });
});

// ---------------------------------------------------------------------------
// 9. Action handlers (Phase 2b — currently no-op acks)
// ---------------------------------------------------------------------------

app.action('approve_action', async ({ ack, body }) => {
  await ack();
  log.info({ event: 'approve_action', user: body.user?.id }, 'approve clicked (P2b)');
});

app.action('approve_all_in_thread', async ({ ack, body }) => {
  await ack();
  log.info({ event: 'approve_all_in_thread', user: body.user?.id }, 'auto-approve enabled (P2b)');
});

app.action('disable_auto_approve', async ({ ack, body }) => {
  await ack();
  log.info({ event: 'disable_auto_approve', user: body.user?.id }, 'auto-approve disabled (P2b)');
});

// ---------------------------------------------------------------------------
// 10. Boot + graceful shutdown
// ---------------------------------------------------------------------------

// Initial model refresh (non-blocking) + daily refresh.
refreshModel().catch((err) => log.warn({ err: err.message }, 'initial model refresh failed'));
const MODEL_REFRESH_INTERVAL_MS = 24 * 60 * 60 * 1000;
const modelRefreshTimer = setInterval(() => {
  refreshModel().catch((err) => log.warn({ err: err.message }, 'periodic model refresh failed'));
}, MODEL_REFRESH_INTERVAL_MS);
modelRefreshTimer.unref(); // do not keep process alive on its own

await app.start();
log.info(
  { phase: '2a', initialModel: currentModel, hasApiKey: Boolean(process.env.ANTHROPIC_API_KEY) },
  'agent-me slack bridge: running on Socket Mode (read-only mode)',
);

let shuttingDown = false;
async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  log.info({ signal }, 'shutdown initiated');
  try { await app.stop(); } catch (err) { log.error({ err: err.message }, 'error stopping bolt'); }
  try { db.close(); } catch (err) { log.error({ err: err.message }, 'error closing sqlite'); }
  process.exit(0);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('uncaughtException', (err) => {
  log.fatal({ err: err.message, stack: err.stack }, 'uncaughtException');
  shutdown('uncaughtException');
});
process.on('unhandledRejection', (reason) => {
  log.fatal({ reason: String(reason) }, 'unhandledRejection');
  shutdown('unhandledRejection');
});
