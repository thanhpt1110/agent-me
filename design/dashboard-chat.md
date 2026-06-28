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
