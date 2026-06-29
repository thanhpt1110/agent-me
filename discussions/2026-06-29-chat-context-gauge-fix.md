# 2026-06-29 — Fix `/chat` context-% gauge stuck at 100%

## Symptom (operator)
The `/chat` header `ctx N%` chip showed **100%**, but Claude Code's own `/context` reported
only **~19%** (194.6k / 1M) for a comparable session. The 100% looked obviously wrong.

## Diagnosis
The per-session `context_tokens` in `data/chat-sessions/<id>/meta.json` for the active session was
**6,074,260** — ~31× the real context. The gauge is `min(100, round(context_tokens / 1,000,000))`,
so 6.07M → clamped to 100%.

Root cause in `claude_agent/runner.py`: `_context_tokens` summed the **`result` event's `usage`**.
In the `claude` stream-json output, `result.usage` is **cumulative over every internal API call in
the turn** — each tool step is its own API round-trip, and `cache_read_input_tokens` (the prompt
re-read from cache each step) is summed across all of them. A turn with many tool calls therefore
reports millions of "tokens", even though the model's actual context window occupancy is just the
size of the most recent request.

Confirmed against real data (`~/.claude/projects/.../<sid>.jsonl`): the **last `assistant`
message's** `usage` summed to ~48k (sane), while the stored cumulative value was 6.07M.

## Fix
Take `context_tokens` from the **last `assistant` message's `usage`** (the final call reads the most
context, so its `input + cache_read + cache_creation + output` ≈ true occupancy), not the cumulative
`result.usage`. `result.usage` is kept only as a fallback when no assistant usage was seen.

- `runner.py`: capture `message.usage` on each `assistant` event into `state["last_usage"]`; at the
  `result` event compute `_context_tokens(state["last_usage"] or result.usage)`.
- `_context_tokens` docstring updated to say "apply to a single call's usage, not cumulative".

## Verification
- `uv run ruff check` clean; `uv run pytest -q` green.
- New regression `test_context_tokens_uses_last_assistant_not_cumulative_result`: feeds 2 assistant
  steps (final ≈ 48.3k) + a result with a 6M cumulative `usage`, asserts the RESULT event reports
  48,303 (last assistant), not the 6M.
- **Real turn on live `:8787`**: a fresh agent-me chat ("reply OK") now stores
  `context_tokens=24,242` → gauge **2%** (previously this class of session pinned at 100%).

## Note
A session that already stored a bad (pre-fix) `context_tokens` keeps showing the stale value until
its **next turn**, when the worker overwrites it with the corrected number. No data migration done.
