# 2026-05-10 (night) — Phase 2b approval gate + 3-log dashboard viewer

_Session run via the Cursor `Task` tool: 3 explore subagents (research)
+ 1 generalPurpose subagent (dashboard log viewer) + parent (Phase 2b
backend). Work parallelised so research + implementation overlap._

## Why this session existed

User asked to (a) implement Phase 2b approval gate, (b) extend the
dashboard with three log viewports — server git poll, Slack
interactions, Claude session traces — and (c) "fan out as many
agents as possible" to do it efficiently. So we did.

## Fan-out shape

```
Parent (Cursor agent)
├── Subagent A (explore, bg) — Phase 2b implementation spec
├── Subagent B (explore, bg) — log infrastructure audit + matrix
├── Subagent C (explore, bg) — Claude session JSONL format + path resolver
└── Subagent D (generalPurpose, bg) — dashboard 3-log viewer end-to-end
```

A/B/C ran first to gather context. Their output drove the synthesis
that informed (a) my Phase 2b implementation in `slack_bridge/app.py`
and (b) D's prompt for the dashboard log viewer. D ran in parallel
with my Phase 2b work; we converged at test/commit time.

The parent never read entire 1300-line bridge module twice — each
subagent did its own targeted reads. Total wall-clock for the four
threads was ~one parent thread's worth, instead of four sequential
ones.

## What landed

### Phase 2b backend (parent → `slack_bridge/`)

- `src/agent_me/slack_bridge/approvals.py` — new module:
  - File-system semaphore at `${STATE_DIR}/approvals/{requests,
    decisions,archive}/`.
  - `bootstrap_hooks(chat_cwd, state_dir)` writes the PreToolUse
    hook script + `.claude/settings.json` into `CHAT_CWD/.claude/`
    on bridge startup. Idempotent so a fresh state dir on a new host
    works without manual setup.
  - `write_decision(...)` atomically writes the decision file (tempfile
    + os.replace) so the hook can never read a half-written JSON.
  - `approval_loop(...)` polls requests/, dispatches new ones,
    expires stale rows. Coalesces with DB to skip already-resolved
    `tool_use_id`s after a bridge restart.
  - DB CRUD + per-thread auto-approve toggle (reuses existing
    `threads.auto_approve` column).
  - Slack block formatter for the approval prompt — three buttons:
    Approve, Reject, Auto-approve this thread.
- `src/agent_me/slack_bridge/app.py` — wired:
  - Schema migration `_migrate_pending_approvals()` runs on every
    boot. Introspects `PRAGMA table_info` and `ALTER TABLE`s any
    missing Phase 2b columns. Idempotent. Pre-2b DBs upgrade
    silently.
  - New constants `PHASE_2B_ALLOWED_TOOLS` + `APPROVAL_GATE_ON`. With
    `APPROVAL_GATE=1` in env, the chat path uses the wider allow-list
    (write tools included) so the hook gets a chance to fire. With
    `APPROVAL_GATE=0` (default), Phase 2a behaviour is bit-for-bit
    unchanged.
  - `CLAUDE_TIMEOUT_S` auto-bumps 5 → 12 min when gate on, so the
    subprocess doesn't get killed mid-approval.
  - `_post_approval_request()` handles new requests: per-thread
    auto-approve fast path, else post a Slack DM with the three
    buttons.
  - `_resolve_approval_from_button()` is the shared path for
    Approve/Reject/Auto-thread handlers: writes the decision file
    *before* updating Slack (so Claude unblocks ASAP), updates the
    DB, then edits the original message in-place to disable buttons.
  - Three new `@app.action` handlers: `approval_approve`,
    `approval_reject`, `approval_auto_thread`.
  - `main_async` spawns `approval_loop` alongside health/morning
    loops when gate on, cancels it on shutdown alongside the others.
- `tests/test_approvals.py` — 18 tests covering schema, bootstrap,
  decision-file writer, request scanner, archive, DB CRUD round-trip,
  per-thread toggle, expiration, Slack block shape, and approval-loop
  dispatch (incl. the "skip already-resolved after restart" case).

### Dashboard 3-log viewer (subagent D → `dashboard/`)

- `src/agent_me/dashboard/log_sources.py` — new module:
  - `SLACK_INTERACTION_EVENTS` allowlist (parent extended it post-D
    with the new `approval_*` events).
  - `tail_journal_unit(unit, …)` spawns `journalctl --user -u <unit>
    -f`. Subprocess lifecycle handled (terminate → kill escalation,
    no zombies left after SSE disconnect).
  - `tail_bridge_slack_filtered(…)` wraps `StateReader.tail_logs`
    so it inherits the inode/size rotation handling, then filters
    by event allowlist.
  - `resolve_session_jsonl_path(session_id)` — sanitizes
    `STATE_DIR/chat-cwd` to Claude's project-dir form, then a glob
    fallback for Claude Code sanitizer drift across versions.
  - `tail_session_jsonl(session_id, …)` — partial-line-safe (buffers
    bytes, only emits on `\n` boundaries; protects against polling
    mid-write).
- `src/agent_me/dashboard/app.py` — three new SSE endpoints
  (`/api/sse/logs/{watcher,slack,session}`) + `/logs` page route.
