# 2026-07-02 — /chat drill-down folder browser, viewport lock, Slack continuity

## Operator asks (Vietnamese, summarized)

1. When starting a new chat, navigate every non-dot folder under `~/` — and
   subfolders at any depth — in a UI that's easy to visualize; if the chosen
   folder is a git repo, pick the branch right there, so "we control what we're
   chatting about and with whom"; once a session starts, nothing may change.
2. The chat page and footer kept sliding/scrolling — lock the whole page so the
   UI never slides.
3. Does the same Slack thread reply into the same session, and mirror on web?

## Folder browser

- Backend: `ChatService.browse_folders(path)` + `GET /api/chat/folders/browse`.
  One level per call: blank → scope roots (repo, workspace umbrella, `$HOME`);
  otherwise the folder's non-hidden, in-scope child dirs (500 cap) + breadcrumb
  entries back to the containing root. Reuses the memoized scope checker, so the
  browser can never offer a folder `create_session` would reject.
- Frontend: folder chip → popover with ⌂ roots, clickable breadcrumb, subfolder
  rows (click = drill in), and a "Use this folder" action that PATCHes the
  unstarted session's cwd (same flow as the old select). The per-folder branch
  dropdown is unchanged and appears as soon as the selected folder is a git
  work tree; own-tree/shared checkout guards apply as before.
- The started-session lock is untouched and total: UI hides the picker (static
  `folder@branch` chip) and the server already refuses `PATCH cwd`/`branch`
  once `folder_locked` (turns > 0 / claude_started / busy / live).

## Viewport lock

`base.html` marks `/chat` as an app screen: `overflow-hidden` body, `flex
h-full` shell, `main` = `flex-1 min-h-0`, footer skipped; `chat.html`'s grid
is `h-full` instead of `calc(100dvh - 9rem)`. Only the transcript pane
scrolls. Other pages keep the footer and scrolling body. Tailwind rebuilt.
Regression test: `test_chat_page_is_viewport_locked`.

## Slack thread ↔ web (answer: yes, with one rescue)

The bridge maps `thread_ts` → claude session id (`claude_sessions`, state.db)
and resumes that id for every reply in the thread; `WebSessionMirror` writes
the same turns into the web store as `slack-<thread_ts>`, so the web lists the
thread, streams an in-flight turn live, and can adopt/continue the same CLI
session. Post-consolidation rescue: the thread's older transcript was stranded
under the dead `…-agent-me-dev` slug — moved into `…-agent-me` (old slug dir
removed) and the mirror meta cwd was patched to the new repo path. The
thread's current session already lived under the new slug.

## Verification

ruff clean; full pytest green (`test_browse_folders_drilldown`,
`test_chat_page_is_viewport_locked` added); `node --check` on the inline chat
script; live `:8787` — browse API walks roots → `~/st2110` → children with
correct crumbs/parent/selectable, `/chat` HTML shows `overflow-hidden` +
no footer. Real-browser click-through still blocked on this host (chromium
system deps need sudo; the maas-playwright MCP has no browser installed).

## Follow-up (same day): breadcrumb drill-down → lazy tree; workspace defaults; `~/agent-me` retired

Operator feedback: the one-level-at-a-time drill-down was hard to use. Replaced
the popover body with a **lazy tree** (same browse API, UI-only): ▸ expands a
level in place (children indented — several levels visible at once), clicking a
name selects it, "Use this folder" applies; flat-list render with depth
padding, per-node fetch, collapse drops the deeper run.

Defaults, per operator: `CHAT_ADD_DIRS` gained `/data/test-workspace` so
`~/test-workspace` is navigable from $HOME alongside `~/agent-me.nvidia.com`;
new env `CHAT_SPAWN_DEFAULT_CWD` (`ChatConfig.spawn_cwd`) makes **new chats
open at the workspace umbrella** while `default_cwd` deliberately stays the
agent's repo (branch-guard target + operating-context source). `newSession()`
POSTs `{}` and the server picks the spawn default.

Cleanup, per operator: the `~/agent-me` symlink is gone. All systemd units now
use `%h/agent-me.nvidia.com/agent-me` (bridge, prod dashboard incl. `.prod`
paths, dev dashboard, watch, funnel) — installed, daemon-reloaded, restarted;
host-side script references and CLAUDE.md/host docs updated. Only home
symlinks left: `~/agent-me.nvidia.com`, `~/test-workspace`.

Verified live on :8787: folders `default` = `/data/agent-me.nvidia.com`;
`POST {}` creates a session with that cwd (deleted after the check); browse
roots = [thaphan, agent-me, agent-me.nvidia.com, test-workspace]; browse $HOME
= [agent-me.nvidia.com, n, ngc-cli, test-workspace]. All three services active
with cwd resolved through the workspace symlink (prod = `.prod`), Bolt running.
`test_folder_scope_validation` was updated — it asserted the now-removed
`~/agent-me` path exists.
