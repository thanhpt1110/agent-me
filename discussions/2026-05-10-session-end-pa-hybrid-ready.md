# Session end — PA hybrid infrastructure ready, end-to-end validation deferred

_2026-05-10 · marathon session, ~14 commits._

This file is the load-bearing handoff for any future Claude session that
arrives at `~/agent-me/`. Read alongside `CLAUDE.md` + `STATE.md` +
`discussions/2026-05-10-phase-2a-complete-daily-brief-kickoff.md`.

## What landed today (high-level)

1. **Bridge ported to Python**, Node `services/slack-bridge/` deleted.
   Run with `uv run agent-me-bridge`. Full feature parity + new `/reauth`
   slash + auto-discovery of operator user_id.
2. **Daily/weekly/monthly brief** — `uv run agent-me-brief --period
   {day,week,month}`. Posts a placeholder DM that updates through 3
   progress steps then becomes the final Block Kit brief. Sources
   grouped by project/repo/space within each infra (Jira → NGC/DRIVE/…
   subgroups, GitLab → per-repo, etc.). Priority table at top.
3. **Morning routine at 6am Vietnam time** — bridge-internal asyncio
   task. Posts a date-headed thread starter; replies in-thread with
   either a Reauth prompt (if MCPs stale) or an action menu (if
   healthy). Reauth-now button → spawn `pa login` (PA mode) or
   agent-me-reauth (claude mode); follow-up replies in same thread
   carry the menu of [Daily / Weekly / Monthly / Verify MCPs / Help].
4. **Hybrid PA + Claude Code architecture** via `MCP_CLI` env var.
   Bridge AND brief swap at the subprocess layer:
     - `MCP_CLI=claude` (default): bridge uses `claude mcp list` /
       agent-me-reauth helper / `claude -p --allowedTools` for brief.
     - `MCP_CLI=pa`: bridge uses `pa auth status` / `pa login` / `pa -p`
       for brief.
   Bridge's `bridge_starting` log line includes `mcp_cli=<backend>` so
   the active path is obvious.
5. **PA CLI installed** + Slack credentials added to PA's `.env`
   (`~/.pa/.env`, chmod 600). Glean skipped (admin-gated secret).
6. **Secrets vault** at `~/agent-me-secrets.md` (outside repo,
   chmod 600). Includes Slack client/secret, GitHub PAT, NVIDIA
   Inference Hub key, Jira PAT — with rotation reminders and Brev
   migration recipe (`scp` once, apply, delete).
7. **Operational helpers**:
     - `scripts/tail-log.sh` — pretty `tail -f` for the JSON file log.
     - `scripts/kill-bridge.sh` — SIGTERM then SIGKILL fallback for
       the rare case Ctrl-C in the bridge terminal hangs (force-exit
       backstop is also wired into the bridge itself: 8s after first
       signal it `os._exit(1)`s).
8. **File logging** — structlog → console (pretty) + JSON
   RotatingFileHandler at `~/.local/state/agent-me/bridge.log` (10 MB
   × 5 files). brief.log captures brief subprocess stdout/stderr so
   crashes are visible (was a real bug earlier — brief's `import time`
   miss + duplicate `action_id` in Block Kit were both invisible
   until file logging caught them).
9. **9 reauth-helper bug iterations** all resolved — pty winsize
   (4096 cols), CSI-cursor stitching, ANSI strip, camelCase tail trim
   (mcp|Once → mcp), client_id dedupe (was infinite-loop opening
   tabs), bracketed-paste split write, NVIDIA-org-policy-aware
   `--dangerously-skip-permissions` (vs. blocked
   `--permission-mode bypassPermissions`).
10. **Plain-text command shortcuts in DM** — 25 keywords (`brief`,
    `brief week`, `weekly`, `mcp`, `status`, `reauth`, `auth`, `help`,
    `?`, `version`, `whoami`, `id`, …) intercept BEFORE Claude is
    spawned. Exact-match only so "help me debug this" still flows to
    Claude.
11. **Block Kit buttons** in `/help`, brief output, morning thread —
    `[📅 Daily brief] [📊 Weekly] [📆 Monthly] [🔄 MCP] [🔧 Reauth]`.
    Action handlers reply in-thread for organized morning conversations.
    User confirmed Slack Interactivity & Shortcuts → ON in app config
    (required for buttons to fire).

## Decisions made this session

- **Auto cron at 6am dropped** — replaced with on-demand pattern
  (DM/buttons/slash) because MCP tokens expire daily and a strict
  cron would silently fail mornings after token expiry. Morning
  routine still fires at 6am but only POSTS a status nudge with
  buttons; user clicks to actually run a brief.
- **PA hybrid: opt-in via env var, not default.** PA mode wired
  through but `MCP_CLI` defaults to `claude` until end-to-end
  validation completes. User decision: defer PA-vs-claude perf
  comparison to next session because PA CLI cold-start adds 5–15s
  per invocation vs Claude Code's already-hot install. Auth-retention
  win > perf cost is the hypothesis but not yet measured.
