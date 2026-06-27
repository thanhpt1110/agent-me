# 2026-06-27 — GitLab remote, DEV chat context, and chat performance

Operator report: the DEV `/chat` (:8787) "doesn't understand context", seemed to
"work on main", and said "no `gh` to commit" — and chat load was slow/laggy. Asked
to double-check the host (this box vs the A100), force all work onto `big-update`
of this host, push every commit to GitLab, and optimise chat performance.

## Diagnosis (the chat was mostly fine; the remote was wrong)

- **Host/branch already correct.** This is `agent-me.nvidia.com` (CPU, no GPU — not
  the A100), on `big-update`, repo `/home/thaphan/agent-me`. No revert needed.
- **The chat already had context.** The transcript of the earlier "Slack live
  progress" prompt shows it ran in `cwd=/home/thaphan/agent-me`, `model=claude-opus-4-8`,
  read `STATE.md`, implemented the feature, ran an adversarial review, and committed
  (`d184e1e`, `f43f94f`).
- **Real bug = git remote.** `origin` pointed at GitHub; the host has no `gh` and no
  GitHub credentials, and `big-update` had never been pushed. So the chat agent's
  `git push` failed ("no gh / can't auth"), and its `origin/main` mentions read like
  "working on main". Meanwhile a GitLab credential was already stored and the project
  already existed at `vrdc-swqa/swqa-2e2-workflow/agent-me` (sibling of
  `agent-me-qa-handsfree`).
- **Perf culprit = Tailwind Play CDN.** `cdn.tailwindcss.com` compiles CSS in the
  browser and re-runs a MutationObserver scan on every DOM mutation — so each streamed
  chat event triggered a re-scan (slow load + progressive lag).

## Changes

1. **Git → GitLab.** `origin` → `https://gitlab-master.nvidia.com/vrdc-swqa/swqa-2e2-workflow/agent-me.git`;
   GitHub kept as `github`. Identity `Thanh Phan <thaphan@nvidia.com>`; credential store
   uses the write-scoped PAT (`~/.git-credentials`, 0600). `big-update` pushed, tracking
   `origin/big-update`. `.git/hooks/post-commit` auto-pushes `big-update` to GitLab on
   every commit (background; skips rebase/merge; `GIT_AUTO_PUSH=0` opts out;
   `data/auto-push.log`).
2. **Chat operating context.** `CHAT_APPEND_SYSTEM_PROMPT` (default in `chat/config.py`)
   tells every turn: this host, repo path, always `big-update`, `origin`=GitLab,
   push-after-commit, never `gh`. `CHAT_MODEL` defaults to `claude-opus-4-8`. Threaded
   through `ChatService` → runner.
3. **Chat performance.** Replaced the Play CDN with a compiled static stylesheet
   (`static/tailwind.css`; `scripts/tailwind/{tailwind.config.js,input.css,build.sh}`,
   built via `npx tailwindcss@3` — no `node_modules` committed). Vendored Alpine to
   `static/alpine.min.js` (pinned 3.15.12) to drop the unpkg runtime dependency/redirect.
   Defined the long-dangling `rounded-card` token so chat panels finally round.
4. **Session clarity.** Header badge `big-update @ agent-me` (cwd tooltip); sidebar `RUN`
   pill for busy sessions; 4s visible-tab poll so every session's run-dot stays live even
   while viewing another; header reads "another/N sessions running".

## Verification

- `uv run ruff check` clean; `uv run python -m pytest -q` → 248 passed (new
  operating-context tests in `test_chat.py`).
- Compiled CSS contains every used class + Tailwind Preflight; DEV dashboard restarted
  healthy on :8787, serves `tailwind.css`/`alpine.min.js`, zero CDN/unpkg references.
- **Real `/chat` turn** (unlock → create → message → SSE): ran in
  `cwd=/home/thaphan/agent-me`, model `claude-opus-4-8`, correctly reported host,
  `big-update`, and `origin`=GitLab.
- `node --check` on the inline chat JS passed; `x-data` is single-quoted with 5 args
  matching the 5-param factory.
- Could NOT run an in-browser click-test (chromium GUI libs absent, no passwordless
  sudo) — operator verifies in their own browser.

## Open

- **Slack live progress**: the fix is committed + now on GitLab, but no bridge runs on
  this host — the bot the operator tested runs elsewhere (A100) on pre-fix code. Needs a
  decision on where the bridge runs, then deploy/restart there.
