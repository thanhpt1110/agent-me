# agent-me — Current State

_Last updated: 2026-05-10 by Claude (Opus 4.7)_

## Phase

**Phase 2 — Slack interface decisions locked. Ready to create app.** All four design questions resolved. Next concrete actions: create personal Slack workspace + Slack app, then build the bridge service.

## Decisions locked

| Topic | Choice |
|---|---|
| Runtime host | Brev cloud CPU instance (24/7) |
| Primary interface | **Personal Slack workspace** (Socket Mode bridge) |
| Config repo | Personal GitHub, **public template** (`thanhpt1110/agent-me`) |
| Default model | Claude Opus 4.7 (1M ctx) |
| Git identity | `includeIf` per-host: github.com → personal, default → NVIDIA |
| License | MIT |
| Slack sandboxing | Review-by-default + per-thread auto-approve toggle |
| State store path | `${AGENT_ME_STATE_DIR:-${XDG_STATE_HOME:-~/.local/state}/agent-me}` |
| Streaming UX | Hybrid: 🔄 placeholder → progress every ~30s → final `chat.update` |

## Done

- [x] Project named **agent-me** ("myself, but in agent mode")
- [x] Folder scaffold + bypassPermissions
- [x] CLAUDE.md, STATE.md, README.md, LICENSE, .gitignore
- [x] `~/.gitconfig` `includeIf` rules for per-host identity
- [x] `gh` CLI installed + authed as `thanhpt1110`
- [x] **GitHub repo published:** public template at https://github.com/thanhpt1110/agent-me
- [x] `design/slack-app-setup.md` — end-to-end Slack app + Socket Mode bridge guide
- [x] Slack design questions resolved + recorded (`discussions/2026-05-10-slack-decisions.md`)
- [x] Terminal.app default profile switched to Basic (light)

## Next up (in order)

1. **User: create personal Slack workspace** — https://slack.com/get-started, free.
2. **User: create Slack app at api.slack.com/apps** — follow `design/slack-app-setup.md` §2-§6:
   - From scratch, name `agent-me`, install into personal workspace
   - OAuth scopes per §3, Socket Mode enabled per §4, events subscribed per §5
   - Capture: Bot token (`xoxb-...`), App token (`xapp-...`), Signing secret
3. **User: drop tokens into `~/agent-me/configs/.env`** (gitignored). Template at `configs/.env.example` (TODO: create this).
4. **Build bridge service** at `~/agent-me/services/slack-bridge/` per §8:
   - Node + `@slack/bolt` Socket Mode
   - PreToolUse hook integration for review-by-default approval flow
   - SQLite state store at `${AGENT_ME_STATE_DIR:-...}/state.db`
   - Hybrid streaming UX
5. **Provision Brev instance** — region, size, install claude CLI/node/git/tmux
6. **Deploy bridge to Brev** + start always-on session
7. **Port `~/daily-brief/` into agent-me** as first sub-agent (cron-driven)
8. **Build Orchestrator** — slash command `/route` that dispatches to sub-agents

## Open research / unresolved

- **Action interception mechanism** for review-by-default flow:
  - (a) Claude Code `PreToolUse` hook posts to Slack and blocks → cleanest
  - (b) Bridge parses Claude's stream and pauses subprocess → more invasive
  - **Decision:** investigate (a) in next session before writing the bridge.
- Brev region preference (latency vs cost)? **→ default to us-west-2 unless user says otherwise.**

## Open questions / parking lot

- Memory architecture: keep using auto-memory or externalize to a DB the agent owns?
- Secrets management on Brev: 1Password CLI, sops + age, HashiCorp Vault?
- Audit log: log every action the agent takes for after-the-fact review?
- Web UI dashboard later (Phase 4+)?
