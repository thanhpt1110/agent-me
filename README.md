# agent-me

[![CI](https://github.com/thanhpt1110/agent-me/actions/workflows/ci.yml/badge.svg)](https://github.com/thanhpt1110/agent-me/actions/workflows/ci.yml)
[![CodeQL](https://github.com/thanhpt1110/agent-me/actions/workflows/codeql.yml/badge.svg)](https://github.com/thanhpt1110/agent-me/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)

> _myself, but in agent mode._

Personal AI OS — a public-shareable framework for a 24/7 always-on autonomous agent that handles your daily/weekly workload. Fork it, set your own configs, deploy your own `agent-me`. First operator: thaphan@nvidia.com.

## Quickstart for forkers

Prerequisites: `claude` CLI, [uv](https://docs.astral.sh/uv/), `gh` CLI, `jq`, Python 3.12+, Node (`claude` itself + the playwright MCP).

1. **Use this template** on GitHub → create your own copy.
2. **Clone & bootstrap**:
   ```bash
   git clone git@github.com:<you>/agent-me.git
   cd agent-me
   ./scripts/bootstrap.sh
   ```
   Runs `uv sync`, prepares `configs/.env`, and registers all 17 MaaS MCP servers idempotently (Jira, GitLab, Confluence, NVBugs, Slack, Outlook, GDrive, OneDrive, SharePoint, Glean, Jama, IPPSEC, MySQL, Nsight-CUDA, NVKS-Prometheus, PagerDuty, Playwright). See `design/setup-on-fresh-host.md` for the long version (incl. Brev specifics).
3. **Three interactive steps** the bootstrap script reminds you to do (browser required):
   - `claude /login` — one-time per machine. (Or `export ANTHROPIC_API_KEY=...` for headless deploys.)
   - `uv run agent-me-reauth` — opens NVIDIA-SSO tabs for each MCP. Tokens last ~24h.
   - Fill `configs/.env` with Slack tokens (template = `configs/.env.example`). Slack app walkthrough: `design/slack-app-setup.md`.
4. **Verify**:
   ```bash
   claude mcp list                            # all 17 should be ✓ Connected
   uv run agent-me-brief --period day --dry-run
   ```
5. **Run the bridge**:
   ```bash
   uv run agent-me-bridge
   ```
   From Slack, DM the bot or use `/help`, `/mcp`, `/reauth`, `/version`, `/whoami`, `/brief`.
6. **(Optional) Native slash commands**: register `/mcp`, `/reauth`, `/version`, `/whoami`, `/help`, `/brief` in the Slack app config — see `design/slack-app-setup.md` §12b. Without this, prefix the command with `@agent-me ` (the bridge intercepts text-form slashes too).
7. **(Optional) Deploy on a 24/7 host**: `design/deploy-on-host.md` is the end-to-end playbook (Colossus / any internal-NVIDIA systemd Linux box). Auto-deploys on every git push (60s polling watcher → systemctl restart bridge).

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
