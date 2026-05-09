// agent-me slack-bridge — Socket Mode entrypoint.
//
// What this file owns:
//   - .env loading from ${AGENT_ME_REPO_DIR}/configs/.env
//   - Bolt App in Socket Mode
//   - SQLite state DB initialization (threads, messages, pending_approvals)
//   - Slack event/action handler registration (stubs for P1)
//   - Graceful shutdown
//
// What this file does NOT yet do (P2 milestones):
//   - Spawning `claude -p` and streaming its output -> see spawnClaude()
//   - Posting / updating Slack messages with progress -> see postThinking() / updateProgress()
//   - Block Kit approval prompts and resolution -> see requestApproval()
//
// Architecture spec: ../../design/slack-app-setup.md §8
// Decisions:         ../../discussions/2026-05-10-slack-decisions.md

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

// Resolution order for the repo dir (used as cwd for `claude -p` and as the
// anchor for finding configs/.env):
//   1. process.env.AGENT_ME_REPO_DIR if already set in the shell
//   2. Walk up from this file (services/slack-bridge -> agent-me)
//   3. Fall back to the documented default /home/agent/agent-me
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
  // Fall back to default search so dotenv can still pick up shell-exported
  // variables or a sibling .env if someone really wants one.
  dotenv.config();
}

// Re-export the resolved repo dir so child processes inherit it.
process.env.AGENT_ME_REPO_DIR = repoDir;

// ---------------------------------------------------------------------------
// 2. Logger
// ---------------------------------------------------------------------------

const log = pino({
  level: process.env.LOG_LEVEL ?? 'info',
  base: { service: 'slack-bridge' },
  // pino-pretty is a devDependency; only attach the transport when we can
  // actually resolve it, otherwise emit JSON (right thing in production).
  transport:
    process.env.NODE_ENV !== 'production' && canRequire('pino-pretty')
      ? { target: 'pino-pretty', options: { colorize: true, translateTime: 'SYS:HH:MM:ss' } }
      : undefined,
});

function canRequire(mod) {
  try {
    // import.meta.resolve is sync since node 20.
    return Boolean(import.meta.resolve(mod));
  } catch {
    return false;
  }
}

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

// ---------------------------------------------------------------------------
// 5. Bolt App
// ---------------------------------------------------------------------------

const app = new App({
  token: process.env.SLACK_BOT_TOKEN,
  appToken: process.env.SLACK_APP_TOKEN,
  signingSecret: process.env.SLACK_SIGNING_SECRET,
  socketMode: true,
  logLevel: process.env.LOG_LEVEL === 'debug' ? LogLevel.DEBUG : LogLevel.INFO,
});

// ---------------------------------------------------------------------------
// 6. Helper stubs (P2 work)
// ---------------------------------------------------------------------------

/**
 * Spawn `claude -p` with cwd = AGENT_ME_REPO_DIR so the agent inherits CLAUDE.md.
 *
 * @param {{ prompt: string, threadTs: string, channel: string }} args
 * @returns {AsyncIterable<{ type: string, [k: string]: unknown }>} stream of events
 *          (message_delta, tool_use_request, tool_result, final, error, ...).
 */
// TODO(P2): implement child_process.spawn with --output-format stream-json,
// parse NDJSON line-by-line, yield typed events. On tool_use_request, await
// requestApproval() before letting the child continue. Buffer stdout/stderr
// and surface errors with non-zero exit code.
// eslint-disable-next-line require-yield
async function* spawnClaude({ prompt, threadTs, channel }) {
  log.info({ event: 'spawn_claude_called', threadTs, channel, promptLen: prompt?.length }, 'stub');
  throw new Error('spawnClaude: not implemented (P2)');
}

/**
 * Post the initial 🔄 thinking placeholder into the thread and return its ts.
 *
 * @param {string} channel
 * @param {string} threadTs
 * @returns {Promise<string>} message ts of the placeholder, used by updateProgress.
 */
// TODO(P2): chat.postMessage with thread_ts; return result.ts.
async function postThinking(channel, threadTs) {
  log.info({ event: 'post_thinking_called', channel, threadTs }, 'stub');
  throw new Error('postThinking: not implemented (P2)');
}

/**
 * Update an in-flight message via chat.update. Used both for ~30s progress
 * pulses and for the final answer swap-in.
 *
 * @param {string} channel
 * @param {string} ts
 * @param {string} text
 * @returns {Promise<void>}
 */
// TODO(P2): chat.update with text + Block Kit blocks; throttle to <1 update/sec.
async function updateProgress(channel, ts, text) {
  log.debug({ event: 'update_progress_called', channel, ts, textLen: text?.length }, 'stub');
  throw new Error('updateProgress: not implemented (P2)');
}

