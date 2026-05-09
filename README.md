# agent-me

> _myself, but in agent mode._

Personal AI OS — a public-shareable framework for a 24/7 always-on autonomous agent that handles your daily/weekly workload. Fork it, set your own configs, deploy your own `agent-me`. First operator: thaphan@nvidia.com.

## Quickstart for forkers

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20+ (for the legacy bridge until the Python port lands), `claude` CLI, `gh` CLI.

1. **Use this template** on GitHub → create your own copy.
2. Clone the repo:
   ```bash
   git clone git@github.com:<you>/agent-me.git
   cd agent-me
   ```
3. Install Python deps with uv (creates `.venv` automatically):
   ```bash
   uv sync
   ```
4. **Slack app**: follow `design/slack-app-setup.md` to create your own app (personal workspace, ~10 min). Drop tokens into `configs/.env` (template at `configs/.env.example`).
5. **Authenticate MCPs**: when `claude mcp list` shows servers as `! Needs authentication`, run:
   ```bash
   uv run agent-me-reauth
   ```
   The helper auto-opens every stale MCP's auth URL in your browser; sign in to NVIDIA SSO in each tab. Tokens persist for ~24h. See `design/mcp-authentication.md` for the full playbook.
6. **Run the bridge** (currently the Node version under `services/slack-bridge/` — Python port is in progress):
   ```bash
   cd services/slack-bridge
   pnpm install
   pnpm dev
   ```
   From Slack, DM the bot or use `/help`, `/mcp`, `/version`.
7. **(Optional) Native slash commands**: register `/mcp`, `/version`, `/help` in the Slack app config — see `design/slack-app-setup.md` §12b.
8. **(Optional) Deploy**: Phase 3 moves the bridge to a 24/7 host (Brev). See `STATE.md` for current phase.

## Architecture overview

```
┌───────────────────────────────────────────────────────────┐
│  Interface Layer (làm sao user chat/ra lệnh khi xa máy)   │
│  - Slack/Teams bot? Telegram? Email? Web UI? CLI SSH?     │
└───────────────────────────────┬───────────────────────────┘
                                │
┌───────────────────────────────▼───────────────────────────┐
│  Orchestrator (Claude Opus 4.7, headless `claude -p`)     │
│  - Route request → đúng sub-agent                         │
│  - Schedule jobs (cron) cho daily/weekly                  │
│  - Memory & state (file-based, sync GitHub)               │
└──┬──────────┬──────────┬──────────┬──────────┬────────────┘
   │          │          │          │          │
┌──▼──┐    ┌──▼──┐    ┌──▼──┐    ┌──▼──┐    ┌──▼──┐
│Work │    │Know-│    │Code │    │Ops  │    │Life │
│Jira │    │ledge│    │Git- │    │Brev │    │Cal  │
│Bugs │    │Glean│    │Lab  │    │mon  │    │Mail │
└─────┘    └─────┘    └─────┘    └─────┘    └─────┘

┌───────────────────────────────────────────────────────────┐
│  Persistence: GitHub repo (private)                       │
│  - /configs, /skills, /prompts, /agents, /briefs (output) │
│  - Pull on startup, push on change                        │
└───────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────┐
│  Runtime host (24/7):                                     │
│  Option A — user's existing online server (SSH access)    │
│  Option B — Brev cloud instance (GPU not needed, CPU OK)  │
│  Option C — launchd local Mac (offline khi máy off)       │
└───────────────────────────────────────────────────────────┘
```

## Layout

- `discussions/` — chat logs & decision records (one per session, dated)
- `design/` — architecture docs, diagrams, ADRs
- `configs/` — runtime configs (synced to GitHub)
- `skills/` — custom skills the agent can invoke
- `scripts/` — bootstrap, deploy, sync scripts

## Status

This framework is under active development. See [STATE.md](STATE.md) for the current development phase, what's in flight, and what's next.

## License

MIT — see [LICENSE](LICENSE). Fork freely, deploy your own, and tell us what you build.
