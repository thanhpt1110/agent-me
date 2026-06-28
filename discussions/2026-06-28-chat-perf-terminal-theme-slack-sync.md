# 2026-06-28 — Chat perf, terminal theme/clipboard, Slack→web session sync

Operator request (paraphrased from Vietnamese): web chat messages still lag —
optimise rendering. Also make the Ctrl+~ terminal follow the page theme (and its
clipboard) when switching light/dark. Give a sample code block to test the Copy
button + theme. And: a Slack DM opens a new agent session — mirror that agent
session into the web chat exactly like a direct chat, and make sure the Slack bot
always drives the agent in the agent-me repo on the `big-update` branch.

## 1. Web chat render performance

Symptom: lag while streaming and on long sessions. Cause: every SSE event (and
every event of the full transcript replayed on session open) called
`events.push(ev)` + `$nextTick(scrollToBottom)` individually → a reactive render
pass and a layout/scroll per event. The per-row template also evaluates five
`<template x-if>` branches, so re-rendering the whole list per event is costly.

Fix (client-only, `chat.html`):
- **Frame-batched flush**: `push` queues into `_pending` and schedules one
  `requestAnimationFrame` flush (`_flushEvents`) that appends the whole batch with
  synchronous `events.push` (Alpine coalesces synchronous mutations into one
  render) and scrolls once. A burst of events or a whole replay becomes one
  render + one scroll per frame.
- **Frozen events**: `Object.freeze(ev)` — Alpine/Vue reactivity skips proxying
  frozen objects, cutting per-item overhead on long transcripts. Safe because
  events are write-once (rendered, never mutated).
- **Stable key**: `:key="ev._k"` (a monotonic id) instead of the array index.
- `selectSession` cancels a pending flush and clears the queue on switch.

Not done: virtualised scrolling / a real Markdown lib. The batch+freeze pass is
the high-impact, low-risk win; revisit virtualisation if very long sessions still
lag.

## 2. Terminal theme + clipboard follow the page

- `base.html` `applyTheme` dispatches `window` `CustomEvent("agent-me:theme")`.
- `chat.html` keeps a reactive `termThemeName`, picks `TERM_DARK`/`TERM_LIGHT`
  for the xterm `theme` and the drawer background, and on the theme event live-
  updates `term.options.theme` + the drawer (no terminal restart).
- Clipboard via `attachCustomKeyEventHandler`: Ctrl/Cmd+Shift+C — or Ctrl+C when
  there is a selection — copies the selection with the existing http-safe
  `copyText` (Clipboard API → hidden-textarea `execCommand` fallback), leaving a
  selection-less Ctrl+C as the normal SIGINT. Ctrl/Cmd+V reads the clipboard on a
  secure context and otherwise falls through to xterm's native paste (which works
  on plain http, where `navigator.clipboard.readText` is unavailable).

## 3. Demo code block

The empty chat state renders `md(demoMarkdown())` — a python + bash fenced block
with inline code/bold — so the Copy button and the light/dark theme can be
exercised on a real `.chat-code` block without spawning a turn. Copy works there
via the existing delegated `.chat-copy` listener on `$refs.scroll`.

## 4. Slack → web session sync

Architecture: the dashboard and the Slack bridge are **separate processes** on the
same host that both run the same `claude` engine in the **same** agent-me
workspace (`SLACK_AGENT_CWD == REPO_DIR == CHAT_DEFAULT_CWD`) and share the
filesystem, including `data/chat-sessions/` (the web `DiskSessionStore`) and
`~/.claude/projects/<enc-cwd>/<id>.jsonl` (the CLI transcripts).

Decision: rather than build a cross-process message bus, mirror Slack turns onto
the shared on-disk store the dashboard already reads, and have the dashboard tail
that file for in-flight external turns.

