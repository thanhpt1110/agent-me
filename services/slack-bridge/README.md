# `@agent-me/slack-bridge`

Slack ↔ Claude bridge service for [`agent-me`](../../). Receives DMs and
`@mentions` from Slack via **Socket Mode** (no public HTTP endpoint), spawns
headless `claude -p` with `cwd = $AGENT_ME_REPO_DIR` so the agent inherits the
repo's `CLAUDE.md` and project memory, and streams replies back into the same
Slack thread with a hybrid streaming UX.

> Architecture spec: [`design/slack-app-setup.md` §8](../../design/slack-app-setup.md#8-architecture-the-slack--claude-bridge).
> Decisions rationale: [`discussions/2026-05-10-slack-decisions.md`](../../discussions/2026-05-10-slack-decisions.md).

## What it does

- Listens on Slack `message.im` (DMs) and `app_mention` events.
- For each new turn: posts a 🔄 thinking placeholder, then spawns `claude -p`,
  then `chat.update`s the placeholder every ~30s with progress, then replaces
  it with the final answer.
- Implements **review-by-default** action gating: any state-touching tool call
  (file write, shell exec, git push, MCP write) posts a Block Kit approval
  prompt with three buttons:
  - **Approve** — execute this action only.
  - **Approve all in this thread** — toggle `threads.auto_approve = 1`.
  - **Reject** — abort and tell the user why.
- Persists per-thread conversation history and pending approvals in SQLite at
  `${AGENT_ME_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-me}/state.db`.

## Environment variables

Read from `${AGENT_ME_REPO_DIR}/configs/.env`. Template at
[`configs/.env.example`](../../configs/.env.example).

| Variable                | Required | Description                                                                                  |
| ----------------------- | :------: | -------------------------------------------------------------------------------------------- |
| `SLACK_BOT_TOKEN`       |    ✓     | `xoxb-…` — bot User OAuth token from Slack OAuth & Permissions page.                         |
| `SLACK_APP_TOKEN`       |    ✓     | `xapp-…` — app-level token with `connections:write` (Socket Mode).                           |
| `SLACK_SIGNING_SECRET`  |    ✓     | Hex string from Basic Information → App Credentials. Used for HTTP fallbacks.                |
| `SLACK_ALLOWED_USER_ID` |          | Restrict bridge to one Slack user ID. Empty = accept any workspace member.                   |
| `AGENT_ME_REPO_DIR`     |          | Path to the repo root. Default: `/home/agent/agent-me`. Used as `cwd` for `claude -p`.       |
| `AGENT_ME_STATE_DIR`    |          | Override for state directory. Default: `${XDG_STATE_HOME:-$HOME/.local/state}/agent-me`.     |
| `CLAUDE_MODEL`          |          | Model override; defaults to whatever `claude` CLI is configured for.                         |
| `LOG_LEVEL`             |          | `debug` \| `info` \| `warn` \| `error`. Default: `info`.                                     |

## Dev quickstart

```bash
cd ~/agent-me/services/slack-bridge
pnpm install
cp ../../configs/.env.example ../../configs/.env
# fill in SLACK_BOT_TOKEN / SLACK_APP_TOKEN / SLACK_SIGNING_SECRET
pnpm dev
```

You should see:

```
agent-me slack bridge: running on Socket Mode
```

Then DM the bot in your Slack workspace. The bot replies in a thread within a
few seconds.

## Layout

```
slack-bridge/
├── package.json          ESM, node >=20
├── index.js              Bolt app entrypoint + handler stubs
├── db/
│   └── schema.sql        SQLite DDL (threads, messages, pending_approvals)
├── README.md             this file
└── .gitignore            defense-in-depth secret/state ignores
```

## Status

This is a **P1 scaffold**. Handler bodies are stubs that log and throw
`not implemented`. The Claude spawn pipeline, approval gate, and `chat.update`
streaming UX are P2 work — see TODOs inside `index.js`.
