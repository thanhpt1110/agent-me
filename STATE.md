# agent-me — Current State

_Last updated: 2026-05-10 by Claude (Opus 4.7) — Phase 4 dashboard drafted; Phase 3 Colossus deploy steps 1–5 done (16/17 MCPs ✓ via Keychain transfer); steps 6–8 (systemd / smoke test / auto-deploy verify) on the user._

## Phase

**Phase 2a + brief fan-out + Slack session persistence live + Phase 4
dashboard drafted (not deployed).** Bridge is Python+uv; daily-brief
uses 7-subagent fan-out, ~39s wall-clock. Slack DM ↔ Claude Code
session persistence shipped. Morning routine fires daily at 6am
Vietnam time. **Phase 4 dashboard scaffold landed today (2026-05-10):**
Starlette + Jinja2 + Alpine.js + Tailwind CDN; reads bridge SQLite
read-only; on-demand brief refresh per source with single-flight
locks; SSE log tail; bearer-token auth; **Tailscale Funnel chosen for
public URL** (no DNS, no bandwidth/request cap, no interstitial).
Smoke test passed locally — code compiles, all 14 routes respond,
auth enforces. **Not yet deployed** — pending Phase 3 Colossus host
ready (MCP reauth verify in flight). Next: user-driven prompt tuning
→ finish Phase 3 deploy → install dashboard on Colossus → Phase 2b
approval gate.

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
- [x] **Mac→host MCP token sync (2026-05-10)** — `scripts/sync-mcp-creds-to-host.sh`. Extracts the Mac Keychain item `Claude Code-credentials` (plain JSON `{"mcpOAuth":{...}}`), jq-merges with the host's existing `~/.claude/.credentials.json` (which already has `claudeAiOauth` from `claude /login`), scp's back. **One command instead of 16 browser OAuth flows** when bringing up a new host; idempotent so it doubles as the daily refresh after a Mac-side reauth. Empirically 16/17 maas-* turn ✓ Connected immediately on Colossus this way (only nvbugs needed Mac-side reauth first). Caveat: each token's `redirect_uri` records the Mac's localhost:NNNN, but ECI doesn't enforce redirect_uri match on refresh, so refresh from the host works.
- [x] **Phase 3 deploy on Colossus 1xA100-40 — steps 1–5 done (2026-05-10)** — Ubuntu 24.04, 16 CPU / 125 GB RAM / 731 GB free; passwordless sudo. Tools installed (uv, claude, gh, node), repo cloned at `~/agent-me`, `bootstrap.sh` registered all 17 MCPs at user scope, secrets vault scp'd + applied to `configs/.env`, `gh auth` linked, `claude /login` done, MCP tokens synced from Mac (16/17 ✓; nvbugs stale on both Mac and Colossus). Steps 6–8 (`scripts/install-systemd.sh` + Slack DM smoke test + auto-deploy verify) handed back to user — they're driving from a claude session on Colossus.
- [x] **`design/maas-mcp-catalog.md`** — full MaaS MCP catalog reference
- [x] **`tail-log.sh`** + **`kill-bridge.sh`** helper scripts
- [x] **Secrets vault** at `~/agent-me-secrets.md` (outside repo, chmod 600)
- [x] **PA hybrid experiment** — built, then reverted (see Recent decisions)
- [x] **Phase 4 dashboard — DRAFT (2026-05-10)** — `src/agent_me/dashboard/`
  (Starlette + Jinja2 + Alpine.js + Tailwind CDN). Read-only over
  bridge SQLite (URI `mode=ro`); on-demand brief refresh per source
  via `agent-me-brief` reused fetcher/parser (no Slack post,
  single-flight per source); SSE log tail; bearer-token auth
  (`DASHBOARD_TOKEN`). Public URL via **Tailscale Funnel** —
  `<host>.<tailnet>.ts.net`, no DNS, no bandwidth cap (free
  Personal), no interstitial. Two new systemd `--user` units
  (`agent-me-dashboard.service`, `agent-me-funnel.service`) +
  `scripts/install-dashboard.sh` idempotent installer. Bridge service
  is **untouched**. Design doc: `design/dashboard-design.md`. Smoke
  tested locally (compile + import + routes + auth). **Not deployed
  yet** — waiting on Phase 3 Colossus host to stabilize.