- `src/agent_me/dashboard/templates/logs.html` — 3-tab UI (Alpine.js
  state per pane: live lines, autoScroll toggle, clear button).
  Session pane has a `<select>` populated from `recent_threads()`
  with a non-null `session_id`.
- `src/agent_me/dashboard/templates/partials/nav.html` — added
  `📜 Logs` pill between Source tabs and `⚙ Ops`.
- `tests/test_log_sources.py` — 16 tests covering allowlist
  filtering, partial-line buffering, sanitizer round-trip, journalctl
  subprocess error paths.

## Numbers

- 68 tests pass total (was 52 → 34 + 18 approvals + 16 log_sources)
- ruff clean across all touched dashboard / test files
- bridge.app.py: 9 lint warnings (down from 13 pre-existing — auto-fix
  caught some old issues, no new ones introduced)
- live boot smoke: `/healthz` 200, `/logs` 200, `/api/state` returns
  7 snapshots, all auth paths still working

## Trade-offs / decisions taken

### Approval gate: file-system semaphore over HTTP/socket
The design doc gave us four IPC options. Picked file-system semaphore
because:
- The hook is a bash script that already knows how to read files;
  curl + a localhost port adds attack surface and a moving part.
- Atomic writes via tempfile + rename avoid hook reading partials.
- Concurrent tool calls have unique `tool_use_id`s from Claude Code,
  so dirs never collide.
- Cost: poll latency ~1s, invisible against a human round-trip.

### Hook lives in CHAT_CWD/.claude/, not REPO_DIR/.claude/
The bridge spawns claude with `cwd=CHAT_CWD`
(`~/.local/state/agent-me/chat-cwd`), and Claude Code reads
`$CWD/.claude/settings.json`. The repo's existing `.claude/settings.json`
(bypassPermissions for dev sessions) doesn't affect Slack chat.
`bootstrap_hooks()` writes the chat-cwd settings file on every bridge
startup so it's idempotent + works on fresh hosts.

### Per-thread auto-approve toggle reuses existing column
`threads.auto_approve` was already in the DB schema (added back in
Phase 2a as a hook for "this thread is trusted"). Phase 2b finally
gives it a meaning: when set, `_post_approval_request` short-circuits
to `allow` without prompting Slack. Operator turns it on by clicking
the third button on any approval prompt in that thread.

### Off by default
`APPROVAL_GATE=1` env var to enable. With it off, the bridge behaves
exactly like Phase 2a (write tools blocked outright). This makes the
Colossus rollout safe — install Phase 2b code, leave gate off, flip
the env var when ready.

### `defer` mode considered, not used in v1
Subagent A flagged that Claude Code 2.1.89+ supports `permissionDecision:
"defer"` which exits the subprocess with `stop_reason: "tool_deferred"`,
letting `claude -p --resume <session>` re-fire the hook later. This
would avoid holding a subprocess open across long approvals. We didn't
use it because:
- File-system semaphore is portable across Claude Code versions.
- v1 timeouts (12 min) are plenty for human approval.
- defer mode has the constraint "single tool per turn" which would
  break parallel batches Claude sometimes spawns.
Captured as a future-work option in the module docstring.

### FE: Jinja+Alpine still
Stuck with the existing stack. Logs page is just three tabs of
EventSource + line buffer; no need to escalate to a SPA framework.

## What was hard

1. **Subagent D's transcript file appeared stalled** at one assistant
   message (~17:00) for 8+ minutes. Investigation showed D was
   actively writing files (`log_sources.py`, `logs.html`,
   `test_log_sources.py`) the whole time — the transcript JSONL
   gets one combined update at the end of the run, not per tool
   call. Lesson: `ls -la` on the workspace tells you more about
   subagent progress than the transcript file.

2. **My state_reader.py vs D's log_sources.py divergence.** While D
   was background-running, I mistakenly added a parallel
   implementation of `tail_journal_unit` etc. into `state_reader.py`
   to be safe. When D's work surfaced, I had to revert mine. Cost:
   one round-trip of churn. Mitigation in future: when fanning out
   for a clearly-bounded task, *don't* parallel-implement the same
   thing in the parent — let the subagent own it.

3. **Schema migration on existing DBs.** SQLite `ALTER TABLE` doesn't
   support `IF NOT EXISTS` for columns, so the migration helper
   introspects via `PRAGMA table_info` and adds only what's missing.
   Test fixture in `tests/test_approvals.py` includes the post-2b
   shape so the test always sees the new columns; the bridge's
   `_migrate_pending_approvals()` proves the migration works on
   pre-2b DBs.

## What's deliberately not in this session

- **No deploy.** The Phase 2b code is committed but `APPROVAL_GATE=0`
  by default. Operator will flip it after Phase 3 stabilises on
  Colossus and a manual smoke-test confirms the hook fires.
- **No safe-Bash auto-allow logic** (e.g. `ls`, `git status` skipping
  approval). Phase 2b v1 asks for every write tool. Refinement
  belongs in a Phase 2b.1 round if approval fatigue shows up.
- **No MCP-server-specific approval rules.** All write MCPs flow
  through the same hook matcher. Per-server logic is straightforward
  to add later in `approvals.HOOK_MATCHER`.
- **No audit-log export.** `pending_approvals` already records every
  decision + reason + actor + auto flag. Dashboard surfaces them in
  the existing Ops panel; export comes if/when needed.
