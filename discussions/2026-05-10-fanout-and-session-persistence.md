# Fan-out brief + Slack session persistence

_2026-05-10 · long session, big delta._

Two threads of work today, both shipped:

## 1. Brief: single-prompt → 7-subagent fan-out

Old: one giant `claude -p` call asking claude to query Jira, GitLab,
NVBugs, Confluence, GitHub in one prompt. ~1700 tokens, single-process,
serial MCP calls. Wall-clock 60–230s. Output sometimes hit Slack's
3000-char/section limit.

New: Python orchestrator spawns 7 subagents in parallel, one per
source, each scoped to that server's `mcp__maas-<X>__*` tool set:

```
post root header DM
asyncio.gather:
    jira       (claude -p, only mcp__maas-jira__*)
    gitlab     (claude -p, only mcp__maas-gitlab__*)
    confluence (claude -p, only mcp__maas-confluence__*)
    nvbugs     (claude -p, only mcp__maas-nvbugs__*)
    slack      (claude -p, only mcp__maas-slack__*)
    outlook    (claude -p, only mcp__maas-outlook__*)
    github     (gh CLI directly)
each subagent posts ONE threaded reply when it finishes
final priority synthesis posted as last threaded reply
root header updated with summary stats + buttons
```

First fan-out dry-run with all 5 healthy MCPs: 39s wall-clock vs the
64–230s the old single-prompt took. Per-source isolation means a slow
Confluence call doesn't hold up Jira; per-source allowedTools means
each subagent's context only loads the tools it needs.

### Required two new MCPs

The MaaS catalog has Slack and Outlook MCPs that we hadn't added yet
(no `claude mcp` defaults). Wired both into the fan-out + setup
script. Outlook authenticated cleanly. Slack got stuck because…

### Bug: project-local vs user-scope MCPs

`claude mcp add` defaults to `--scope local` (project-specific, stored
under `.projects.<cwd>.mcpServers` in `~/.claude.json`). The other 15
MCPs were already at user scope (global). The auth-helper
(`agent-me-reauth`) extracted Slack/Outlook URLs fine on the regex
level but the auto-open didn't fire — the OAuth flow for project-local
servers behaves subtly differently. Migrating both to user scope
(`claude mcp remove` → `claude mcp add --scope user`) made them
behave identically to the older Azure-auth servers.

`scripts/setup-mcps.sh` now passes `--scope user` always so future
setups don't repeat this mistake.

### Reauth helper false-positive trim

Separately, the helper's URL-extractor had a stale heuristic: any
`[a-z][A-Z]` boundary past position 200 with no `&?=` in the next
40 chars triggered a "trim trailing prose" cut. Slack's URL has long
base64url state values (`Cs7EuCAJrhQ8VxMiqROTRuDr_9Yye0ay-VVdS31uQXg`)
where the next param is more than 40 chars away — false positive.
Fixed by checking the *entire* rest of the URL for query syntax, not
a 40-char tail. The original use case (English glue like "Once they
complete…" with no separator) still trims correctly because there's
no `&?=` *anywhere* in the prose tail.

## 2. Slack DM ↔ Claude Code session persistence

Old: every Slack DM message was `claude -p <text>`, no continuity.
The bridge stored `messages` per `thread_ts` but never read them
back. Multi-turn chat in Slack didn't work.

New: `claude_sessions` table maps `thread_ts → session_id`. Bridge
calls `claude -p --output-format json --resume <id>` to continue an
existing thread, or omits `--resume` for a new one. Cache hits jump
from 0 → 76k tokens on turn 2 in a smoke test (i.e. claude re-reads
the system prompt and tool catalogs from cache instead of paying full
freight).

`SessionExpired` exception detects "No conversation found with
session ID: …" specifically and falls back to a fresh session. So if
the on-disk session file ever goes away (claude cleanup, project path
moved), the bridge degrades to "lose continuity once" rather than
"crash on the user."

`/reset` (typed in a thread) drops the bridge's pointer; next message
starts fresh. Plain-text shortcuts: `reset`, `clear`, `new`, `new chat`,
`forget`. No native Slack slash command for `/reset` because Slack
slash commands aren't thread-aware (no `thread_ts` in the payload).

Design + tradeoffs: `design/session-persistence.md`.

## Setup automation

Codified everything we use into `scripts/setup-mcps.sh` (idempotent
register of all 17 MaaS MCP servers via `claude mcp add --scope user`)
and `scripts/bootstrap.sh` (full first-time setup wrapper:
prerequisites check → `uv sync` → seed `configs/.env` → setup-mcps →
print interactive next steps).

`design/setup-on-fresh-host.md` is the long form of "what bootstrap.sh
does and why," including Brev-specific notes (systemd unit, SSH
port-forward for OAuth, daily token sync recipe).

## Files touched

- `src/agent_me/scripts/daily_brief.py` — full fan-out rewrite
- `src/agent_me/slack_bridge/app.py` — session persistence
- `src/agent_me/scripts/reauth_mcps.py` — trim heuristic fix +
  per-call gap 12s → 20s
- `scripts/bootstrap.sh` — rewritten from TODO stub
- `scripts/setup-mcps.sh` — new, idempotent MCP register
- `design/session-persistence.md` — new
- `design/setup-on-fresh-host.md` — new
- `design/maas-mcp-catalog.md` — new (MaaS catalog reference)
- `README.md` — Quickstart points at bootstrap.sh
- `CLAUDE.md` — bootstrap reference
- `STATE.md` — current state (will update separately)

## Verified

- ✅ Brief dry-run: 39s wall-clock for 7 subagents (vs 60–230s single)
- ✅ Bridge module imports clean; new schema applied
- ✅ Session resume: turn 2 sees turn 1's content (76k cache hits)
- ✅ Bogus session id → SessionExpired raised, recoverable
- ✅ `/reset` clears row; next call starts fresh
- ✅ `setup-mcps.sh` idempotent (17/17 already present)
- ✅ Reauth helper extracts Slack URL correctly post-fix
- ⏳ Slack DM end-to-end multi-turn: user verifies after restart

## Next session

User will manually test multi-turn chat in Slack. After that:
prompt-tuning the brief (user-driven), Phase 3 Brev deploy, Phase 2b
approval gate.