- [x] **Phase 4 polish round (2026-05-10 evening, post-Phase-3-pivot)** —
  before Colossus deploy. Auto-redeploy: `agent-me-watch.sh`
  auto-detects `agent-me-bridge` + `agent-me-dashboard` from
  `~/.config/systemd/user/` and restarts both on git pull (env
  override `AGENT_ME_RESTART_UNITS` if you want to be explicit;
  legacy `AGENT_ME_BRIDGE_UNIT` still honoured). New endpoint
  `POST /api/refresh/_all` fans out 7 single-source jobs in parallel
  (single-flight per source); overview page got a Refresh-All button
  with live per-card progress badges. Edge cases: log-tail handles
  rotation by both inode and size (was size-only); `_ro_connect`
  sets `busy_timeout=1500ms` so reads don't fail on transient
  checkpoint locks; broader `sqlite3.Error` catch instead of just
  `OperationalError`. Mobile: nav is now horizontal-scroll on phones
  (no hamburger); per-item meta wraps under title on narrow screens.
  Pytest scaffold: `tests/{conftest,test_state_reader,test_auth,test_app}.py`
  — 34 tests, covers DB queries (with/without seed), brief cache,
  log scrape, auth (bearer/cookie/exempt/handshake), full route
  surface (HTML render + JSON API + refresh-all). All green; ruff
  zero warnings. Live boot smoke: `/healthz` 200, `/` 401 unauth /
  200 with bearer, `/api/state` returns 7 snapshots, `/api/refresh/_all`
  202.

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

## Recent decisions (continued, 2026-05-10 evening)

- **2026-05-10 — Phase 4 tunnel: Tailscale Funnel (locked).** Compared
  Cloudflare Quick Tunnel (random URL, no SSE), ngrok free static
  (1GB/20K req cap + interstitial every 7d), Cloudflare Named Tunnel
  (needs owned domain), and Tailscale Funnel. User explicitly flagged
  "report daily/weekly + đôi khi chat" — chat over SSE could blow
  ngrok's 1GB/20K caps and the interstitial is constant UX friction.
  Tailscale Funnel: stable URL `<host>.<tailnet>.ts.net`, no cap on
  free Personal, no interstitial, supports SSE/WebSocket, outbound-
  only from host (same security posture as bridge). Trade-off: cost
  one `apt install tailscale` and one Tailscale account signup
  (free, OAuth via Google/GitHub). Worth it. STATE.md "Phase 4 — locked
  decisions" section's old "Brev port-expose" line is **superseded**
  by this — Brev was abandoned same morning when MaaS MCPs proved
  unreachable from external networks.
- **2026-05-10 — Mac Keychain → host credentials transfer.** Discovered
  the Mac stores all MCP OAuth tokens as plain JSON inside the Keychain
  item `Claude Code-credentials` (`{"mcpOAuth":{"<server>|<id>":{...}}}`).
  The Linux host stores the same shape at `~/.claude/.credentials.json`
  with `claudeAiOauth` next to `mcpOAuth` — disjoint top-level keys, jq
  merge is a one-liner. Codified as `scripts/sync-mcp-creds-to-host.sh`.
  Daily refresh workflow: `uv run agent-me-reauth` on Mac → opens stale
  URLs locally (Mac has a browser) → `./scripts/sync-mcp-creds-to-host.sh
  <host>` → host gets fresh tokens. Replaces the SSH-port-forward +
  agent-me-reauth-on-the-host path as the recommended ritual.
- **2026-05-10 — Phase 4 FE stack: Jinja2+Alpine, NOT Flutter Web.**
  Bandwidth isn't the deciding factor (Tailscale Funnel has no cap),
  but Flutter Web's 3-6 MB bundle + 3-7s cold-start trade UX in the
  wrong direction for "open dashboard, see report" use case. Plus
  it'd add a Dart toolchain to a Python-only project. Sticking with
  server-rendered HTML + sprinkled Alpine reactivity; if UX ever
  feels insufficient, upgrade path is SvelteKit (~30 KB), not
  Flutter.

## Roadmap (next session priorities)

