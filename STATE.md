# agent-me — Current State

_Last updated: 2026-05-10 by Claude (Opus 4.7)_

## Phase

**Phase 2a complete — bridge live, fully Python+uv.** Slack bridge ported from Node to Python (`uv run agent-me-bridge`); Node code deleted. MCP re-auth helper (`uv run agent-me-reauth`) auto-opens browser tabs. Slash commands `/mcp /version /whoami /help /reauth` work both as native Slack commands and as in-message text. Periodic 6h MCP-auth health probe DMs the operator when re-auth is needed. Next: Phase 3 (Brev deploy) or Phase 2b (PreToolUse approval gate).

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
- [x] Slack design questions resolved (`discussions/2026-05-10-slack-decisions.md`)
- [x] Personal Slack workspace + custom app + bot/app/signing tokens in `configs/.env`
- [x] **Phase 2a bridge live** — DM + mention; slash commands `/mcp /version /whoami /help /reauth` (native + text-intercept); 6h MCP health probe + DM notify
- [x] **Python+uv migration** — Node bridge deleted; bridge runs via `uv run agent-me-bridge`; pyproject.toml + uv.lock
- [x] `src/agent_me/scripts/reauth_mcps.py` — auto-open MCP re-auth helper (pty + URL extraction + client_id dedupe + tail-trim heuristic)
- [x] `design/mcp-authentication.md` — full re-auth playbook
- [x] `discussions/2026-05-10-pa-vs-custom-comparison.md` — defense of build-vs-PA choice
- [x] STATE.md Phase 4 dashboard decisions locked (Brev port-expose, build after bridge stable)
- [x] Terminal.app default profile switched to Basic (light)

## Next up (pick one)

1. **Phase 2b — PreToolUse approval gate.** Build the review-before-execute flow with Slack buttons. See `design/approval-hook-design.md` for the file-system semaphore approach.
2. **Phase 3 — Brev deployment.** Provision instance, port-expose, install uv + claude CLI, systemd unit for `agent-me-bridge`, MCP auth via SSH port-forward (see `design/mcp-authentication.md` Pattern A).
3. **Port `~/daily-brief/`** into agent-me as the first cron-driven sub-agent.
4. **Phase 4 — Web dashboard** at `src/agent_me/dashboard/` (Express+SSE equivalent in Python, e.g. starlette+SSE).

## Open research / unresolved

- **Action interception mechanism** for review-by-default flow:
  - (a) Claude Code `PreToolUse` hook posts to Slack and blocks → cleanest
  - (b) Bridge parses Claude's stream and pauses subprocess → more invasive
  - **Decision:** investigate (a) in next session before writing the bridge.
- Brev region preference (latency vs cost)? **→ default to us-west-2 unless user says otherwise.**

## Phase 4 — locked decisions (deferred to after bridge)

- **Web UI dashboard:** build under `services/dashboard/` (Express + SSE, reads
  same SQLite state DB as bridge, tails bridge.log + claude.log). Shows running
  task, pending Slack approvals, recent actions, daily brief, system health.
- **Public URL strategy:** Brev built-in port-expose (URL form `*.brev.dev`).
  Accept that URL may rotate per instance restart; document the new URL in
  `STATE.md` whenever it changes.
- **Why not PA:** PA is a desktop app + CLI; no daemon/web mode that exposes
  agent-me task progress externally. See `discussions/2026-05-10-pa-vs-custom-comparison.md`.

## Open questions / parking lot

- Memory architecture: keep using auto-memory or externalize to a DB the agent owns?
- Secrets management on Brev: 1Password CLI, sops + age, HashiCorp Vault?
- Audit log: log every action the agent takes for after-the-fact review?
- Dashboard auth: bearer token in URL vs Cloudflare Access vs simple basic auth?
