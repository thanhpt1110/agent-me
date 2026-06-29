# Design — Native dashboard Chat feature

Status: shipped on branch `big-update` (2026-06-26). Production unaffected.

A `/chat` page in the Starlette dashboard where each conversation is a persistent
Claude Code session running on this VM, streamed live over SSE. See the decision
log: [`discussions/2026-06-26-native-dashboard-chat.md`](../discussions/2026-06-26-native-dashboard-chat.md).

## Module layout — `src/agent_me/dashboard/chat/`

| File | Responsibility | SOLID |
|------|----------------|-------|
| `models.py` | Value types: `EventKind` (StrEnum), `make_event()`, `SessionMeta`, `TurnResult`. No behaviour. | SRP — schema is the only reason to change. |
| `config.py` | `ChatConfig` (frozen) + `from_env(repo_dir)` — the **only** `CHAT_*`/env reader. | DIP — config is injected, never read at use-time. |
| `store.py` | `SessionStore` Protocol + `DiskSessionStore` (`data/chat-sessions/<id>/{meta.json,transcript.jsonl}`). | ISP/LSP/OCP — swap to SQLite or a fake without touching callers. |
| `runner.py` | `ClaudeRunner` Protocol + `ClaudeCliRunner` (asyncio subprocess, NDJSON `stream-json` → events). | SRP/DIP — `FakeClaudeRunner` in tests; an SDK runner could replace it. |
| `jobs.py` | `ChatTurnJob` — live fan-out to SSE subscribers (bounded queues, 300-event replay buffer, 120s keepalive, monotonic `seq`). | SRP — mirrors the dashboard's `AutoSFAJob`. |
| `service.py` | `ChatService` — orchestrator; the only object route handlers touch. | SRP/DIP — all collaborators constructor-injected; fully unit-testable. |

`__init__.build_chat_service(repo_dir)` wires the production graph.

## Routes (thin handlers in `app.py`)
- `GET  /chat` — page (`templates/chat.html`)
- `GET/POST /api/chat/sessions` — list / create
- `GET/DELETE /api/chat/sessions/{id}` — meta+transcript / delete
- `PATCH /api/chat/sessions/{id}` — `{title}` rename, `{cwd}` folder picker, `{branch}` branch picker (409 if started/shared/own-tree/invalid)
- `GET  /api/chat/folders` — candidate spawn folders (folder picker)
- `GET  /api/chat/branches?cwd=…` — local git branches for a folder (branch picker)
- `POST /api/chat/sessions/{id}/message` — start a turn (202 / 400 empty / 404 / 409 busy)
- `POST /api/chat/sessions/{id}/cancel` — Stop generating
- `GET  /api/sse/chat/{id}` — `EventSourceResponse` stream

All non-exempt → gated by `AuthMiddleware`. No `EXEMPT_PATHS` change.

## CLI invocation
`claude -p <text> --output-format stream-json --verbose --dangerously-skip-permissions
[--resume <claude_session_id>] [--model M] [--add-dir D...]`, `cwd=<session cwd>`.
Reuses the host OAuth login (no API key). First turn captures the CLI's session id
from the `init`/`result` event; later turns pass it to `--resume`.

## SSE event schema (`event:<kind>`, `data:` JSON; each carries `seq`)
`snapshot` (status), `user`, `init` (session_id), `text`, `thinking`,
`tool_use` (name/input), `tool_result` (content/is_error, truncated at 8000),
`result` (final), `turn_end` (client closes), `error`, `timeout` (keepalive).

## Streaming / reconnect model
`stream(sid)` yields the full persisted transcript first, then live job events.
Both carry `seq`; the client clears + rebuilds on each `EventSource` (re)open and
dedups by `seq`, so transcript/buffer overlap and auto-reconnects don't duplicate.
A turn is owned by an `asyncio.Task`; closing the browser doesn't kill it.