1. **Phase 3 — host deploy** ← **steps 1–5 done, 6–8 in user's
   hands (2026-05-10)**. Target = Colossus `1xA100-40` (Ubuntu 24.04,
   16 CPU / 125 GB RAM, internal NVIDIA network so MaaS MCPs reach).
   Deploy artifacts shipped (`deploy/*.service`, `agent-me-watch.sh`,
   `install-systemd.sh`); `design/deploy-on-host.md` is the playbook.
   Done: tools, bootstrap, secrets, `claude /login`, **MCP tokens
   synced from Mac via `scripts/sync-mcp-creds-to-host.sh`** (16/17 ✓
   in one command, no per-server browser flow). Remaining for user:
   `./scripts/install-systemd.sh` on Colossus → smoke-test by DMing
   `mcp` to bot → push trivial commit to verify watcher restarts
   bridge within 60s. User is driving from a claude session on the
   host.
2. **Prompt tuning** (user-driven). User explicitly said they'll
   tweak the brief prompt directly. Don't pre-empt — wait.
3. **Phase 2b — review-before-execute approval gate.** Slack-button
   gating for write tools. Design ready in `design/approval-hook-design.md`
   (file-system semaphore). Open question still: PreToolUse hook stays
   sync-blocked? Investigate before coding.
4. **Phase 4 — web dashboard** ← **DRAFTED (2026-05-10)**, not yet
   deployed. Code at `src/agent_me/dashboard/`; design at
   `design/dashboard-design.md`. **Public URL via Tailscale Funnel**
   (locked), supersedes the earlier "Brev port-expose" plan since
   Brev was abandoned same day. Once Phase 3 Colossus is stable, run
   `scripts/install-dashboard.sh` on Colossus → installs tailscale
   if missing, generates `DASHBOARD_TOKEN`, registers two systemd
   `--user` units (dashboard + funnel). Public URL after install:
   `https://<host>.<tailnet>.ts.net`. The bridge service is not
   modified or restarted.

## Open research / unresolved

- **Action interception mechanism** for Phase 2b: PreToolUse hook
  (cleanest) vs stream-parse (invasive). Investigate hook-blocking
  semantics first.
- **Brev region** — default us-west-2 unless user prefers otherwise.
- **NVBugs MCP auth** — periodically goes 401; requires manual
  `claude mcp` reauth. Not blocking briefs (other sources keep
  working) but nags morning routine.

## Phase 4 — locked decisions

- Web UI dashboard at `src/agent_me/dashboard/` — **drafted
  2026-05-10**. Stack: Starlette + Jinja2 + Alpine.js + Tailwind
  CDN + sse-starlette. No Node, no bundler.
- **Public URL via Tailscale Funnel** (`<host>.<tailnet>.ts.net`).
  Free Personal tier, no bandwidth/request cap, no interstitial, no
  DNS to manage. Outbound-only from host. Supersedes earlier
  Brev-port-expose idea (Brev abandoned 2026-05-10 due to
  MaaS-MCP unreachability from external networks).
- Reads the bridge's SQLite (URI `mode=ro`) + tails
  `bridge.log`/`brief.log` over SSE. Never writes to bridge state.
- On-demand refresh per source: reuses `agent_me.scripts.daily_brief`
  fetcher/parser via in-process import (single `claude -p`
  subprocess per source, single-flight lock); does **not** post to
  Slack — bridge's 6am cron remains the only Slack-posting path.
- Auth: shared bearer token (`DASHBOARD_TOKEN` in `configs/.env`),
  signed-cookie session for browsers. Refuses to bind to non-loopback
  if token unset.
- The Slack bridge is **not modified** by Phase 4 — bridge keeps its
  SQLite write connection, its `claude -p` cwd
  (`~/.local/state/agent-me/chat-cwd`), its Socket Mode WebSocket.
  Dashboard is purely additive. Two new systemd `--user` units; the
  bridge unit is unchanged.

## Open questions / parking lot

- Memory architecture: keep auto-memory file-based or externalize to
  a DB the agent owns?
- Secrets management on Brev: scp-once vs 1Password CLI vs sops + age
  vs HashiCorp Vault. Current stop-gap = `~/agent-me-secrets.md` + scp.
- Audit log: log every action the agent takes for after-the-fact review.
- Dashboard auth: bearer token in URL vs Cloudflare Access vs simple basic auth.
