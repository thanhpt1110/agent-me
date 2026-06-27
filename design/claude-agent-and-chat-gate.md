# Shared claude engine, Slack→claude bridge, and the /chat gate

Architecture for the 2026-06-27 codex→claude migration on branch `big-update`.

## Shared engine — `src/agent_me/claude_agent/`
- `models.py` — `EventKind`, `make_event`, `TurnResult`, `now_ms`.
- `runner.py` — `ClaudeAgentRunner` (impl) + `ClaudeRunner` (Protocol) + `RunnerConfig`.
  `run_turn(...)` spawns `claude -p --output-format stream-json --verbose
  --dangerously-skip-permissions [--session-id|--resume] [--model] [--add-dir]
  [--allowedTools] [--append-system-prompt] [--permission-mode]`, streams NDJSON,
  maps each line to an `EventKind` event via a sync `on_event` callback, and
  returns a `TurnResult` (final text, session id, error/exit/stderr). Owns its
  process group (`start_new_session=True`) so terminate reaches child tools.

Consumers:
- **dashboard chat** — `dashboard/chat/runner.py` and `models.py` re-export the
  engine; `ChatService`/store/jobs are unchanged.
- **Slack bridge** — `slack_bridge/claude_agent.py` maps engine events onto the
  bridge's progress `state` dict (see _Live progress_ below).
- **brief** — `scripts/daily_brief.py` `claude_fetcher` runs one read-only turn
  per source with `allowed_tools` scoped to a `maas-*` MCP glob.

## Slack bridge backend (`slack_bridge/app.py`)
- `AGENT_BACKEND` (default `claude`; `codex` = legacy). `spawn_agent` dispatches
  the interactive path; `spawn_write_agent` dispatches one-shot permissioned
  writes (Model Free draft, connector writes).
- `spawn_claude` runs in `SLACK_AGENT_CWD` (default the repo) with the workspace
  system prompt (`build_claude_system_prompt`): reply concisely, the bridge posts
  the final text, end multi-step work with a "Done:" summary.
- Sessions: `claude_sessions(thread_ts → session_id)`; first turn pins a uuid via
  `--session-id`, later turns `--resume`; a missing session raises `SessionExpired`
  → the bridge clears and retries fresh.

### Live progress (the placeholder)
The `🔄 thinking…` placeholder is updated as the turn streams, driven by the
engine's NDJSON events:
- `claude_agent.py` `make_event_handler` turns `thinking`/`text`/`tool_use`
  events into a one-line `state["activity"]` (💭 reasoning / 💬 narration /
  🔧 tool + arg hint), tracks `tools_started/done`, and stamps `started`
  (monotonic) for elapsed-time rendering. Surfacing `thinking`/`text` is what
  fixes the old "stuck at 0/0 tool calls" symptom on tool-less reasoning turns —
  previously only tool events refreshed the placeholder.
- `run_turn_with_progress` runs a **heartbeat** (`heartbeat_interval_s`,
  `SLACK_PROGRESS_HEARTBEAT_S`, default 3s) that re-schedules a render even when
  the agent is quiet (a slow MCP call, silent extended thinking) so the spinner
  + elapsed clock keep moving. Cancelled and drained before the turn returns.
- `app.format_progress` renders: animated spinner · phase
  (thinking/running N tools/working) · ⏱ elapsed · `done/started` tools, then
  the activity line and recent completed tools.
- `make_slack_progress_callback` is a **coalescing, trailing** throttle: ≤1
  `chat.update` per `PROGRESS_UPDATE_MIN_INTERVAL_S` (1.5s) to respect Slack's
  rate limit, but mid-cooldown updates are coalesced and flushed when the window
  opens, so the placeholder never sticks on a stale frame (the original bug was
  the first empty frame anchoring the throttle and dropping everything after).
  Identical renders are de-duped. Handlers call `await progress_cb.aclose()`
  before posting the final reply so a pending flush can't overwrite the answer.

## /chat gate (`dashboard/app.py`, `templates/chat.html`)
- Reuses `DASHBOARD_OPERATOR_ACTION_CODE` and `_operator_token_serializer`.
- `chat_locked(request)` → open when unconfigured; else requires a valid
  `agent_me_chat_access` cookie (or a home-page operator token header).
- `_chat_guard` gates every `/api/chat/*` handler and the SSE endpoint (403
  `chat_locked`). `POST /api/chat/unlock` validates the passcode header and sets
  the cookie — cookie-based so SSE (which cannot send custom headers) is gated.
- Client (`chat.html`): a "Thanh Phan only" modal posts the passcode, then loads
  sessions; any 403 re-locks. Mobile: sidebar is a slide-over drawer; a floating
  Chat button lives in `base.html` on every page except `/chat`.

## Brief (`scripts/daily_brief.py`)
`SOURCES`: calendar/outlook/slack → `claude_fetcher` (`CLAUDE_SOURCE_TOOLS` maps
each to a `maas-*` glob); teams → `codex_fetcher`; jira/gitlab/nvbugs → direct
MaaS MCP JSON-RPC; github → `gh`. The Slack/email mirror stays on codex
(`maas-slack` has no write tool).

## Constraints
- No Claude Teams MCP and `maas-slack` is read-only → Teams + mirror keep codex.
- `codex` is not installed on the dev box, so those codex paths fail here (the
  claude paths work). Operator accepted the codex fallback for Teams/mirror.
