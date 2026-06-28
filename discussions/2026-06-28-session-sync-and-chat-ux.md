# 2026-06-28 — Cross-surface session sync + chat UX/perf overhaul

Branch `big-update`. All changes are in the dashboard `/chat` feature. Driven by a
series of operator requests over one session (summarised in English).

## Requests

1. When prompting from web, is the session saved on this host? Make web, Slack and
   terminal sessions **sync** — the web should list/replay everything `claude --resume`
   shows; `claude --resume` in the terminal should see web + Slack sessions; Slack does
   not need to list web/terminal.
2. Deleting a chat on the web should delete that `claude` session locally too. Show a
   last-update relative time + origin (web/terminal/slack) badge next to each chat title,
   plus a "running" icon. Optimise that sidebar.
3. Fix the `Cmd+~` shortcut to open the web terminal (didn't work). Make `/` list Claude's
   built-in commands (`/context`, `/usage`, …), not only skills. Let the user send a
   follow-up while the agent is working (don't block) — terminal-like queueing.
4. Loading the *entire* session onto the web is slow; switch to paging/cursor. Opening an
   old session and prompting again **lags and never renders the new turn's live progress**
   — investigate and fix. Make it smooth like ChatGPT.

## What we found (research before implementing)

- Every surface runs the `claude` CLI in `cwd=~/agent-me`, so the CLI writes one
  `~/.claude/projects/-home-thaphan-agent-me/<id>.jsonl` per session **for all of them**.
  That dir — not `~/.claude/history.jsonl` — is what `claude --resume` lists (history.jsonl
  had only 4 of the 9 sessions). So at the storage layer sessions were already shared; the
  gap was only that the **web listed from its own `data/chat-sessions/` store**, missing
  terminal sessions.
- The web kept a *separate* transcript (`data/chat-sessions/<id>/transcript.jsonl`) and read
  the **whole file** on every hot path (`transcript()`, `len(transcript)` for seq, idle/live
  replay). Big sessions → lag.
- The "no live progress on an old session" bug had two compounding causes:
  1. `send()` opened the SSE **before** the POST created the server-side turn, so the stream
     raced to the idle branch and the new turn streamed to no subscriber; and
  2. replayed history includes past `turn_end` events, and the client closed the stream on
     *any* `turn_end` — so even when it did subscribe, a historical `turn_end` in the replay
     closed it mid-stream.

## What we built

- `dashboard/chat/native_sessions.py` — `NativeSessionReader`: list (cached by mtime/size),
  `read_events`/`read_events_tail` (jsonl → web events), live/busy from `~/.claude/sessions`.
- `ChatService.session_dicts()` merges native + web (dedup); `get_session`/`stream` replay
  native read-only; `_adopt_native` imports a terminal session's history once then `--resume`s
  it when continued from the web; `_delete_native` + extended `purge_local_sessions` remove the
  jsonl + `<id>/` sidecar so delete propagates to `claude --resume`.
- Queueing: `start_turn` enqueues when an in-process turn is running; `_drain` runs the queue on
  one job and emits a single `turn_end`. Queued bubbles are flagged for the UI.
- Store paging: `tail(limit)` + `events_before(cursor, limit)` (byte-offset cursor, O(limit)),
  `last_seq()`, `size()`. `stream()` replays only the tail, tags it `replay:true`, and emits a
  leading `snapshot` carrying the cursor. New `GET /api/chat/sessions/{id}/history?before=`.
- Frontend (`chat.html`): POST-before-SSE in `send()`; reset+reopen when a native session is
  adopted; ignore `replay` `turn_end`/busy; scroll-up "Load older"; trim DOM only while at the
  bottom; origin badges + ticking relative time + `.chat-run` spinner; `Ctrl+`` keybind
  (`e.code` fallback) + updated hints; built-ins-first `/` menu; Send→"Queue" while busy.
- `commands.py`: built-ins expanded (`/context`, `/usage`, …) and sorted before skills.
- New env `CHAT_SHOW_NATIVE_SESSIONS` (default on).

## Decisions / trade-offs

- **Adopt-on-continue imports the full native history once** into the web transcript (so the
  seq world is consistent and paging works), rather than merging native+web at read time per
  request (which made cross-numbering dedup fragile). One-time O(file) on first continue; every
  later open is an O(limit) tail.
- A session **open live in a terminal** is read-only on the web (resuming a live CLI session
  would fork it) — detected via `~/.claude/sessions/*.json` + a pid-alive check.
- `Cmd+`` / `Cmd+~` can't be made reliable on macOS (the OS reserves them for window cycling
  and consumes them before the page). `Ctrl+`` is the documented, working shortcut (VS Code does
  the same). The on-screen Terminal button always works.

## Verification

- `uv run ruff check` clean; `uv run python -m pytest -q` 288 passed (one unrelated `auto_sfa`
  timing test flakes, passes on retry).
- Real `NativeSessionReader` over the live store: 9 sessions, correct `ai-title` titles, current
  session flagged live+busy.
- In-process E2E: opening an old session + sending streams the new turn's live progress and ends
  on the live `turn_end`; the historical `turn_end` is tagged `replay` and ignored.
- `tail(400)` on a 20k-event/5MB transcript ≈ 1.6 ms vs ≈ 56 ms for a full read; paging
  reconstructs a 50-event transcript with no gaps/dups.
- DEV dashboard restarted on :8787; served `/chat` + `app.css` contain all new symbols.
- `node --check` on the chat JS passes; `x-data` stays single-quoted (guard intact).
- Real in-browser click-test still pending host chromium GUI libs (operator can verify).
