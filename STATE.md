# agent-me — Current State

_Last updated: 2026-05-10 by Claude (Opus 4.7) — end of multi-hour session._

## Phase

**Phase 2a complete + daily-brief shipped + hybrid PA infrastructure ready.**
Bridge is fully Python+uv (no JS), live on macOS dev host. Daily/weekly/
monthly brief works end-to-end via `/brief` slash, plain-text trigger, or
Block Kit button. Morning routine fires daily at 6am Vietnam time
posting an MCP-status DM with reauth + brief buttons. PA CLI (NVIDIA's
Personal Assistant) installed and partially configured — `MCP_CLI=pa`
swap is wired through bridge + brief but **not yet validated end-to-end**;
deferred to next session per user decision (perf trade-off vs claude
needs more eval). Next: Phase 3 (Brev deploy) or revisit PA.

## Decisions locked

| Topic | Choice |
|---|---|
| Runtime host | Brev cloud CPU instance (24/7) — Phase 3 |
| Primary interface | Personal Slack workspace (Socket Mode bridge) |
| Config repo | Personal GitHub, **public template** (`thanhpt1110/agent-me`) |
| Default model | Claude Opus 4.7 (1M ctx); also what PA uses under the hood |
| Git identity | `includeIf` per-host: github.com → personal, default → NVIDIA |
| License | MIT |
| Slack sandboxing | Review-by-default + per-thread auto-approve toggle (Phase 2b) |
| State store path | `${AGENT_ME_STATE_DIR:-${XDG_STATE_HOME:-~/.local/state}/agent-me}` |
| Streaming UX | Hybrid: 🔄 placeholder → progress steps → final `chat.update` |
| Brief MCP backend | `MCP_CLI` env var: `claude` (default) or `pa` (NVIDIA PA CLI) |
| File logging | structlog → console (pretty) + rotating JSON file `bridge.log` (10MB×5) |
| Morning routine | 6am Vietnam time (`Asia/Ho_Chi_Minh`); thread-rooted DM |
| Secrets vault | `~/agent-me-secrets.md` outside repo, chmod 600 |

## Done

- [x] Project + scaffold + bypassPermissions
- [x] **GitHub repo public template:** https://github.com/thanhpt1110/agent-me
- [x] **Bridge live (Python + slack-bolt async)** — DM, app_mention, 5 native slash commands (`/brief /mcp /reauth /version /whoami /help`), text-intercept slash, 25 plain-text shortcuts (`brief week` / `mcp` / `reauth` / etc.), Block Kit interactive buttons (Slack Interactivity setup verified by user)
- [x] **MCP re-auth helper** (`uv run agent-me-reauth`) — pty + auto-open browser tabs, 9 bug iterations resolved (mention-prefix, bracketed paste, line-wrap, ANSI-strip, infinite-loop dedupe by client_id)
- [x] **Daily-brief script** (`uv run agent-me-brief --period day|week|month`) — Jira/GitLab/GitHub/NVBugs/Confluence + email-skipped; grouped by project/repo/space; priority table; live placeholder updates (Step 1/3 → 2/3 → 3/3 → final blocks); Block Kit with [Refresh][Weekly][Monthly][Reauth] buttons
- [x] **Morning routine** — daily 6am VN-time DM, MCP probe, post-reauth menu in thread
- [x] **File logging** — `~/.local/state/agent-me/bridge.log` (rotating JSON) + `brief.log` (append, captures crash stderr)
- [x] **`tail-log.sh`** + **`kill-bridge.sh`** helper scripts
- [x] **Hybrid PA / Claude infrastructure** — `MCP_CLI=pa` swap in brief AND bridge (`/mcp` → `pa auth status`, `/reauth` → `pa login`, morning probe via PA), env+CLI fallback to claude
- [x] **PA CLI installed** + Slack `client_id`/`client_secret` added to `~/.pa/.env` (Glean skipped — credentials require admin)
- [x] **Secrets vault** at `~/agent-me-secrets.md` (outside repo, chmod 600) — Slack/GitHub/NVIDIA-Inference/Jira PATs documented for Brev migration; rotation reminder included
- [x] All Phase 2a docs + ~14 commits pushed

## Validated empirically

- ✅ PA returns clean JSON on the brief-style prompt (verified live with `pa -p`).
- ✅ PA uses `aws/anthropic/bedrock-claude-opus-4-7` under the hood — same model class.
- ✅ PA has automatic MCP retry on first-hit timeout.
- ⚠️ PA CLI cold-start adds ~5-15s vs Claude Code per-spawn (desktop app feels faster because long-lived process; CLI is fresh-spawn).
- ⏳ PA's MCP-auth retention >24h vs Claude's daily expire — **still to validate** (need to leave PA mode on for ~48h and observe).

## Roadmap (next session priorities)

1. **Validate PA mode end-to-end** — run `MCP_CLI=pa uv run agent-me-brief --period day --dry-run` once, compare item count + timing vs claude mode. If acceptable, switch bridge to PA.
2. **Phase 3 — Brev deploy** (highest leverage). Always-on host means morning routine, future cron jobs, and PA auth retention all become reliable. SSH-port-forward pattern for PA login from Mac browser. Document Brev provisioning + systemd unit for bridge + timer for brief.
3. **Phase 2b — review-before-execute approval gate.** Slack-button gating for write tools. Design ready in `design/approval-hook-design.md` (file-system semaphore). Open question still: PreToolUse hook stays sync-blocked? Investigate before coding.
4. **Phase 4 — web dashboard** at `src/agent_me/dashboard/` (starlette + SSE) on Brev port-expose. Reads same SQLite state DB the bridge writes to.
5. **Persistent PA REPL pattern** (deferred) — instead of cold-spawn `pa -p` per query, keep a long-lived `pa` REPL process in the bridge and pipe queries via stdin (similar to reauth helper architecture). Eliminates cold-start tax. Worth doing if PA mode wins on auth retention.

## Open research / unresolved

- **PA daemon mode?** — `pa --help` to check if there's a built-in always-on/server mode that would make `pa -p` invocations warm.
- **GLEAN_CLIENT_SECRET** — public docs don't say where to get it; user has not pursued. Skipped because Glean is reachable via Claude Code MCP independently.
- **Action interception mechanism** for Phase 2b: PreToolUse hook (cleanest) vs stream-parse (invasive). Will investigate hook-blocking semantics first.
- **Brev region** — default us-west-2 unless user prefers otherwise.

## Phase 4 — locked decisions (deferred to after bridge stable)

- Web UI dashboard at `src/agent_me/dashboard/` (Python equivalent — likely Starlette + SSE; not Express).
- Public URL via Brev port-expose (`*.brev.dev`); URL may rotate per instance restart.
- Drops the historical "why not PA" framing — we now have hybrid infra; the dashboard reads the bridge's SQLite + tails brief.log/bridge.log.

## Open questions / parking lot

- Memory architecture: keep auto-memory file-based or externalize to a DB the agent owns?
- Secrets management on Brev: scp-once vs 1Password CLI vs sops + age vs HashiCorp Vault. Current stop-gap = `~/agent-me-secrets.md` + scp.
- Audit log: log every action the agent takes for after-the-fact review.
- Dashboard auth: bearer token in URL vs Cloudflare Access vs simple basic auth.
- Persistent PA REPL pattern (see roadmap #5).