## Persistence
`<repo>/data/chat-sessions/<uuid>/` (gitignored) — `meta.json` (title, cwd, model,
`claude_session_id`, timestamps, turns) + append-only `transcript.jsonl`. Busy lock
is in-memory (one turn per session → 409; reset on restart).

## Config (`configs/.env`, all optional)
`DEPLOY_ENV` (dev|production — DEV banner), `CHAT_DEFAULT_CWD`, `CHAT_ADD_DIRS`,
`CHAT_MODEL`, `CHAT_CLAUDE_BIN`, `CHAT_TURN_TIMEOUT_S`. Login gate inherited from
`DASHBOARD_TOKEN`/`DASHBOARD_TRUST_NETWORK` (no chat-specific auth).

## Deployment
`deploy/agent-me-dashboard-dev.service` → `uv run agent-me-dashboard --host 0.0.0.0
--port 8787` with `DEPLOY_ENV=dev`, `DASHBOARD_TRUST_NETWORK=1`. Production dashboard
stays on the A100 (:8765). Optional public URL: `deploy/agent-me-dashboard-dev-tunnel.service`.

## Frontend (`templates/chat.html`, Alpine.js)
Full-width messages; progressive-disclosure tool rows; collapsed thinking;
streaming caret; near-bottom autoscroll + jump-to-latest; markdown + copy-able
code; Stop control; empty-state chips; `role="log" aria-live="polite"` + sr-only
announcer; Enter-to-send / Shift+Enter newline (IME-safe). NVIDIA theme: green
accent only, **black text on green** buttons, JetBrains Mono code.

## Tests
`tests/test_chat.py` — service logic via `FakeClaudeRunner` (persist+seq, resume,
busy-lock, validation, stream replay) and HTTP via Starlette `TestClient`.

## Cross-surface session sync + paging (2026-06-28)
The `claude` CLI writes one `~/.claude/projects/<cwd-slug>/<id>.jsonl` per session for
**every** surface (web, Slack bridge, interactive terminal) — that dir is what
`claude --resume` lists. So the surfaces already share storage; the web just needed to
read it.

- `native_sessions.py::NativeSessionReader` — read-only view of that store:
  `list_sessions` (cached by mtime+size), `read_events`/`read_events_tail` (jsonl →
  web events), live/busy from `~/.claude/sessions/*.json` (+ pid-alive check).
- `ChatService.session_dicts()` merges web-store sessions with native ones (dedup by
  every CLI id a web session covers); `stream()`/`get_session` replay native sessions
  read-only; `_adopt_native` imports a terminal session's history once and `--resume`s it
  when the user continues it from the web. `_delete_native` + `purge_local_sessions`
  (now also removes the `<id>/` sidecar) make a web delete remove it from `claude --resume`.
- Env `CHAT_SHOW_NATIVE_SESSIONS` (default on) toggles the merge.

### Efficient transcript I/O (no whole-file reads on hot paths)
`DiskSessionStore` gained `tail(limit)` and `events_before(cursor, limit)` — byte-offset
reverse reads, O(limit) not O(file) (tail of a 20k-event/5MB transcript ≈ 1.6 ms vs ≈ 56 ms
for `transcript()`), plus `last_seq()`/`size()`. `stream()` replays only the tail, emits a
leading `snapshot` with the paging `cursor`, and tags every replayed event `replay:true`.
`GET /api/chat/sessions/{id}/history?before=<cursor>&limit=` powers scroll-up paging.

### Follow-up queueing (terminal-like)
Sending while a turn runs enqueues the message; `_drain` runs the queue on a single job and
emits one `turn_end` at the end. Queued user bubbles are flagged `queued`.

