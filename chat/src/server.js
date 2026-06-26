// agent-chat — always-on web chat that drives persistent Claude Code sessions
// on this VM's workspace. Each chat = one resumable `claude` CLI session; turns
// run server-side (owned by this daemon), so they keep working after the browser
// disconnects. State is persisted to disk so a reconnecting client catches up.
import express from 'express';
import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';
import { runClaudeTurn } from './claude.js';
import { SessionStore } from './sessions.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const DATA_DIR = path.join(ROOT, 'data');
const PUBLIC_DIR = path.join(ROOT, 'public');
fs.mkdirSync(DATA_DIR, { recursive: true });

// Reuse agent-me's shared config (configs/.env) so the chat can gate on the SAME
// secret as the dashboard buttons. Never overrides values already set in the env.
function loadEnvFile(p) {
  try {
    for (const line of fs.readFileSync(p, 'utf8').split('\n')) {
      if (line.trim().startsWith('#')) continue;
      const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
      if (!m) continue;
      const v = m[2].replace(/^["']|["']$/g, '');
      if (process.env[m[1]] === undefined) process.env[m[1]] = v;
    }
  } catch { /* no env file — fine */ }
}
loadEnvFile(path.join(ROOT, '..', 'configs', '.env')); // agent-me/configs/.env

const PORT = parseInt(process.env.PORT || '8787', 10);
const HOST = process.env.HOST || '0.0.0.0';
const DEPLOY_ENV = process.env.DEPLOY_ENV || 'dev';
const PROD_NOTE = process.env.PROD_NOTE ||
  'DEV — agent-me chat (migration target). Production agent-me still runs on the A100 (dashboard :8765); this is for testing before cut-over.';
const WORKSPACE = process.env.WORKSPACE || path.dirname(ROOT); // default: the agent-me repo root
const ADD_DIRS = (process.env.ADD_DIRS || '/home/thaphan').split(',').map((s) => s.trim()).filter(Boolean);

// ---- access password (Thanh-Phan-only gate; same secret as the dashboard buttons) ----
// Priority: CHAT_PASSWORD > DASHBOARD_OPERATOR_ACTION_CODE > DASHBOARD_TOKEN >
// ACCESS_TOKEN > a generated token persisted to data/access-token.txt.
const TOKEN_FILE = path.join(DATA_DIR, 'access-token.txt');
let ACCESS_TOKEN = (process.env.CHAT_PASSWORD || process.env.DASHBOARD_OPERATOR_ACTION_CODE
  || process.env.DASHBOARD_TOKEN || process.env.ACCESS_TOKEN || '').trim();
const TOKEN_SOURCE = process.env.CHAT_PASSWORD ? 'CHAT_PASSWORD'
  : process.env.DASHBOARD_OPERATOR_ACTION_CODE ? 'DASHBOARD_OPERATOR_ACTION_CODE'
  : process.env.DASHBOARD_TOKEN ? 'DASHBOARD_TOKEN'
  : process.env.ACCESS_TOKEN ? 'ACCESS_TOKEN' : 'generated';
if (!ACCESS_TOKEN) {
  if (fs.existsSync(TOKEN_FILE)) ACCESS_TOKEN = fs.readFileSync(TOKEN_FILE, 'utf8').trim();
  if (!ACCESS_TOKEN) {
    ACCESS_TOKEN = crypto.randomBytes(18).toString('base64url');
    fs.writeFileSync(TOKEN_FILE, ACCESS_TOKEN, { mode: 0o600 });
  }
}
const COOKIE = 'act';
const tokenOk = (tok) => typeof tok === 'string' && tok.length === ACCESS_TOKEN.length &&
  crypto.timingSafeEqual(Buffer.from(tok), Buffer.from(ACCESS_TOKEN));

const parseCookies = (header = '') => Object.fromEntries(
  header.split(';').map((c) => c.trim().split('=')).filter((p) => p[0]).map(([k, ...v]) => [k, decodeURIComponent(v.join('='))]),
);
const authed = (req) => tokenOk(parseCookies(req.headers.cookie || '')[COOKIE]);

const store = new SessionStore(DATA_DIR);
const app = express();
app.use(express.json({ limit: '2mb' }));

// ---- auth endpoints ----
app.post('/api/login', (req, res) => {
  if (!tokenOk((req.body || {}).token)) return res.status(401).json({ ok: false });
  res.setHeader('Set-Cookie', `${COOKIE}=${encodeURIComponent(ACCESS_TOKEN)}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${60 * 60 * 24 * 30}`);
  res.json({ ok: true });
});
app.get('/api/me', (req, res) => res.json({ authed: authed(req), workspace: WORKSPACE, env: DEPLOY_ENV, note: PROD_NOTE }));

// gate the API (everything below)
app.use('/api', (req, res, next) => (authed(req) ? next() : res.status(401).json({ error: 'unauthorized' })));

// ---- session REST ----
app.get('/api/sessions', (_req, res) => res.json({ sessions: store.list() }));
app.post('/api/sessions', (req, res) => {
  const { title, cwd, model } = req.body || {};
  const meta = store.create({ title, cwd: cwd || WORKSPACE, model });
  broadcastSessions();
  res.json(meta);
});
app.get('/api/sessions/:id', (req, res) => {
  const meta = store.getMeta(req.params.id);
  if (!meta) return res.status(404).json({ error: 'not found' });
  res.json({ meta, transcript: store.transcript(req.params.id) });
});
app.delete('/api/sessions/:id', (req, res) => {
  if (!store.exists(req.params.id)) return res.status(404).json({ error: 'not found' });
  store.remove(req.params.id);
  broadcastSessions();
  res.json({ ok: true });
});
app.post('/api/sessions/:id/message', (req, res) => {
  const id = req.params.id;
  const text = ((req.body || {}).text || '').toString();
  if (!store.exists(id)) return res.status(404).json({ error: 'not found' });
  if (!text.trim()) return res.status(400).json({ error: 'empty message' });
  if (store.isBusy(id)) return res.status(409).json({ error: 'a turn is already running' });
  runTurn(id, text).catch((e) => console.error('runTurn fatal', e));
  res.status(202).json({ ok: true });
});

app.use(express.static(PUBLIC_DIR));

// ---- websocket: broadcast events to all authed clients ----
const server = http.createServer(app);
const wss = new WebSocketServer({ noServer: true });
const clients = new Set();
server.on('upgrade', (req, socket, head) => {
  if (req.url.split('?')[0] !== '/ws' || !authed(req)) { socket.destroy(); return; }
  wss.handleUpgrade(req, socket, head, (ws) => {
    clients.add(ws);
    ws.on('close', () => clients.delete(ws));
    ws.on('error', () => clients.delete(ws));
    ws.send(JSON.stringify({ type: 'sessions', sessions: store.list() }));
  });
});
function send(obj) {
  const s = JSON.stringify(obj);
  for (const ws of clients) { if (ws.readyState === 1) { try { ws.send(s); } catch { /* drop */ } } }
}
const broadcast = (sessionId, event) => send({ type: 'event', sessionId, event });
const broadcastSessions = () => send({ type: 'sessions', sessions: store.list() });

// ---- run one turn (server-owned; survives client disconnect) ----
async function runTurn(id, text) {
  const meta = store.getMeta(id);
  store.setBusy(id, true);
  broadcastSessions();
  broadcast(id, store.appendEvent(id, { kind: 'user', text }));
  if ((!meta.title || meta.title === 'New chat') && (meta.turns || 0) === 0) {
    store.updateMeta(id, { title: text.replace(/\s+/g, ' ').trim().slice(0, 60) || 'Chat' });
  }
  let capturedSid = meta.claudeSessionId || null;
  try {
    const out = await runClaudeTurn({
      prompt: text,
      cwd: meta.cwd,
      sessionId: meta.claudeSessionId || undefined,
      resume: !!meta.claudeSessionId,
      model: meta.model || undefined,
      addDirs: ADD_DIRS,
      onEvent: (evt) => {
        if (evt.kind === 'init' && evt.sessionId) capturedSid = evt.sessionId;
        broadcast(id, store.appendEvent(id, evt));
      },
    });
    if (out.sessionId) capturedSid = out.sessionId;
    store.updateMeta(id, { claudeSessionId: capturedSid, turns: (meta.turns || 0) + 1 });
  } catch (err) {
    broadcast(id, store.appendEvent(id, { kind: 'error', text: `Failed to run claude: ${err?.message || err}` }));
  } finally {
    store.setBusy(id, false);
    broadcast(id, store.appendEvent(id, { kind: 'turn_end' }));
    broadcastSessions();
  }
}

server.listen(PORT, HOST, () => {
  console.log(`agent-chat [${DEPLOY_ENV}] listening on http://${HOST}:${PORT}`);
  console.log(`workspace default: ${WORKSPACE}  | extra dirs: ${ADD_DIRS.join(', ')}`);
  console.log(`access password source: ${TOKEN_SOURCE}`);
  if (TOKEN_SOURCE === 'generated') console.log(`generated access token: ${ACCESS_TOKEN}`); // only log a secret we created
});
