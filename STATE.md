# agent-me — Current State

_Last updated: 2026-05-10 by Claude (Opus 4.7) — after PA-mode experiment reverted._

## Phase

**Phase 2a complete + daily/weekly/monthly brief shipped + morning routine
live.** Bridge is fully Python+uv; daily-brief works end-to-end (`uv run
agent-me-brief --period {day,week,month}`); morning routine fires daily at
6am Vietnam time posting an MCP-status DM with reauth + brief buttons;
plain-text shortcuts and Block Kit interactivity are wired through.
**PA-hybrid attempt was reverted on 2026-05-10** — see "Recent decisions"
below. Next: prompt tuning (user-driven) → Phase 3 (Brev deploy) or
Phase 2b (approval gate).

## Decisions locked

| Topic | Choice |
|---|---|
| Runtime host | Brev cloud CPU instance (24/7) — Phase 3 |
| Primary interface | Personal Slack workspace (Socket Mode bridge) |
| Config repo | Personal GitHub, **public template** (`thanhpt1110/agent-me`) |
| Default model | Claude Opus 4.7 (1M ctx) |
| MCP backend | **Claude Code only** (PA hybrid tried and reverted) |
| Git identity | `includeIf` per-host: github.com → personal, default → NVIDIA |
| License | MIT |
| Slack sandboxing | Review-by-default + per-thread auto-approve toggle (Phase 2b) |
| State store path | `${AGENT_ME_STATE_DIR:-${XDG_STATE_HOME:-~/.local/state}/agent-me}` |
| Streaming UX | Hybrid: 🔄 placeholder → progress steps → final `chat.update` |
| File logging | structlog → console (pretty) + rotating JSON file `bridge.log` (10MB×5) |
| Morning routine | 6am Vietnam time (`Asia/Ho_Chi_Minh`); thread-rooted DM |
| Secrets vault | `~/agent-me-secrets.md` outside repo, chmod 600 |

## Done

- [x] Project + scaffold + bypassPermissions
- [x] **GitHub repo public template:** https://github.com/thanhpt1110/agent-me
- [x] **Bridge live (Python + slack-bolt async)** — DM, app_mention, 5 native slash commands (`/brief /mcp /reauth /version /whoami /help`), text-intercept slash, 25 plain-text shortcuts, Block Kit interactive buttons
- [x] **MCP re-auth helper** (`uv run agent-me-reauth`) — pty + auto-open browser tabs, 9 bug iterations resolved
- [x] **Daily-brief script** (`uv run agent-me-brief --period day|week|month`) — Jira/GitLab/GitHub/NVBugs/Confluence; grouped by project/repo/space; priority table; live placeholder updates; Block Kit refresh/reauth buttons
- [x] **Morning routine** — daily 6am VN-time DM, MCP probe, post-reauth menu in thread
- [x] **File logging** — `~/.local/state/agent-me/bridge.log` (rotating JSON) + `brief.log`
- [x] **`tail-log.sh`** + **`kill-bridge.sh`** helper scripts
- [x] **Secrets vault** at `~/agent-me-secrets.md` (outside repo, chmod 600)
- [x] **PA hybrid experiment** — built, then reverted (see Recent decisions)

## Recent decisions

- **2026-05-10 — PA hybrid reverted.** Built `MCP_CLI=pa|claude` swap
  in bridge + brief over a marathon session, but reverted before
  end-to-end validation. Reasoning: PA's enterprise-source auth was
  not retained between sessions (all 8 sources showed 401 the next
  morning), so the hypothesized "auth-retention win" didn't
  materialize on the bench. PA also adds 5–15s cold-start per
  invocation. Net: not worth the abstraction cost. Decision: stay
  on claude-only; user will tune the brief prompt directly. Revert
  commits: `2dd8b40 / 85c7119 / fd8799d`. Original PA work still in
  history (`d8907db / bdadca1 / efddcb6`) if we want to revisit.

## Roadmap (next session priorities)

1. **Prompt tuning** (user-driven). User explicitly said they'll
   tweak the brief prompt directly. Don't pre-empt this — wait for
   their direction.
2. **Phase 3 — Brev deploy** (highest leverage). Always-on host means
   morning routine, future cron jobs, and MCP auth retention all
   become reliable. Document Brev provisioning + systemd unit for
   bridge + timer for brief.
3. **Phase 2b — review-before-execute approval gate.** Slack-button
   gating for write tools. Design ready in `design/approval-hook-design.md`
   (file-system semaphore). Open question still: PreToolUse hook stays
   sync-blocked? Investigate before coding.
4. **Phase 4 — web dashboard** at `src/agent_me/dashboard/` (starlette
   + SSE) on Brev port-expose. Reads same SQLite state DB the bridge
   writes to.

## Open research / unresolved

- **Action interception mechanism** for Phase 2b: PreToolUse hook
  (cleanest) vs stream-parse (invasive). Investigate hook-blocking
  semantics first.
- **Brev region** — default us-west-2 unless user prefers otherwise.
- **NVBugs MCP auth** — periodically goes 401; requires manual
  `claude mcp` reauth. Not blocking briefs (other sources keep
  working) but nags morning routine.

## Phase 4 — locked decisions (deferred to after bridge stable)

- Web UI dashboard at `src/agent_me/dashboard/` (Python — likely
  Starlette + SSE; not Express).
- Public URL via Brev port-expose (`*.brev.dev`); URL may rotate per
  instance restart.
- Reads the bridge's SQLite + tails brief.log/bridge.log.

## Open questions / parking lot

- Memory architecture: keep auto-memory file-based or externalize to
  a DB the agent owns?
- Secrets management on Brev: scp-once vs 1Password CLI vs sops + age
  vs HashiCorp Vault. Current stop-gap = `~/agent-me-secrets.md` + scp.
- Audit log: log every action the agent takes for after-the-fact review.
- Dashboard auth: bearer token in URL vs Cloudflare Access vs simple basic auth.
