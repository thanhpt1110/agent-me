# Slack App Setup for `agent-me`

This guide walks you (the forker of `agent-me`) through standing up a Slack app
that DMs your personal Claude agent and relays replies back into Slack. It is
written so anyone forking `agent-me` can follow the same steps; NVIDIA-internal
notes are called out explicitly so external forkers can ignore them.

> **Goal.** Send a DM (or `@mention` in a channel) to a Slack bot. The bot
> spawns a headless `claude -p` process inside `~/agent-me/` so the agent
> inherits the repo's `CLAUDE.md` context, and streams the answer back into the
> Slack thread.

> **Implementation note (2026-05-10):** this guide was originally drafted when
> the bridge was Node + `@slack/bolt`. The current bridge is Python +
> `slack-bolt-python` at `src/agent_me/slack_bridge/app.py`, run with
> `uv run agent-me-bridge`. The Slack-side setup in §1–§7 is implementation-
> agnostic and still applies verbatim. The architecture sketch in §8 and the
> code snippets in §10/§11 reference Node and are kept here as historical
> context; if you are forking, read the actual Python source for the
> authoritative shape.

---

## Table of Contents

1. [Decide which workspace to install into](#1-decide-which-workspace-to-install-into)
2. [Create the Slack app](#2-create-the-slack-app)
3. [Configure OAuth scopes](#3-configure-oauth-scopes)
4. [Enable Socket Mode (recommended)](#4-enable-socket-mode-recommended)
5. [Subscribe to events](#5-subscribe-to-events)
6. [Install the app and capture tokens](#6-install-the-app-and-capture-tokens)
7. [Store tokens safely](#7-store-tokens-safely)
8. [Architecture: the Slack ↔ Claude bridge](#8-architecture-the-slack--claude-bridge)
9. [Threading & conversation state strategy](#9-threading--conversation-state-strategy)
10. [Testing checklist](#10-testing-checklist)
11. [Security checklist](#11-security-checklist)
12. [Open questions / things to verify](#12-open-questions--things-to-verify)

---

## 1. Decide which workspace to install into

You generally have two choices:

### Option A — A personal / hobby workspace (recommended for most people)

Create a free Slack workspace at <https://slack.com/get-started> if you don't
already have one. You will be the workspace owner, so you can self-approve any
custom Slack app you build.

**Pros**

- You own the workspace, no admin approval gate.
- You can iterate on scopes freely.
- Keeps personal AI traffic off corporate logs / DLP / retention.
- No risk of corp policy change breaking the bot.

**Cons**

- Yet another Slack workspace to keep open.
- No native presence next to your work conversations.

### Option B — Your employer's Slack workspace

You can install a custom app into a corporate workspace **only if** the
workspace policy allows it. In nearly all enterprises this requires admin
approval per app, scoped to the permissions you request.

**Pros**

- Bot lives next to the channels you already DM in.
- Reuses your existing Slack identity.

**Cons**

- Admin approval gate (see notes below).
- Corp retention/DLP policies apply to every prompt and answer.
- Tokens belong to a workspace your employer can revoke at any time.
- If you ever leave the company, the bot dies.

### NVIDIA-specific notes (skip if you are not an NVIDIA employee)

Based on the internal "Integrate with Slack" page on the **Slack @ NVIDIA**
SharePoint site (owned by Vishal Seth) and **ServiceNow KB0020066 ("FAQ –
Slack")**:

- NVIDIA explicitly supports custom Slack apps. Quoting the SharePoint page:
  *"Slack is a powerful messaging platform with an awesome API that's easy to
  consume for building your custom apps, integrations and bots. Whenever you
  create an app or integration, you'll be required to have your integration
  approved by an admin."*
- **Approval is per-scope.** The reviewer (currently Vishal Seth) checks the
  Bot Token Scopes you request against an allowlist.
- **Banned scopes** (will not be approved): `admin.*`, `auditlogs.*`,
  `discovery.*`, `channels:join`, `chat:write.public`, `groups:write`,
  `conversations.connect:*`, plus most `*` user scopes (because user scopes
  impersonate employees), `remote_files:share`, `remote_files:write`.
- **Bot scopes are generally fine.** None of the scopes this guide requires
  (`chat:write`, `im:history`, `im:read`, `im:write`, `app_mentions:read`,
  `users:read`, `channels:history`) is on the banned list, so approval should
  be straightforward.
- There are two NVIDIA Slack workspaces — **NVIDIA Internal** and **NVIDIA
  External**. Pick Internal for personal-assistant use. Approvals are
  workspace-scoped, so an app approved on one is not automatically approved on
  the other.
- Open a request via ServiceNow if you cannot find the approval flow inside
  the Slack admin UI itself.
- E-Staff approval is required for **private channels** and **Slack Connect**
  invites, but **not** for installing an admin-approved bot into your own DMs.

> **Recommendation for `thaphan@nvidia.com`:** start in a **personal Slack
> workspace** while iterating. Once the bridge is stable and you actually want
> to DM the agent from your work Slack, file an NVIDIA Slack-app approval with
> the exact scope list from §3 below. There is no policy reason it would be
> denied, but the approval round-trip will slow you down during initial
> development.

---

## 2. Create the Slack app

1. Go to <https://api.slack.com/apps>.
2. Click **Create New App** → **From scratch**.
3. Fill in:
   - **App Name:** `agent-me` (or `agent-me-dev` while you iterate).
   - **Pick a workspace:** the one you chose in §1.
4. Click **Create App**. You land on the app's *Basic Information* page —
   keep this tab open, you'll come back to it for tokens and the signing
   secret.

> If you prefer declarative setup, you can paste a manifest under
> **Features → App Manifest**. A starter manifest lives at
> `services/slack-bridge/manifest.example.yaml` (create it as part of the
> implementation step).

---

## 3. Configure OAuth scopes

Navigate to **Features → OAuth & Permissions** and scroll to **Scopes →
Bot Token Scopes**. Add the following bot scopes:

| Scope               | Why we need it                                                              |
| ------------------- | --------------------------------------------------------------------------- |
| `chat:write`        | Post messages back into DMs and channels the bot is in.                     |
| `im:history`        | Read the prior messages in a DM thread (for context recovery).              |
| `im:read`           | Look up DM channel metadata.                                                |
| `im:write`          | Open a DM channel with the user if needed.                                  |
| `app_mentions:read` | Receive `@agent-me` mentions in public channels.                            |
| `users:read`        | Resolve a user ID to a display name when formatting responses.              |
| `channels:history`  | Read prior messages in a public channel thread (only if you use channels).  |

**Do not add user scopes.** They impersonate the installer and trigger extra
review on most workspaces (and are banned at NVIDIA).

If you later want the bot to read private channels, add `groups:history` and
`groups:read`, but that is **out of scope** for this initial setup.

---

## 4. Enable Socket Mode (recommended)

In **Settings → Socket Mode**, toggle **Enable Socket Mode** to **On**. Slack
will prompt you to create an *App-Level Token* with the `connections:write`
scope — name it `agent-me-socket` and create it. Save the resulting
`xapp-...` token; you'll need it in §6.

### Why Socket Mode for this use case

The agent runs on a **Brev** dev box (or any machine you SSH into). Socket
Mode is the right transport here because:

1. **No public HTTP endpoint.** Slack opens a WebSocket from your process
   *outbound* to Slack. Your machine never has to be reachable from the
   internet — no ngrok, no Cloudflare Tunnel, no inbound firewall hole.
2. **Works behind NAT and corp VPNs.** Outbound TLS to `slack.com` is
   essentially always allowed.
3. **No URL verification dance.** HTTP-mode apps must answer Slack's
   `url_verification` challenge on every endpoint change; Socket Mode skips
   that entirely.
4. **No request-signature plumbing for events.** The signing secret is still
   used (and we still verify it for any slash-command HTTP fallbacks), but
   event delivery over the WebSocket is authenticated by the app token.
5. **Reconnect is free.** Bolt's Socket Mode client handles backoff and
   reconnect automatically when your laptop sleeps or the Brev box restarts.

The trade-off is that Socket Mode is **single-tenant** — it is intended for
internal-use apps, which is exactly what `agent-me` is. If you ever
distribute this app to other workspaces (App Directory), you would need to
switch to HTTP mode. Don't.

---

## 5. Subscribe to events

In **Features → Event Subscriptions**:

1. Toggle **Enable Events** to **On**.
2. Because Socket Mode is on, **no Request URL field appears** — events are
   delivered over the WebSocket.
3. Under **Subscribe to bot events**, add:
   - `message.im` — DMs to the bot.
   - `app_mention` — `@agent-me` mentions in any channel the bot is in.
   - *(Optional, if you want channel threads)* `message.channels` —
     this is noisy; only enable if you really need passive channel reads.
4. Save changes. Slack may prompt you to reinstall the app — do so when
   prompted.

---

## 6. Install the app and capture tokens

1. Go to **Settings → Install App** and click **Install to Workspace**
   (NVIDIA: this submits for admin approval; expect a delay).
2. After install, copy the **Bot User OAuth Token** — starts with `xoxb-`.
3. From **Settings → Basic Information → App-Level Tokens**, copy the
   `xapp-...` token you generated in §4.
4. Also from **Basic Information → App Credentials**, copy the **Signing
   Secret** (used for any HTTP-mode fallback like slash commands).

You should now have three secrets:

| Variable               | Format    | Source                                  |
| ---------------------- | --------- | --------------------------------------- |
| `SLACK_BOT_TOKEN`      | `xoxb-…`  | OAuth & Permissions → Install App       |
| `SLACK_APP_TOKEN`      | `xapp-…`  | Basic Information → App-Level Tokens    |
| `SLACK_SIGNING_SECRET` | hex 32    | Basic Information → App Credentials     |

---

## 7. Store tokens safely

Pick **one** of:

### Option 1 — `.env` file (simple, dev only)

```bash
# ~/agent-me/services/slack-bridge/.env  (NEVER commit this)
SLACK_BOT_TOKEN=xoxb-XXXXXXXX
SLACK_APP_TOKEN=xapp-XXXXXXXX
SLACK_SIGNING_SECRET=XXXXXXXX
CLAUDE_CWD=/home/ubuntu/agent-me
```

Make sure `.env` is in `.gitignore`. Commit `.env.example` with empty values
instead.

### Option 2 — 1Password CLI (recommended)

```bash
op item create --category=apicredential \
  --title='agent-me Slack bridge' \
  --vault=Personal \
  SLACK_BOT_TOKEN=xoxb-... \
  SLACK_APP_TOKEN=xapp-... \
  SLACK_SIGNING_SECRET=...
```

Then load at runtime:

```bash
op run --env-file=.env.op -- node index.js
```

with `.env.op` containing `SLACK_BOT_TOKEN=op://Personal/agent-me Slack bridge/SLACK_BOT_TOKEN`.

### Option 3 — systemd `EnvironmentFile=` (for the long-running daemon)

Put the env file under `/etc/agent-me/slack.env` with `chmod 600`, owned by
the user that runs the service.

---

## 8. Architecture: the Slack ↔ Claude bridge

### Layout

```
~/agent-me/
├── CLAUDE.md                       # repo-level context Claude inherits
├── design/
│   └── slack-app-setup.md          # this file
└── services/
    └── slack-bridge/
        ├── package.json
        ├── index.js                # Bolt app entrypoint
        ├── claude.js               # spawns `claude -p` and streams output
        ├── store.js                # thread_ts -> conversation map (sqlite or json)
        ├── manifest.example.yaml   # optional: declarative app manifest
        ├── .env.example
        └── README.md
```

### `package.json` essentials

```json
{
  "name": "agent-me-slack-bridge",
  "private": true,
  "type": "module",
  "scripts": {
    "start": "node index.js",
    "dev": "node --watch index.js"
  },
  "dependencies": {
    "@slack/bolt": "^3.21.0",
    "better-sqlite3": "^11.3.0",
    "dotenv": "^16.4.5"
  }
}
```

### Minimal `index.js`

```js
import 'dotenv/config';
import pkg from '@slack/bolt';
const { App, LogLevel } = pkg;
import { runClaude } from './claude.js';
import { rememberThread, recallThread } from './store.js';

const app = new App({
  token: process.env.SLACK_BOT_TOKEN,
  appToken: process.env.SLACK_APP_TOKEN,
  signingSecret: process.env.SLACK_SIGNING_SECRET,
  socketMode: true,
  logLevel: LogLevel.INFO,
});

// DM handler
app.message(async ({ message, say, client }) => {
  if (message.channel_type !== 'im' || message.subtype) return;

  const threadKey = message.thread_ts ?? message.ts;
  const history = await recallThread(threadKey);

  await client.assistant?.threads?.setStatus?.({
    channel_id: message.channel,
    thread_ts: threadKey,
    status: 'is thinking…',
  }).catch(() => {}); // ignore if assistant API not enabled

  const reply = await runClaude({
    cwd: process.env.CLAUDE_CWD,
    prompt: message.text,
    history,
  });

  await say({ text: reply, thread_ts: threadKey });
  await rememberThread(threadKey, message.text, reply);
});

// @-mention handler
app.event('app_mention', async ({ event, say }) => {
  const threadKey = event.thread_ts ?? event.ts;
  const reply = await runClaude({
    cwd: process.env.CLAUDE_CWD,
    prompt: event.text.replace(/<@[^>]+>\s*/, ''),
    history: await recallThread(threadKey),
  });
  await say({ text: reply, thread_ts: threadKey });
  await rememberThread(threadKey, event.text, reply);
});

await app.start();
console.log('agent-me slack bridge: running on Socket Mode');
```

### `claude.js` — spawning the headless agent

```js
import { spawn } from 'node:child_process';

export async function runClaude({ cwd, prompt, history }) {
  const fullPrompt = history?.length
    ? `Prior turns:\n${history.map(h => `Q: ${h.q}\nA: ${h.a}`).join('\n\n')}\n\nNew message:\n${prompt}`
    : prompt;

  return new Promise((resolve, reject) => {
    const child = spawn('claude', ['-p', fullPrompt, '--output-format', 'text'], {
      cwd,
      env: process.env,
    });
    let out = '', err = '';
    child.stdout.on('data', d => (out += d));
    child.stderr.on('data', d => (err += d));
    child.on('close', code => (code === 0 ? resolve(out.trim()) : reject(new Error(err))));
  });
}
```

Key points:

- `cwd: ~/agent-me` so Claude reads `CLAUDE.md` from the repo root.
- `claude -p` is **headless**: one prompt, one answer, exits.
- For long answers, switch to `--output-format stream-json` and stream
  partial messages back to Slack via `chat.update` for a live-typing feel.

---

## 9. Threading & conversation state strategy

### DM rules

- **Top-level DM message** → start a new thread; reply with `thread_ts =
  message.ts`.
- **Reply inside an existing thread** → continue that thread.
- This gives every conversation a stable `thread_ts` that is the conversation
  ID. Flat (non-threaded) DMs are fine for one-shot questions but ruin
  multi-turn context.

### Channel rules

- Only respond to `app_mention`. Never lurk in channels.
- Always reply in a thread (never as a top-level reply) — keep noise low.

### Persistent context

Use SQLite (`better-sqlite3`) at `~/agent-me/services/slack-bridge/state.db`:

```sql
CREATE TABLE IF NOT EXISTS turns (
  thread_ts   TEXT NOT NULL,
  channel     TEXT NOT NULL,
  role        TEXT NOT NULL,         -- 'user' | 'assistant'
  content     TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (thread_ts, created_at)
);
```

On every turn:

1. Look up rows with `thread_ts = ?` ordered by `created_at`.
2. Concatenate them into the prompt sent to `claude -p`.
3. Cap context at, e.g., last 20 turns; let Claude's repo context do the
   heavy lifting.
4. Write the new user turn and assistant turn back.

> **Future iteration.** Replace the `claude -p` spawn with a long-lived
> `claude --resume <session-id>` so Claude keeps its own session memory and
> you only persist `(thread_ts → session_id)`. Swap in once the SDK ships a
> stable session resume API for headless mode.

---

## 10. Testing checklist

Run these in order.

### 10.1 Workspace + token sanity

```bash
# Should return {"ok":true, "user":"agent-me", ...}
curl -sS -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  https://slack.com/api/auth.test | jq
```

If `ok: false`, the bot token is wrong or not yet authorized — reinstall the
app.

### 10.2 App-level token sanity

```bash
# Should return {"ok":true, "url":"wss://wss-primary.slack.com/..."}
curl -sS -X POST -H "Authorization: Bearer $SLACK_APP_TOKEN" \
  https://slack.com/api/apps.connections.open | jq
```

If this fails, the app-level token is missing `connections:write`.

### 10.3 Socket Mode handshake

```bash
cd ~/agent-me/services/slack-bridge && npm start
# Expect: "agent-me slack bridge: running on Socket Mode"
```

### 10.4 First DM

1. Open a DM to **agent-me** in Slack.
2. Send `hello`.
3. The bot should reply within ~5–15s in a thread.

### 10.5 Multi-turn context

1. In the same thread: `what was my last message?`
2. Bot should answer "hello" — confirms `store.js` is working.

### 10.6 `@mention` in a channel

1. Invite the bot: `/invite @agent-me` in any channel.
2. Post `@agent-me what time is it?`.
3. Bot should reply in a thread.

### 10.7 Repo context inheritance

1. DM: `summarize what's in CLAUDE.md`.
2. Bot must reference your actual `CLAUDE.md` content — confirms `cwd` is
   set correctly.

### 10.8 Crash & reconnect

1. `Ctrl-C` the bridge, restart it.
2. DM still works; existing thread context is preserved (SQLite-backed).

---

## 11. Security checklist

- [ ] `.env` is in `.gitignore` and **never** committed. `git log -p -- '*.env'`
      should show nothing.
- [ ] Tokens are **not** logged. Search the bridge code: `grep -RIn "process.env\.SLACK" services/slack-bridge` and confirm no `console.log` prints them.
- [ ] Signing secret is verified on any HTTP endpoint you expose
      (Bolt does this automatically, but turn off any custom Express
      middleware that bypasses it).
- [ ] Bot ignores its own messages (Bolt does this by default — don't
      override `ignoreSelf`).
- [ ] **Token rotation plan.** Bot token regenerated every 90 days from the
      Slack admin UI, secrets file rewritten, daemon restarted. Set a
      calendar reminder.
- [ ] **Least privilege.** No user scopes. No `chat:write.public`. No
      `admin.*`. Re-audit scopes whenever you add a feature.
- [ ] **Limit blast radius of `claude -p`.** The agent has full repo write
      access in `cwd`. Decide whether the Slack bridge should run with a
      restricted Claude config (`--allowedTools` / sandboxed cwd) versus the
      full power of your interactive Claude. Initial recommendation: use a
      separate `~/agent-me-runtime/` checkout that only has read-only
      research tools enabled, until you trust the bridge.
- [ ] **Rate limits.** Add a simple per-user rate limit (e.g. 10
      messages/min) to avoid runaway loops costing Anthropic credits if a
      bug makes the bot reply to itself.
- [ ] **Brev exposure.** The Brev box should only have outbound network for
      the Slack bridge. Do not open inbound ports just for Slack — Socket
      Mode does not need them.

---

## 12b. Register native slash commands (recommended after first run)

By default, slash-style commands (`/mcp`, `/version`, `/help`) only work
when sent inside a normal message, e.g. `@agent-me /mcp` or as the body of
a DM message that gets routed through our text-intercept. They will **not**
be auto-completed by Slack and a bare `/mcp` typed at the top of a channel
will hit Slack's "command not found" error.

To get autocompletion + a clean bare-`/mcp` UX, register the commands in the
app config:

1. Open <https://api.slack.com/apps> → **agent-me** → sidebar **Slash Commands**.
2. Click **Create New Command** for each:
   | Command | Short Description | Usage Hint |
   |---|---|---|
   | `/mcp` | List MCP server health & auth status | _(blank)_ |
   | `/reauth` | Refresh Codex MCP auth helper | _(blank)_ |
   | `/version` | Show bridge + Codex versions | _(blank)_ |
   | `/whoami` | Show Slack user id | _(blank)_ |
   | `/help` | List bot commands | _(blank)_ |
   | `/brief` | Run daily/weekly/monthly brief | `[week|month]` |
   | `/brev` | Fill Brev credits form in screenshot test mode, or check browser SSO | `<org_id>` or `auth` |
   | `/model-free-draft` | Create Model Free reply-all draft | _(blank)_ |
3. **Request URL** field can stay blank in Socket Mode (Slack uses the WebSocket).
4. **Save** each one.
5. Sidebar → **Install App** → **Reinstall to Workspace** → **Allow**.
   Adding slash commands is a capability change so reinstall is required.

After this, you can type `/mcp` directly anywhere the bot is present
(DMs and channels it's been added to) and Slack will route the command to
the bridge.

## 12. Resolved decisions (for the upstream `agent-me` deployment)

These were the open questions; the upstream maintainer (`@thanhpt1110`) has
locked the following defaults. Forkers can override any of these.

1. **Workspace choice — _Personal Slack workspace_.**
   First deployment uses a fresh personal workspace owned by the user. No
   NVIDIA admin approval needed. NVIDIA Internal install is deferred — may
   never happen since the agent's job is autonomous, not interactive
   collaboration.
2. **Sandboxing — _Review-before-execute, with per-thread auto-approve
   toggle_.**
   Default mode: every Claude action that touches state (file write, shell
   run, git commit, git push, MCP write call) posts a Slack message with the
   diff/command and an "Approve ✅" / "Auto-approve all from this thread ⚡️"
   button. Once auto-approve is toggled on, future actions in that thread
   pass through without prompting until user clicks "Disable auto-approve".
   This mirrors Claude Code's permission model but with Slack buttons as the
   approval UI. Read-only actions (read file, MCP read call) execute
   without prompting.
3. **State store location — _ENV var with XDG default_.**
   `${AGENT_ME_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-me}`.
   Forkers override via env var; default follows XDG Base Directory spec.
4. **Streaming UX — _Hybrid: typing indicator + final post_.**
   On message receipt: post 🔄 "thinking…" placeholder immediately. On
   Claude completion: replace with full reply via `chat.update`. For long
   tasks (>2 min): post intermediate progress lines (still via `chat.update`
   on the same message) every ~30s. Stays well under Slack's 1 update/sec
   per channel rate limit and avoids the complexity of token-by-token
   streaming.

These decisions are recorded in `~/agent-me/STATE.md` and
`~/agent-me/discussions/2026-05-10-slack-decisions.md`.
