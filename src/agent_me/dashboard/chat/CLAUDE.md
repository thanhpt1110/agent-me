# dashboard/chat — native /chat feature (SOLID layers)

Persistent `claude` sessions over SSE; turns run server-side and survive client
disconnect. `runner`/`models` re-export the shared `agent_me.claude_agent` engine.

- `config.py` — `ChatConfig.from_env` (the only `CHAT_*` env reader). Operating
  context (host/branch) comes from shared `agent_me.claude_agent.operating_context`.
- `store.py` — `SessionStore` Protocol + `DiskSessionStore` (meta.json + transcript.jsonl).
  `ensure()` = create-with-explicit-id (idempotent); `last_event()`/`last_seq()` = cheap
  tail reads. **`tail(limit)` + `events_before(cursor, limit)`** read only the file tail
  (byte-offset cursor) so opening/streaming a long session is O(limit), not O(file) —
  never read the whole transcript on a hot path. Scroll-up paging uses the cursor.
- `native_sessions.py` — `NativeSessionReader`: read-only view of the `claude` CLI's own
  `~/.claude/projects/<slug>/*.jsonl` store (the sessions `claude --resume` lists) so the
  web shows terminal/Slack sessions too. Each cwd is a separate slug dir, so the web
  scans **all** slug dirs, not just `default_cwd`'s: `list_all_sessions(allow=...)` (every
  folder, filtered by cwd), `_locate`/`find_anywhere` + `read_events*_anywhere` (find a
  session by id alone, across slugs — the web only knows the id). Summaries cached by
  (mtime,size); `read_events_tail` is a bounded byte tail; live/busy from `~/.claude/sessions`.
  Continuing one from the web *adopts* it (`service._adopt_native`: import history once,
  then `--resume`); the client resets on adopt so seq stays consistent.
- `runner.py` — re-exports `ClaudeCliRunner` (= the shared engine).
- `turn_runner.py` — **`TurnExecutor` + the `agent-me-chat-turn` worker.** Web turns do
  NOT run inside the dashboard; the dashboard appends to the durable queue
  (`store.append_pending`) and spawns this worker in its **own process group**
  (`start_new_session=True`; the unit is `KillMode=process`) so a dashboard restart
  can't kill an in-flight turn — the worker keeps running, writes events to the
  transcript, and the dashboard reconnects by **tailing** it. The worker drains
  `meta.pending`, sets/clears `meta.running_pid`, and emits USER/agent/turn_end.
  `store.claim_run` (flock) ensures exactly one worker per session.
- `jobs.py` — `ChatTurnJob` live fan-out (legacy; the tail-based stream replaced it).
- `mirror.py` — `WebSessionMirror`: a *different process* (the Slack bridge) writes
  its agent turns into this same on-disk store under `slack-<thread_ts>`
  (`origin="slack"`), so Slack chats list/replay in the web like a direct chat.
- `service.py` — `ChatService`, the only object the route handlers touch. **Folder
  scope:** `folder_options` offers $HOME + every non-hidden folder under it (+ configured
  roots); `_folder_in_scope`/`_allowed_cwd` gate which cwd a chat may spawn in (under
  $HOME with no dot-prefixed component, or under `default_cwd`/`add_dirs`). For a
  mirrored session with no in-process job it reports busy while the turn is
  unfinished+recent and **tails** the transcript so an in-flight Slack turn streams
  live; deleting an `origin == "slack"` session won't purge the shared CLI transcript.
  **Queue:** sending while a turn runs appends to `meta.pending`; the running worker
  picks it up (terminal-like, never blocked). **Durable/auto-resume:** `meta.pending`
  survives a restart; `resume_pending()` (called at startup from the app lifespan)
  relaunches a worker for any session with queued work whose worker died.
  **Restart-survival:** the worker is a detached process (own group) — a dashboard
  restart leaves it running; the dashboard tails the transcript and reconnects.
  **Replay:** `stream()` tags replayed history events `replay:true` so a *past* turn's
  `turn_end` never closes the client mid-replay; `is_busy`/`stream` use
  `running_pid` (pid-alive) to know a turn is live. A session live in a terminal
  (`_live_elsewhere`) is read-only here.

The `/chat` page is gated by the operator passcode (cookie unlock, SSE-safe).
See `../CLAUDE.md` (single-quoted x-data) and repo-root `CLAUDE.md`.

Code-block gotchas (`chat.html` `md()` + `app.css`):
- **Copy** must work over plain `http://` (dev box off the tunnel), where
  `navigator.clipboard` is `undefined`. Use the delegated `.chat-copy` listener +
  `copyText` (Clipboard API → hidden-`textarea` `execCommand` fallback); never an
  inline `onclick` (Alpine `@click` also can't bind inside `x-html` content).
- Inline-code CSS uses `:not(pre) > code`, NOT `:not(.chat-code) > code` — the
  latter also matches block code (`<pre><code>`) and, in the light theme, painted
  the block `<code>` light-on-light. Block code carries its own light-theme colors.