- **Brief format**: items grouped by project/repo/space within each
  source; not flat-list. Priority table sorted by deadline at top.
  Auto-split sections if >2900 chars. Validated with synthetic 14-item
  dataset across 5 sources and 5 groups.
- **Group fallback**: when claude/pa returns no `group` field,
  derive from item key prefix (Jira "NGC-789" → group "NGC") so the
  format never collapses to "uncategorized" by accident.

## Empirical findings (PA testing in-session)

```
$ pa -p 'Use mcp__maas-jira__jira_search to find 1 issue assigned to me. Return ONLY a JSON array...'
⏺
  ▸ jira_search
  [MCPManager] safeCall attempt 1/2 failed for 'jira': The operation was aborted due to timeout
  ✓ jira_search
[{"key": "NGC-60742", "summary": "[Transition Dry Run][QA Test Tracking]Retail Shopping Assistant", "status": "Done"}]
  39.1s  ·  24,020→50 tokens  ·  $0.12  ·  aws/anthropic/bedrock-claude-opus-4-7
```

Three takeaways:
1. PA returns clean JSON when prompted properly — our brief prompt
   format works without changes.
2. PA is `aws/anthropic/bedrock-claude-opus-4-7` under the hood —
   same model class as Claude Code's Opus 4.7. Quality should be
   identical.
3. PA has built-in MCP retry (`safeCall attempt 1/2`) — robustness
   advantage over claude raw, at the cost of slower first-hit.

## What's NOT done (next session starts here)

1. **End-to-end PA validation**: run
     ```bash
     time MCP_CLI=pa uv run agent-me-brief --period day --dry-run
     time uv run agent-me-brief --period day --dry-run
     ```
   side by side. Compare item count, timing, and look at brief.log
   for any parse errors. Decision criterion: if PA total time is
   within 2× of claude AND item coverage matches, switch
   `MCP_CLI=pa` permanently.
2. **PA auth retention validation** — leave PA mode on for ~48h, see
   whether `pa auth status` still shows ✓ for maas-* services. If yes
   → strong reason to commit to PA. If no → stay on claude with
   reauth flow.
3. **Phase 3 deploy to Brev** — provision instance, install uv +
   claude CLI + pa CLI, scp the secrets vault once, systemd unit for
   `agent-me-bridge` + timer for brief, document SSH port-forward
   pattern for PA login (`design/mcp-authentication.md` already has
   the equivalent for claude reauth).
4. **Persistent PA REPL** (Roadmap #5) — replace cold-spawn `pa -p`
   with long-lived `pa` REPL pty session (analogous to reauth helper
   architecture). Worth doing only if PA mode wins on auth retention.

## Slack app config — current state on user's workspace

| Setting | Status |
|---|---|
| Bot token / App token / Signing secret | ✓ in `~/agent-me/configs/.env` |
| Operator user_id | ✓ pinned via `SLACK_ALLOWED_USER_ID=U0B3LLSD2M6` |
| App Home → Messages tab | ✓ enabled (DM works) |
| Interactivity & Shortcuts | ✓ enabled (buttons work — user confirmed) |
| Slash commands registered | `/help`, `/mcp`, `/reauth`, `/version`, `/whoami`, `/brief` (text-intercept fallback works for any unregistered slash) |

## Pointers a fresh session needs

- Repo: `~/agent-me/` (https://github.com/thanhpt1110/agent-me — public template)
- Bridge entry: `uv run agent-me-bridge` (foreground; `~/agent-me/scripts/kill-bridge.sh` to stop)
- Brief entry: `uv run agent-me-brief [--period day|week|month] [--dry-run] [--channel C…]`
- Reauth helper entry: `uv run agent-me-reauth [--limit N] [--debug-bytes /tmp/pty.bin]`
- Logs: `~/.local/state/agent-me/{bridge,brief}.log` — pretty tail via `~/agent-me/scripts/tail-log.sh`
- State DB: `~/.local/state/agent-me/state.db` (SQLite WAL)
- Secrets vault: `~/agent-me-secrets.md` (outside repo, chmod 600)
- PA: `pa auth status` / `pa login` / `pa -p "…"`; PA's own `.env` at `~/.pa/.env`
- Mode swap: `MCP_CLI=claude|pa` in `~/agent-me/configs/.env`
- Backup of conversation context: this file + `STATE.md` + `CLAUDE.md` + the prior `2026-05-10-phase-2a-complete-daily-brief-kickoff.md` discussion log + auto-memory at `~/.claude/projects/-Users-thaphan/memory/project_agent_me.md`.

## When you (next-session-Claude) arrive

1. Read this file + STATE.md + CLAUDE.md (in that order).
2. Skim `discussions/2026-05-10-phase-2a-complete-daily-brief-kickoff.md`
   for full bug-history context (especially reauth helper).
3. Confirm with user which next-up item to pick: PA validation,
   Phase 3 Brev, Phase 2b approval gate, persistent PA REPL.
4. Don't re-litigate locked decisions in STATE.md unless user
   reopens them.