### Frontend fixes
`send()` POSTs the message **before** opening the SSE (so the server-side turn exists and the
stream takes the live path — fixes "old session shows no live progress"); the client ignores
`turn_end`/busy on `replay` events (a past turn's end must not close the live stream); scroll-up
"Load older" paging with DOM trim only while following at the bottom; sidebar origin badge +
ticking relative time + `.chat-run` spinner; `Ctrl+`` terminal toggle (`e.code` fallback; `Cmd+``
is OS-reserved on macOS); built-ins-first `/` menu (`/context`, `/usage`, …).

## Detached turn-runner — turns survive a dashboard restart (2026-06-29)
Web chat turns used to run as children of the dashboard process, so a `systemctl restart`
(deploy, or the agent restarting the dashboard) SIGTERM-killed the in-flight `claude`
(exit 143) and dropped the queued message. Now turns run in a **detached worker**:

- `turn_runner.py::TurnExecutor` + the `agent-me-chat-turn` console script. The dashboard
  `start_turn` only: `store.append_pending(sid, text)` (durable, flock'd) → `store.claim_run(sid)`
  (atomic, one worker per session) → `subprocess.Popen([... turn_runner], start_new_session=True)`
  (own process group). The unit is `KillMode=process`, so a dashboard restart leaves the worker
  running. `_spawn_worker` records the real `Popen.pid` in `meta.running_pid`.
- The worker drains `meta.pending` (pop on completion; keep on signal-interrupt for resume),
  writes USER/agent/`turn_end` events to the transcript, and clears `running_pid` when done.
- The dashboard **tails the transcript** (`_tail_external`, 0.25s) for live streaming — so it can
  restart and a reconnecting browser just re-tails. `is_busy`/`stream` use `running_pid` (pid-alive).
- `resume_pending()` (app lifespan startup) relaunches a worker for any session that still has
  queued work but no live worker (poison-guarded after 3 signal-interrupts).
- `cancel_turn` SIGTERMs the worker's process group (worker + its claude child) and clears the queue.
- A session live in a terminal (`_live_elsewhere`, via `~/.claude/sessions` pid-alive) is read-only
  from the web (resuming it would fork the running CLI session).

## Folder lock + context-% gauge (2026-06-29)
- **Folder lock**: a session's `claude --resume` cwd is pinned at create time, so the header
  folder picker only ever set the cwd for the *next* new chat. The picker now reflects the
  **current** chat's cwd (`selectedFolder` follows `selectSession`) and is `:disabled` + 🔒 once
  the chat has started — `service.folder_locked(meta)` = `claude_started || turns>0 || running ||
  live`. An unstarted chat can still be re-pointed: the `<select>` `@change` PATCHes `{cwd}` to
  `/api/chat/sessions/{id}`; `service.update_cwd` validates the dir and **refuses (409)** once
  locked, and the client reverts. `+ New` always opens a fresh, re-pointable chat.
- **Context-% gauge**: `claude_agent.runner._context_tokens` sums one API call's `usage`
  (`input + cache_read + cache_creation + output`) into the RESULT event's `context_tokens` and
  `TurnResult.context_tokens`. **Source = the LAST `assistant` message's `usage`, NOT the cumulative
  `result.usage`** — `result.usage` re-counts `cache_read_input_tokens` for every internal API call
  in the turn, so a multi-tool turn overstated context many-fold and pinned the gauge at 100% (e.g.
  6.07M for a 2-turn chat whose real context was ~190k). The last assistant call read the most
  context, so its usage is the true occupancy; `result.usage` is only a fallback when no assistant
  usage was seen. The worker persists it to `SessionMeta.context_tokens` (a 0 from a killed turn is
  skipped so the prior reading stays); it rides `public_dict` to the client, which renders `ctx N%`
  on the title over `config.context_window` (`CHAT_CONTEXT_WINDOW`, default 1,000,000). The client
  captures `context_tokens` from the live `result` event so the gauge updates per turn. A stale
  pre-fix `context_tokens` self-corrects on the session's next turn.

## Header/sidebar redesign (2026-06-29)
- The header badge is `folder@branch` (e.g. `agent-me@big-update`). The folder picker is no
  longer an always-visible header control: it shows **inline next to the title only while a chat is
  unstarted** (`x-show="currentMeta() && !folderLocked()"`), and disappears once the first prompt is
  sent → the started chat just shows the `folder@branch` chip + `ctx N%`. New chats are created from
  the **sidebar** ("+ Chat") defaulting to `default_cwd` (agent-me); the header picker re-points them
  via PATCH `{cwd}` until they start. There is no "+ New" / picker on the right anymore.
- `ctx N%` also shows per session in the **sidebar** (`ctxPctFor(s)`/`ctxTitleFor(s)`; the header's
  `contextPct()`/`ctxTitle()` delegate to them).
- **Stop** is an icon-only `.btn-danger.btn-icon`; **Terminal** is a `>_` `.btn-icon` toggling
  `.btn-primary`/`.btn-secondary` (was the "⌁ Terminal" text button). Buttons use the app.css
  `.btn*` component classes (the Tailwind `*-danger` utilities aren't in the compiled CSS).
- **Delete** uses an in-app confirm modal (`confirmDeleteId` → `performDelete()`), reusing the gate
  modal's styling, instead of `window.confirm`.
- **Origin badges** collapsed to `web` / `terminal` / `slack`: any native CLI session on the host is
  now `terminal` (the old `cli` vs `terminal` split — interactive vs headless `entrypoint` — only
  confused; `NativeSession.origin` returns `"terminal"`).

## Content-sized folder chip + per-folder branch picker (2026-06-29)
The `folder@branch` chip now reflects the **selected folder's real branch**, not a value hardcoded
at render time, and both picker selects size to their content.
- **Per-folder branch resolution.** `chat_branch` (rendered once from the dashboard repo's own HEAD —
  always `big-update`) is gone. `ChatService.branch_options(cwd)` → `{is_git, branches, current}`
  via git in *that* folder (`symbolic-ref --short HEAD` so an unborn branch shows its real name, not
  `HEAD`; `for-each-ref … refs/heads` sorted by committerdate, capped at `_MAX_BRANCHES=100`).
  Served by `GET /api/chat/branches?cwd=…`; the frontend caches results in `branchesByCwd` and reads
  them via `branchSuffix(cwd)` (chip) / `pickerIsGit()` / `pickerBranches()`. A non-git folder shows
  just the folder name (no `@`).
- **Content-sized selects.** The old fixed `max-w-[9rem]` is gone. Both `<select>`s use
  `:style="selectWidth(text)"` = `clamp(2rem, calc(Nch + 1.15rem), 14rem)` — content-hugging but
  clamped so a long folder/branch name can't blow out the `h-14` header — plus the `.chip-select`
  class in `app.css` (`appearance-none` + a custom SVG caret) so width is predictable instead of
  depending on the browser's native arrow box.
- **Branch checkout (unstarted only).** Selecting a branch PATCHes `{branch}` →
  `ChatService.checkout_branch`, which validates the target against the folder's own branch list (so
  it never reaches `git checkout` as a free-form arg) and runs in the session's cwd. Refused (→ 409)
  once the chat has started.
- **Shared/own-tree guard.** A checkout mutates the *whole working tree*, so `_branch_switch_allowed`
  refuses it when `meta.cwd == default_cwd` (the agent's own `~/agent-me` — keeps it on `big-update`,
  never flipped under the live dashboard/bridge) or when **another session is pinned to the same
  folder**. The client mirrors this with `branchReadOnly()` and renders those branches as a read-only
  `@branch` label instead of a dropdown. The locked chip shows the folder's current HEAD; with this
  guard, a dedicated folder's HEAD == the branch that session runs on (nothing else flips it).
- Tests: `tests/test_chat.py::{test_branch_options_and_checkout, test_branch_options_handles_unborn_branch,
  test_branch_checkout_refused_for_shared_or_own_tree}`.
