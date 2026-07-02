# 2026-07-02 — Fix the flaky `test_auto_sfa_runner_persists_trigger_history`

Backlog item from `ideas.md` (2026-06-27): this test failed ~1/8 runs and every
verification note for weeks had to say "passes on retry." Killed it.

## Root cause (reproduced, not guessed)

Ran the test node 25× on the live host → **2 failures**, always the same assertion:
`assert rows[0]["status"] == "done"` → `'running' == 'done'`.

`AutoSFARunner._run` (`dashboard/auto_sfa_runner.py`) sets `job.status = "done"`
**in-memory** and only then `await asyncio.to_thread(self._record_history, job)` to
write `"done"` to SQLite. The test polled `runner.get_job(...).status == "done"`,
broke on that in-memory flip, then read `runner.recent_history()` from the DB — which
could still hold the earlier `"running"` row because the `to_thread` write hadn't
landed yet. Pure test-side race; the runner is correct (it always writes `"done"`
eventually).

## Fix

Await the job's background task instead of polling status. `_run` awaits the history
write before it returns, so `await job._task` guarantees the DB is consistent when we
read it. Applied to `test_auto_sfa_runner_persists_trigger_history` (the flaky one) and
to `test_auto_sfa_runner_runs_create_flow` (same fragile poll-then-read-history shape;
not flaky today but cleaner deterministic). Left the concurrent-jobs and cancel tests
on their polls — they assert mid-flight/in-memory state, not the persisted terminal row.

Chose the test-side fix over reordering the runner (write DB before flipping in-memory
status): the runner behaviour is correct, the transient DB-vs-memory window is harmless
for a history view, and `ideas.md` explicitly prescribed "await job completion instead
of a fixed point."

## Verification

- Previously-flaky node + sibling run **40×** back-to-back → **40/40 pass** (was ~2/25
  failing before).
- `uv run ruff check src tests` clean; `uv run python -m pytest` → **313 passed**.
