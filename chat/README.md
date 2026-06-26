# agent-me chat (DEV)

An **always-on web chat** that lives inside the `agent-me` repo. Each chat is a
**persistent Claude Code session** that executes on this VM's workspace (full tool
access: Bash, file edits, git, the QA-E2E toolchain, …). Prompt from any browser;
turns run **server-side**, so the agent keeps working after you close the tab or
your laptop goes offline. State is persisted to disk, so reconnecting catches up.

> **DEV / migration target.** This runs on **port 8787** on this VM. The
> **production** `agent-me` (Slack bridge + dashboard) still runs on the **A100**
> (`agent_me.dashboard` on :8765). This chat is for testing before cut-over; it is
> deliberately decoupled from the production services and the `main` branch
> (lives on the `big-update` branch).

## How it works
- **Backend** (`src/server.js`): Express + WebSocket. Spawns the local `claude`
  CLI per turn (`--output-format stream-json`, `--resume <id>`,
  `--dangerously-skip-permissions`). The CLI reuses the box's existing OAuth login —
  no `ANTHROPIC_API_KEY` needed (which is why we use the CLI, not the Agent SDK).
- **Sessions** (`src/sessions.js`): one dir per chat under `data/sessions/<id>/`.
- **Frontend** (`public/`): chat UI — sidebar, live streaming of text + tool calls.

## Access control — Thanh-Phan-only
Login is gated by a single password, resolved (first match) from:
`CHAT_PASSWORD` → `DASHBOARD_OPERATOR_ACTION_CODE` → `DASHBOARD_TOKEN` →
`ACCESS_TOKEN` → a generated token (`data/access-token.txt`, printed on first
start). The `DASHBOARD_*` values come from `../configs/.env` — i.e. the **same
secret that gates the dashboard's operator buttons**, so only the operator
(Thanh Phan) can use it.

## Run always-on (systemd --user, survives logout + reboot)
Linger is already enabled for this user.
```bash
cp ../deploy/agent-me-chat.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-me-chat
journalctl --user -u agent-me-chat -f
```
Reachable at `http://<vm-ip>:8787` across the NVIDIA network. Optional public URL:
`../deploy/agent-me-chat-tunnel.service` (cloudflared quick tunnel).

## ⚠️ Security
Each chat runs Claude with `--dangerously-skip-permissions` — **unrestricted shell
+ file access on this VM** as your user. The only gate is the password + the
`0.0.0.0` bind. Treat the password like a credential.

## Config
See `.env.example` (and the shared `../configs/.env`).