- `chat/mirror.py::WebSessionMirror` — `begin_turn` (ensure a `slack-<thread_ts>`
  session, `origin="slack"`, emit the user event), `tap` (an `on_event` that
  mirrors text/thinking/tool/error with monotonic `seq`), `end_turn` (pin the real
  CLI `claude_session_id` so the thread is resumable from the browser, bump
  `turns`, emit `turn_end`). Single-writer per session → a plain in-memory seq
  counter seeded from transcript length is enough.
- `store.ensure(sid, …)` (create-with-explicit-id, idempotent) and
  `store.last_event(sid)` (cheap tail read) added.
- `service`: `is_busy` also returns true for an external session whose last event
  isn't `turn_end` and was touched within `external_busy_window_s` (15 min —
  bounds a crashed turn). `stream()` tails the transcript (`_tail_external`,
  poll `external_tail_poll_s`) when there's no in-process job but the session is
  externally busy, so an in-flight Slack turn streams live in the web. Deleting a
  `origin == "slack"` session does **not** purge the CLI transcript the live Slack
  thread still uses.
- Slack wiring: `slack_bridge/claude_agent.py::run_turn_with_progress` gained an
  `event_tap` (wrapped so a mirror exception never breaks the live turn);
  `slack_bridge/app.py` builds the mirror once (`SLACK_WEB_MIRROR=0` disables) and
  drives begin/tap/end around the general agent path only (slash commands / brief
  / auto-sfa are not chat sessions and aren't mirrored).
- Repo/branch correctness: extracted the operating-context (host, **big-update**
  branch, GitLab origin, no `gh`, auto-push) into shared
  `agent_me.claude_agent.operating_context`, used by both the web chat config and
  the Slack system prompt — so a Slack turn always works in the agent-me repo on
  `big-update`, same as a web turn.

## Verification

`uv run ruff check` clean; `uv run python -m pytest -q` all pass incl. new
`tests/test_chat_mirror.py`; `node --check` on the `chat.html` script block.
Full headless browser run still needs the dev host (`playwright install-deps`).

---

## Round 2 — chat still unstable (same day)

User reported the web chat was still not stable: new prompts lag/freeze, phantom
"after rename" chat sessions kept appearing, clicking an old session hung and
loaded nothing, the terminal still didn't follow the theme, and they wanted
`Cmd+~` to open the terminal.

Diagnosis (empirical, not guessed):
- The two real sessions hold 221 and 379 events; 287 of the 379 are tool
  `<details>` rows carrying full `<pre>` bodies. Draining `ChatService.stream()`
  for them takes **4 ms** and terminates correctly — so the backend was never the
  problem. The freeze is entirely client-side, and round-1's rAF batching made it
  worse: it flushed all ~380 heavy rows in ONE synchronous render → multi-second
  tab block = "đứng hoàn toàn". Content was always persisted (427 KB on disk);
  the frozen UI just never showed it ("nothing saved" was perception).
- "after rename" came from the HTTP chat tests, which write to the real
  `data/chat-sessions/` because the dashboard builds `CHAT_SERVICE` from env at
  import. Every `pytest` run (including mine) polluted the live dashboard.

Fixes:
- **Bounded client rendering** (`chat.html`): `FLUSH_CHUNK=40` rows/frame
  (reschedule until drained), `EVENTS_CAP=400` (splice the front), and lazy tool
  bodies (`<details x-data="{o:false}" @toggle> + <template x-if="o">`). DOM is
  now bounded and never blocks a frame.
- **Server replay cap** (`service.py` `_tail_cap`, `config.replay_max=400`): only
  the last 400 events replay on connect; disk keeps everything, `seq` preserved.
- **Test isolation** (`conftest.py`): `CHAT_DATA_DIR` → temp dir before app import.
- **Terminal**: `Cmd+~` opens it (leaving `Cmd+`` to the macOS window-cycler — a
  review finding), and `applyTermTheme` calls `term.refresh()` for an instant
  recolour.

Two adversarial review passes; the second caught the `Cmd+`` window-cycle hijack,
now fixed by binding only `Cmd+~` (the `~`/Shift form) for the meta key.
