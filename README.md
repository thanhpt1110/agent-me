# agent-me

> _myself, but in agent mode._

Personal AI OS — a public-shareable framework for a 24/7 always-on autonomous agent that handles your daily/weekly workload. Fork it, set your own configs, deploy your own `agent-me`. First operator: thaphan@nvidia.com.

## Quickstart for forkers

1. Click **"Use this template"** on GitHub to create your own copy of this repo.
2. Clone your new repo to your machine: `git clone git@github.com:<you>/agent-me.git && cd agent-me`.
3. Edit `configs/` with your own credentials, MCP server endpoints, and runtime settings, then run `scripts/bootstrap.sh` on the host you want the agent to live on.

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
