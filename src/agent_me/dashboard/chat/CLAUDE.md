# dashboard/chat — native /chat feature (SOLID layers)

Persistent `claude` sessions over SSE; turns run server-side and survive client
disconnect. `runner`/`models` re-export the shared `agent_me.claude_agent` engine.

- `config.py` — `ChatConfig.from_env` (the only `CHAT_*` env reader). Operating
  context (host/branch) comes from shared `agent_me.claude_agent.operating_context`.
- `store.py` — `SessionStore` Protocol + `DiskSessionStore` (meta.json + transcript.jsonl).
  `ensure()` = create-with-explicit-id (idempotent); `last_event()` = cheap tail read.
- `runner.py` — re-exports `ClaudeCliRunner` (= the shared engine).
- `jobs.py` — `ChatTurnJob` live fan-out (monotonic `seq` for client dedup).
- `mirror.py` — `WebSessionMirror`: a *different process* (the Slack bridge) writes
  its agent turns into this same on-disk store under `slack-<thread_ts>`
  (`origin="slack"`), so Slack chats list/replay in the web like a direct chat.
- `service.py` — `ChatService`, the only object the route handlers touch. For a
  mirrored session with no in-process job it reports busy while the turn is
  unfinished+recent and **tails** the transcript so an in-flight Slack turn streams
  live; deleting an `origin == "slack"` session won't purge the shared CLI transcript.

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
