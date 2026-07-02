# slack_bridge — Slack bot (entry: `uv run agent-me-bridge`)

Socket Mode bot. The interactive agent runs **claude** by default in the
the agent-me repo workspace (terminal-like); `AGENT_BACKEND=codex` is the legacy path.

- `app.py` — Bolt handlers + routing (`handle_user_query`), `spawn_claude` /
  `spawn_agent` / `spawn_write_agent`, sessions, progress, slash/plain commands.
- `claude_agent.py` — maps the shared engine's events onto the bridge's progress
  `state` dict. Surfaces `thinking`/`text`/`tool_use` as a live one-line
  `state["activity"]`, anchors elapsed time (`started`), and runs a heartbeat
  ticker so the placeholder advances even when the agent goes quiet mid-turn.
  Import-light + unit-tested.

Live progress (`app.py`): `format_progress` renders an animated spinner +
elapsed time + phase (thinking / running N tools / working) + the freshest
activity + recent tools. `make_slack_progress_callback` is a **coalescing,
trailing** throttle (≤1 `chat.update` per `PROGRESS_UPDATE_MIN_INTERVAL_S`, but
always flushes the latest state — never drops it); call `await
progress_cb.aclose()` before posting the final reply so a pending flush can't
overwrite the answer. `SLACK_PROGRESS_HEARTBEAT_S` tunes the heartbeat.
- `approvals.py` — PreToolUse approval gate (off unless `APPROVAL_GATE=1`).

Sessions persist per Slack thread in SQLite table `claude_sessions`
(thread_ts → session_id); first turn mints a uuid, later turns `--resume`.

See repo-root `CLAUDE.md` for conventions.
