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
  web shows terminal/Slack sessions too. `list_sessions` (cached by mtime/size),
  `read_events_tail` (bounded byte tail → web events), live/busy from `~/.claude/sessions`.
  Continuing one from the web *adopts* it (`service._adopt_native`: import history once,
  then `--resume`); the client resets on adopt so seq stays consistent.
- `runner.py` — re-exports `ClaudeCliRunner` (= the shared engine).
- `jobs.py` — `ChatTurnJob` live fan-out (monotonic `seq` for client dedup).
- `mirror.py` — `WebSessionMirror`: a *different process* (the Slack bridge) writes
  its agent turns into this same on-disk store under `slack-<thread_ts>`
  (`origin="slack"`), so Slack chats list/replay in the web like a direct chat.
- `service.py` — `ChatService`, the only object the route handlers touch. For a
  mirrored session with no in-process job it reports busy while the turn is
  unfinished+recent and **tails** the transcript so an in-flight Slack turn streams
  live; deleting an `origin == "slack"` session won't purge the shared CLI transcript.
  **Queue:** sending while a turn runs enqueues the message (`_drain` runs the queue
  on one job, one `turn_end` at the end) — terminal-like, the user isn't blocked.
  **Replay:** `stream()` tags replayed history events `replay:true` so a *past* turn's
  `turn_end` never closes the client mid-replay (that was the "old session won't show
  live progress" bug); the client must POST the message before opening the SSE.

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
