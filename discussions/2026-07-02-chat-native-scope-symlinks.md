# 2026-07-02 — /chat native-session scope: symlinks + workspace umbrella

## Problem

The operator's live `claude` CLI session (running at the workspace umbrella
`/data/agent-me.nvidia.com`) did not appear in the web `/chat` list. Their
expectation — and the feature's purpose — is that everything under
`~/.claude/projects` renders on the web so a host session can be continued
from the browser when the terminal isn't reachable.

## Root cause

`ChatService._folder_in_scope` compared raw path strings against the scope
roots (`$HOME`, `default_cwd`, `add_dirs`, default `add_dirs = $HOME`):

1. The umbrella is the **parent** of `default_cwd`
   (`/data/agent-me.nvidia.com/agent-me`) and not under `$HOME` → out of
   scope → the session was filtered out of `list_all_sessions`.
2. Since the 2026-07-02 host consolidation the repo lives on the data disk
   with `~/agent-me` as a symlink — the lexical check treated the two
   spellings as unrelated folders, and `folder_options` offered the same real
   directory twice (once per spelling).

## Fix

- `_clean_descendant` resolves symlinks on both sides before `relative_to`.
  Side effect (intended): the old lexical check let `~/link-to-anywhere` pass
  scope by its string; the resolved check rejects targets outside the roots.
  The dot-component rule now applies to the resolved relative path, so
  `.prod` (the production worktree) stays hidden.
- `folder_options` dedups by resolved path (first spelling wins) and offers
  the non-hidden children of every add-dir, so sibling repos on the data disk
  (`devtest-wrapper`) are pickable for new chats.
- `configs/.env`: `CHAT_ADD_DIRS=/data/agent-me.nvidia.com,/home/thaphan` —
  the umbrella joins the scope and is also passed to the agent as
  `--add-dir`; `$HOME` kept explicitly since setting the env replaces the
  default. Documented in `.env.example`.

## Verification

- 3 new tests: symlinked-root scope (`tests/test_native_sessions.py`),
  umbrella-root session lists via add-dir (same file), picker dedup +
  add-dir children (`tests/test_chat.py`). `ruff` clean, full `pytest` green.
- Live on :8787 after restart: unlock → `GET /api/chat/sessions` lists the
  running terminal session (`origin=terminal`, `cwd=/data/agent-me.nvidia.com`)
  alongside the Slack-mirrored session; natives already covered by the Slack
  web session stay deduped by `claude_session_ids`.
- Adversarial review workflow (correctness / security-scope / regression
  lenses, each finding re-verified by an independent skeptic against the real
  code) run on the diff before commit.

## Review findings → hardening (all four confirmed, all fixed)

1. **Picker/scope mismatch (high)**: `folder_options` offered $HOME children
   whose symlink target resolves outside every scope root (live example:
   `~/test-workspace` → `/data/test-workspace`), while the new resolve-based
   `_allowed_cwd` rejected them → `create_session` silently fell back to the
   repo. Fixed: the picker filters through the scope checker — it only offers
   what create will accept. (To make such a folder pickable, add it to
   `CHAT_ADD_DIRS`.)
2. **Purge under the wrong slug (medium)**: the CLI writes transcripts under
   the slug of its *physical* getcwd; a web session whose stored cwd was an
   alias spelling purged only the raw-cwd slug on delete, so the transcript
   survived in `claude --resume` and resurfaced as a native session. Fixed:
   `create_session`/`update_cwd` store a canonical cwd (`ChatService._canon`)
   and `purge_local_sessions` tries both the recorded and the resolved slug
   (covers legacy alias-spelled metas).
3. **Branch-guard bypass via alias (medium)**: `_branch_switch_allowed`
   compared raw strings, so an alias spelling of `default_cwd` could
   `git checkout` the live dashboard/bridge tree. Fixed: canonical-path
   comparison (same for the shared-folder pin check).
4. **Per-poll resolve() cost (low today)**: scope checks ran uncached
   `resolve()` per session per root every 4 s poll. Fixed: roots resolved and
   deduped once per listing, per-cwd verdicts memoized (`_scope_checker`).
