# agent-me — Current State

_Last updated: 2026-05-10 by Claude (Opus 4.7) — fan-out brief + Slack session persistence shipped._

## Phase

**Phase 2a + brief fan-out + Slack session persistence live.** Bridge
is Python+uv; daily-brief now uses 7-subagent fan-out (jira / gitlab /
confluence / nvbugs / slack / outlook / github), one Slack root header
+ threaded reply per source, ~39s wall-clock vs old 60–230s.
Slack DM ↔ Claude Code session persistence: each `thread_ts` maps to
a `session_id` so multi-turn chat works (cache hits ~76k tokens on
turn 2). Morning routine fires daily at 6am Vietnam time. PA hybrid
attempted and reverted earlier today. Next: user-driven prompt
tuning → Phase 3 Brev deploy → Phase 2b approval gate.

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
- [x] **Bridge live (Python + slack-bolt async)** — DM, app_mention, native slash commands (`/brief /mcp /reauth /version /whoami /help`), text-intercept slash, plain-text shortcuts (incl. `reset` / `clear` / `new`), Block Kit interactive buttons
- [x] **MCP re-auth helper** (`uv run agent-me-reauth`) — pty + auto-open browser tabs, NVIDIA-SSO + ECI-OAuth flows
- [x] **Daily-brief — fan-out v2 (2026-05-10)** — `uv run agent-me-brief --period day|week|month`. 7 subagents in parallel (jira / gitlab / confluence / nvbugs / slack / outlook / github), one root header + threaded reply per source, priority synthesis posted last. ~39s wall-clock vs prev ~60–230s.
- [x] **Slack session persistence (2026-05-10)** — `claude_sessions` table maps `thread_ts → session_id`; bridge runs `claude -p --output-format json --resume <id>`. Cache hits compound across turns. `/reset` (+ plain shortcuts) clears a thread's session. `SessionExpired` exception falls back to fresh session if id is stale. See `design/session-persistence.md`.
- [x] **MCPs registered (17 total)** — Slack + Outlook added 2026-05-10 at user scope (project-local scope confused the OAuth helper; learnt the hard way).
- [x] **Morning routine** — daily 6am VN-time DM, MCP probe, post-reauth menu in thread
- [x] **File logging** — `~/.local/state/agent-me/bridge.log` (rotating JSON) + `brief.log`
- [x] **`scripts/setup-mcps.sh` + `scripts/bootstrap.sh`** — idempotent fresh-host setup; `design/setup-on-fresh-host.md` walks through prerequisites
- [x] **Deploy artifacts (2026-05-10)** — `deploy/agent-me-bridge.service` + `agent-me-watch.service` (systemd --user), `scripts/agent-me-watch.sh` (60s git-pull-and-restart loop), `scripts/install-systemd.sh` (idempotent installer + linger). `design/deploy-on-host.md` is the step-by-step playbook another Claude session can follow with minimal human input (browser twice for `claude /login` + `agent-me-reauth`, scp once for secrets). Targets any internal-NVIDIA systemd Linux host (Colossus is first-class; external clouds like Brev work for the bridge but block on MaaS MCP endpoints).
- [x] **`design/maas-mcp-catalog.md`** — full MaaS MCP catalog reference
- [x] **`tail-log.sh`** + **`kill-bridge.sh`** helper scripts
- [x] **Secrets vault** at `~/agent-me-secrets.md` (outside repo, chmod 600)
- [x] **PA hybrid experiment** — built, then reverted (see Recent decisions)

## Recent decisions

- **2026-05-10 — Brief fan-out.** Single-prompt → 7-subagent fan-out
  in `daily_brief.py`. Each subagent scoped to one MCP server's
  tool wildcard, posts one threaded reply when done. Cuts wall-clock
  ~3-5×, isolates per-source failures, fits Slack message limits.
  See `discussions/2026-05-10-fanout-and-session-persistence.md`.
- **2026-05-10 — Slack session persistence.** `--resume <session_id>`
  per `thread_ts`. Multi-turn chat in Slack now works; cache hits
  stack across turns. Top-level DMs are still each their own session
  (use threads for multi-turn). `/reset` clears.
- **2026-05-10 — Bridge chat uses `chat-cwd`, not REPO_DIR.** First
  test surfaced that running `claude -p` from REPO_DIR loaded the
  project's CLAUDE.md (containing the auto-memory protocol meant for
  dev sessions); claude faithfully wrote `.md` files when the user
  said "ghi nhớ", costing 10 turns / 78s / $1.09. Plus the
  `--dangerously-skip-permissions` flag I'd added in the refactor was
  bypassing `--disallowedTools` (Write was supposed to be blocked but
  wasn't). Fix: cwd → `~/.local/state/agent-me/chat-cwd/` (empty,
  no CLAUDE.md), drop the dangerous flag, add
  `--permission-mode dontAsk`. Result: 1 turn / 2.6s / $0.10 / no
  unwanted writes. Brief and reauth helper still use REPO_DIR — they
  need project context. Decision: app-level user-preference storage
  (e.g. "remember user's name"), if ever needed, lives in bridge
  SQLite — NOT in claude's `~/.claude/projects/.../memory/` .md
  files. That dir is for Claude Code dev agents, not app users.
- **2026-05-10 — Deploy target: Colossus, not Brev.** First Brev
  attempt revealed external network can't reach NVIDIA MaaS MCP
  endpoints (`*.nvidia.com`). MaaS MCPs are non-negotiable for the
  bridge to work, so deployment moved to Colossus (internal network).
  Playbook stays generic (`design/deploy-on-host.md`) — works on any
  internal NVIDIA systemd Linux box; Brev kept as documented
  alternative for the future-VPN scenario.
- **2026-05-10 — All MCPs at user scope.** `setup-mcps.sh` enforces
  `--scope user`. Project-local servers' OAuth flow confused
  `agent-me-reauth`; user scope behaves identically to the older
  Azure-auth servers.
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

1. **Phase 3 — host deploy** ← **in flight (2026-05-10)**. Deploy
   artifacts shipped (`deploy/agent-me-bridge.service`,
   `agent-me-watch.service`, `scripts/agent-me-watch.sh`,
   `scripts/install-systemd.sh`); `design/deploy-on-host.md` is the
   single playbook a Claude session on the host follows end-to-end.
   Pivot from Brev → Colossus on 2026-05-10: Brev is external network
   so MaaS MCP endpoints (`*.nvidia.com`) 401 from there. Colossus
   has internal network and is the new target. User will SSH to
   Colossus, install Claude Code CLI, then ask that Claude to walk
   the playbook, scp `~/agent-me-secrets.md` once for tokens.
   Auto-deploy via 60s polling watcher → git pull → uv sync (if
   pyproject changed) → `systemctl --user restart agent-me-bridge`.
   Slack uses Socket Mode (no public endpoint needed). Awaiting user
   to provide Colossus hostname/SSH access.
2. **Prompt tuning** (user-driven). User explicitly said they'll
   tweak the brief prompt directly. Don't pre-empt — wait.
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