/**
 * Post a Block Kit approval prompt with three buttons:
 *   - Approve (action_id=approve_action)
 *   - Approve all in thread (action_id=approve_all_in_thread)
 *   - Reject (action_id=disable_auto_approve doubles as reject for now)
 *
 * Inserts a pending_approvals row, returns a Promise that resolves once one
 * of the action handlers below resolves it.
 *
 * @param {{ channel: string, threadTs: string, action: string, payload: unknown }} args
 * @returns {Promise<{ approved: boolean, autoApprove: boolean }>}
 */
// TODO(P2): generate approval id (uuid), insert pending_approvals row, post
// Block Kit message capturing slack_message_ts, register a Promise resolver
// keyed on approval id in an in-memory Map, action handlers below look up the
// resolver and call it. On process restart, reload pending rows and mark
// 'expired'.
async function requestApproval({ channel, threadTs, action, payload }) {
  log.info({ event: 'request_approval_called', channel, threadTs, action }, 'stub');
  throw new Error('requestApproval: not implemented (P2)');
}

// ---------------------------------------------------------------------------
// 7. Event handlers
// ---------------------------------------------------------------------------

// DMs only — channel_type === 'im'. Bot ignores its own messages by default.
app.event('message', async ({ event, client, logger }) => {
  // Bolt's typing has `subtype` only on system messages; skip those.
  if (event.subtype) return;
  if (event.channel_type !== 'im') return;

  // Optional single-user lockdown.
  if (process.env.SLACK_ALLOWED_USER_ID && event.user !== process.env.SLACK_ALLOWED_USER_ID) {
    log.warn({ event: 'message_rejected_user', from: event.user }, 'unauthorized user');
    return;
  }

  const threadTs = event.thread_ts ?? event.ts;
  log.info(
    { event: 'message_received', channel: event.channel, threadTs, user: event.user },
    'DM received',
  );

  // TODO(P2): the actual flow:
  //   1. upsert thread row, append user message row
  //   2. const placeholderTs = await postThinking(event.channel, threadTs);
  //   3. for await (const evt of spawnClaude({ prompt: event.text, threadTs, channel: event.channel })) {
  //        if (evt.type === 'tool_use_request') {
  //          const { approved } = await requestApproval({ channel, threadTs, action: evt.tool, payload: evt.input });
  //          if (!approved) { ...abort... }
  //        }
  //        if (evt.type === 'progress') await updateProgress(event.channel, placeholderTs, evt.text);
  //        if (evt.type === 'final') await updateProgress(event.channel, placeholderTs, evt.text);
  //      }
  //   4. append assistant message row.
});

app.event('app_mention', async ({ event, client, logger }) => {
  const threadTs = event.thread_ts ?? event.ts;
  log.info(
    { event: 'app_mention_received', channel: event.channel, threadTs, user: event.user },
    'mention received',
  );
  // TODO(P2): same pipeline as DM handler above. Strip the leading <@BOT_ID>
  // mention from event.text before passing to spawnClaude().
});

// ---------------------------------------------------------------------------
// 8. Action handlers (Block Kit buttons on approval prompts)
// ---------------------------------------------------------------------------

app.action('approve_action', async ({ ack, body, client }) => {
  await ack();
  log.info({ event: 'approve_action', user: body.user?.id }, 'approve clicked');
  // TODO(P2): look up approval id in body.actions[0].value, mark approved in
  // pending_approvals, resolve the in-memory Promise, chat.update the prompt
  // with a "✅ Approved by <user>" footer.
});

app.action('approve_all_in_thread', async ({ ack, body, client }) => {
  await ack();
  log.info({ event: 'approve_all_in_thread', user: body.user?.id }, 'auto-approve enabled');
  // TODO(P2): UPDATE threads SET auto_approve = 1 WHERE thread_ts = ?, then
  // resolve current approval the same way as approve_action.
});

app.action('disable_auto_approve', async ({ ack, body, client }) => {
  await ack();
  log.info({ event: 'disable_auto_approve', user: body.user?.id }, 'auto-approve disabled');
  // TODO(P2): UPDATE threads SET auto_approve = 0 WHERE thread_ts = ?. Also
  // serves as Reject for an in-flight prompt for now.
});

// ---------------------------------------------------------------------------
// 9. Boot + graceful shutdown
// ---------------------------------------------------------------------------

await app.start();
log.info('agent-me slack bridge: running on Socket Mode');

let shuttingDown = false;
async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  log.info({ signal }, 'shutdown initiated');
  try {
    await app.stop();
  } catch (err) {
    log.error({ err }, 'error stopping bolt app');
  }
  try {
    db.close();
  } catch (err) {
    log.error({ err }, 'error closing sqlite');
  }
  process.exit(0);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('uncaughtException', (err) => {
  log.fatal({ err }, 'uncaughtException');
  shutdown('uncaughtException');
});
process.on('unhandledRejection', (reason) => {
  log.fatal({ reason }, 'unhandledRejection');
  shutdown('unhandledRejection');
});
