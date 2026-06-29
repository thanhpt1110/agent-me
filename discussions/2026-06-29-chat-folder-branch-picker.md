# 2026-06-29 — Chat picker: content-sized folder chip + per-folder branch dropdown

## Ask (operator, paraphrased from Vietnamese)
Fix the chat UI a bit:
1. The `folder@branch` chip border should be **flexible-width to the folder name**, not a fixed size.
2. The branch is **hardcoded `@big-update`** — it doesn't actually know which branch the folder is on. Stop hardcoding it.
3. Keep the folder as a **dropdown** (already is).
4. Nice-to-have: when a folder is chosen, also show a **branch dropdown** if the folder has `.git`, with dynamic width too.

## What was wrong
- `chat_branch` was computed once per page render from `git rev-parse --abbrev-ref HEAD` run in the
  **dashboard's own repo** (`~/agent-me`) → always `big-update`, shown for *every* folder regardless
  of the selected folder.
- The folder `<select>` had a fixed `max-w-[9rem] truncate`.

## Change
- **Backend** (`chat/service.py`, `app.py`)
  - `branch_options(cwd)` → `{is_git, branches, current}` resolved per-folder via git.
  - `GET /api/chat/branches?cwd=…` exposes it (operator-gated like the rest of `/api/chat`).
  - `PATCH /api/chat/sessions/{id}` accepts `{branch}` → `checkout_branch` (unstarted-only; branch
    validated against the folder's own `for-each-ref` list → no free-form arg to git).
  - Removed the hardcoded `chat_branch` page-context var.
- **Frontend** (`templates/chat.html`, `static/app.css`)
  - `branchesByCwd` cache + `branchSuffix`/`pickerIsGit`/`pickerBranches`/`onBranchChange`.
  - `selectWidth()` = `clamp(2rem, calc(Nch + 1.15rem), 14rem)`; `.chip-select` (`appearance-none` +
    custom SVG caret) for predictable, content-hugging width.
  - Branch dropdown shown only for a git folder; `aria-label`s added.

## Review (adversarial workflow) → fixes folded in
Ran a 3-dimension review (correctness / security / UX) with per-finding verification. The security
dimension found no injection (the branch is whitelisted against the folder's branch list). The
load-bearing finding was about **blast radius**, plus several smaller ones — all fixed before commit:
- **Shared/own-tree checkout (medium).** `checkout_branch` was gated only by *this* session's lock,
  but a `git checkout` mutates the whole working tree. A new chat defaults to `~/agent-me` — the live
  dashboard/bridge/operator-terminal tree — so it could flip `big-update` out from under them, and
  could flip a folder another started session is mid-run in. **Fix:** `_branch_switch_allowed` refuses
  checkout for `default_cwd` and for any folder another session is pinned to; the client mirrors this
  with `branchReadOnly()` and shows those branches read-only. This also keeps the hard rule "never
  switch `~/agent-me` off `big-update`".
- **Unborn-branch repo showed `@HEAD` (low).** `rev-parse --abbrev-ref HEAD` prints `HEAD` on a
  commit-less repo. **Fix:** prefer `symbolic-ref --short HEAD` (real name), fall back to rev-parse.
- **Width could overflow the header (medium).** Dropping `max-w-[9rem]` with an unbounded width let a
  long name push the `h-14` header. **Fix:** `clamp(…, 14rem)`.
- **Native-arrow width was browser-dependent (medium).** **Fix:** `.chip-select` `appearance-none` +
  custom caret so the JS-set width is exact.
- **Stale branch flashed mid-fetch (low).** **Fix:** `syncBranches` clears `selectedBranch`
  synchronously before the fetch.
- **No accessible name on the selects (low).** **Fix:** `aria-label`.
- Deferred (documented): persisting the branch on `SessionMeta` so the *started* chip pins the exact
  branch a session ran on — neutralized in practice by the shared-tree guard (a dedicated folder's
  HEAD == that session's branch), so left for a later pass rather than expanding scope.

## Verification
- `uv run ruff check src tests` clean; `uv run pytest tests/test_chat*.py -q` green (+3 branch tests).
- **Live `:8787` HTTP** end-to-end against a throwaway temp repo: unlock → branches (git + non-git) →
  PATCH cwd/branch → checkout confirmed on disk; agent-me own-tree + shared-folder both 409;
  `~/agent-me` stayed on `big-update`.
- `node` check of `selectWidth`/`branchReadOnly`/`branchSuffix`/`pickerBranches`.
- Browser screenshot blocked by missing system libs (`libatk`) on this CPU host — same limitation as
  `scripts/verify_chat_ui.py`; verified render-relevant facts via served HTML + the live server instead.
