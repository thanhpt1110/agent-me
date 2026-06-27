# 2026-06-27 — Slack bot live progress

## Problem (operator report)

Testing a Slack DM to the bot, the only thing shown the entire turn was:

```
🔄 0/0 tool calls done (live progress) (edited)
```

Ask: if we can't stream the live terminal/thinking like the dashboard `/chat`,
then at least show that the agent is *in progress / thinking something*, or poll
roughly every ~10s for live thinking + status + what the agent is doing, so the
turn is trackable.

## Root cause

Two compounding bugs in the Slack progress path (claude backend, the default):

1. **Tool-less turns never refreshed.** `slack_bridge/claude_agent.py`
   `make_event_handler` only called `schedule()` on `init`/`tool_use`/
   `tool_result`/`error`. The engine *does* emit `thinking` and `text` events
   (real assistant narration), but the adapter dropped them. So a chat turn that
   reasons and answers without calling a tool only ever scheduled the one `init`
   render — `0/0 tools` — and froze there until the final reply replaced it.

2. **The throttle dropped everything after the first frame.** The old
   `make_slack_progress_callback` was a leading-only 2s throttle. The first
   render fired on `init` (empty `0/0` state) and anchored the window; every
   later update inside 2s was *dropped*, not deferred. For short/bursty turns
   (which finish inside the window) the user therefore saw only the empty frame.
   Confirmed by simulating the pipeline: a ~1.4s turn produced exactly 1 update.

## Changes

`slack_bridge/claude_agent.py`
- Surface `thinking` → `💭 …`, `text` → `💬 …`, `tool_use` → `🔧 name — hint`
  into a single `state["activity"]` one-liner; each schedules a render.
- `initial_state` gains `started` (monotonic, for elapsed) and `activity`.
- Helpers `one_line()` (collapse + truncate) and `tool_hint()` (pull a short
  arg: file_path / command / pattern / query / url).
- `run_turn_with_progress` runs a **heartbeat** task (`heartbeat_interval_s`,
  default 4s; the bridge passes `SLACK_PROGRESS_HEARTBEAT_S`, default 3s) that
  re-schedules a render on a fixed tick so the spinner + elapsed clock keep
  advancing while the agent is quiet (e.g. a slow MCP call mid-turn). It is
  cancelled and drained before the turn returns.

`slack_bridge/app.py`
- `format_progress` redesigned: animated braille spinner · phase
  (thinking / running N tools / working) · ⏱ elapsed (`_fmt_elapsed`) ·
  `done/started` tools, then the activity line, the running set (only when >1),
  and recent completed tools. Tolerant of legacy state dicts (`.get` defaults).
- `make_slack_progress_callback` rewritten as a **coalescing, trailing**
  throttle: ≤1 `chat.update` per `PROGRESS_UPDATE_MIN_INTERVAL_S` (now 1.5s),
  but an update arriving mid-cooldown is coalesced into `pending_text` and
  flushed when the window opens — the latest state is never dropped. Identical
  renders are de-duped (no redundant API call / "(edited)" flicker). Exposes
  `aclose()`; the 3 streaming handlers (`_dispatch`, model-free, main chat) call
  `await progress_cb.aclose()` before their final write so a pending trailing
  flush cannot overwrite the answer.
- Legacy codex `spawn_codex` state dict got `started`/`activity` so the shared
  renderer stays correct on that path too.

## Decisions

- **Heartbeat owned by `run_turn_with_progress`, not a detached task**, so it is
  cancelled + drained before the turn returns — no progress update can race the
  final reply. The trailing flush is the one detached task; `aclose()` cancels
  it deterministically before the final write.
- **Throttle 1.5s** keeps us under Slack `chat.update` (tier-3, ~1 req/s) while
  the trailing flush guarantees freshness; **heartbeat 3s** covers silent gaps.
  Both env-tunable (`SLACK_PROGRESS_HEARTBEAT_S`).
- Kept the work in the bridge surface (adapter + `app.py`); the shared engine
  `claude_agent/` stays surface-agnostic.

## Verification

- `uv run ruff check` clean; `uv run python -m pytest -q` → 245 passed.
  New `tests/test_slack_bridge_progress.py` (renderer, coalescing, dedup,
  aclose, final-state ignore) + extended `tests/test_slack_bridge_claude.py`
  (activity surfacing, tool hint, heartbeat fires / disabled).
- Simulated the full pipeline with a mock Slack client: a tool-using turn now
  delivers `thinking… 0/0` → `working… 1/1 · 💬 narration · done: tool`, and
  `aclose` cancels the trailing flush (1 update, no stale frame).
- **Real `claude` CLI turn** through `spawn_claude` (temp cwd): the 0-tool
  "reply PONG" turn rendered `⠋ thinking… · ⏱ 0s · 💭 starting up` →
  `⠹ thinking… · ⏱ 2s · 💬 PONG` → `⏱ 3s` — animating spinner, advancing clock,
  real narration. Old code would have shown only `0/0 tool calls done`.

## Follow-ups

- Optional: stream partial assistant text token-by-token (engine emits per
  block, not per token, so this would need CLI/`stream-json` partial support).
- Consider a compact "elapsed only" heartbeat line during very long single tool
  calls to avoid repeating the same activity text (currently de-duped, so it
  just stops updating until elapsed ticks the second over).
