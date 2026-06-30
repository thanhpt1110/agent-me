# 2026-06-30 — Dynamic /chat folders + cross-folder session sync

Operator ask (paraphrased from Vietnamese): the /chat folder picker only shows 3
hard-coded folders — make it dynamic on this host: offer any folder under `~` that
doesn't start with `.`. Also double-check session sync: does a prompt from the
terminal or Slack still sync up to the web, and conversely does a web chat sync to
the host so `claude --resume` sees it? Fix anything broken.

## What was actually happening (verified on the live host before coding)

- **Only 3 folders** because `ChatService.folder_options` filtered `$HOME` children to
  `(child / ".git").is_dir()` — only git repos. The host has `n`, `ngc-cli`, `st2110`
  (non-git) which were therefore hidden; the picker showed `agent-me`, `~`,
  `agent-me-qa-handsfree`.
- **Real sync gap (host→web).** The `claude` CLI stores one `<id>.jsonl` per cwd under
  `~/.claude/projects/<cwd-slug>/`. `NativeSessionReader` only scanned `default_cwd`'s
  slug, so terminal/headless sessions started in any *other* folder never reached the web.
  Confirmed against the real store: the web listed only the 3 sessions in
  `-home-thaphan-agent-me`, while `agent-me-qa-handsfree` (3), `st2110` (2) and `~` (5)
  were invisible.
- **web→host was already fine** — the CLI persists per-cwd-slug, so a web chat run in a
  folder is listed by `cd <folder> && claude --resume`. Nothing to fix there; the gap was
  strictly the reverse direction.

## Decisions

- **Folder scope rule** (matches the operator's "under `~`, not dot-prefixed"):
  `_cwd_visible(p)` = `p` is `$HOME` or a descendant with **no** path component starting
  with `.`. Because `..` starts with `.`, this also rejects `..` traversal with no extra
  code. Generalized to `_folder_in_scope` = `_cwd_visible` **OR** under a configured root
  (`default_cwd`/`add_dirs`) so a non-`$HOME` deployment (e.g. the A100's
  `/localhome/local-thaphan/agent-me`) keeps working and its sessions still list. Lexical
  only (no `resolve()`): avoids per-poll I/O and surprises from symlinked project dirs;
  `/chat` is operator-gated and runs `--dangerously-skip-permissions`, so cwd scoping is a
  guardrail, not a security boundary. `_allowed_cwd` (spawn validation) adds existence.
- **Scan all slugs, filter by the session's own cwd.** `list_all_sessions(allow=…)` walks
  every `~/.claude/projects/*` dir and keeps sessions whose recorded `cwd` is in scope, so
  `/tmp/*` scratch and stale `/Users/*` (Mac-path) slugs drop out. Find-by-id across slugs
  (`_locate`/`find_anywhere`, `read_events*_anywhere`) because the web only knows the id,
  not the folder. `_delete_native` purges the session's **real** cwd, not `default_cwd`.
- **Kept the cwd-scoped methods** (`list_sessions(cwd)`, `find(cwd,…)`, `read_events(cwd,…)`)
  and refactored their bodies into `_events_from_path`/`_events_tail_from_path` shared with
  the `_anywhere` variants — no behavior change for existing call sites/tests.
- **Folder picker UI unchanged** — it's a `<select>` populated from `/api/chat/folders`, so
  widening the option list is the whole change; non-git folders simply show no branch
  dropdown (`branch_options` → `is_git=False`). Kept the flat `$HOME`-children list (no
  recursion) — bounded and matches the existing structure; capped at 50.

## Verification

- `uv run ruff check src tests` clean; `uv run python -m pytest -q` 309 passed (the
  `auto_sfa` timing test flakes and passes on retry). 4 new regression tests added.
- Live: `folder_options()` → 6 home folders; `list_all_sessions(allow=_folder_in_scope)` →
  13 in-scope native sessions across 4 folders (was 3), `/tmp/*`+`/Users/*` filtered.
- Real round-trip: ran a real `claude` turn in `~/st2110` (non-default) → CLI wrote
  `…/-home-thaphan-st2110/<id>.jsonl` (web→host resume sees it); web found/listed/replayed
  it by id (host→web); `delete_session` purged the right slug; throwaway session removed.
- An adversarial 3-dimension review workflow (path-scope / cross-slug-sync / regression)
  ran before committing and confirmed 4 real findings, all fixed + regression-tested:
  (1+2, high) out-of-scope sessions were filtered from the *list* but still reachable/
  adoptable/runnable by id — `_adopt_native` could spawn `claude` in `/tmp`; now
  `_scoped_native` gates every by-id path. (3, medium) a cwd-less transcript fell back to
  `cwd=~/.claude/projects`, which the old `_is_within($HOME)` clause wrongly admitted; the
  scope rule was unified to `_clean_descendant` so the dotfile exclusion holds relative to
  every root (incl. `$HOME`). (4, low) `folder_options` called `Path.home()` unguarded.
  Two further raised items were refuted on verification (the `--resume`-from-another-cwd
  "second file" claim, and a purge-wrong-slug claim that only applies to now-unreachable
  cwd-less sessions).

## Files

- `src/agent_me/dashboard/chat/service.py` — scope helpers, `folder_options`, create/update
  validation, native call sites routed across all slugs.
- `src/agent_me/dashboard/chat/native_sessions.py` — `_locate`, `_with_live`,
  `list_all_sessions`, `_list_slug`, `find_anywhere`, `read_events*_anywhere`, path-based
  read helpers.
- Tests: `tests/test_native_sessions.py`, `tests/test_chat.py`, `tests/test_chat_optimizations.py`.
- Docs: `design/dashboard-chat.md` (new dated section), `src/agent_me/dashboard/chat/CLAUDE.md`.
