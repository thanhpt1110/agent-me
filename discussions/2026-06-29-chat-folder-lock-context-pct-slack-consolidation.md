# 2026-06-29 — Chat folder-lock + context-% gauge; Slack consolidation

Branch `big-update`. Three operator asks (summarised from a Vietnamese chat):

1. Slack bot was answering from the **A100 prod** instance (running `codex` in an
   empty `chat-cwd`), so "what workspace / what branch" looked wrong. Operator
   wants the Slack bot to run via the **dev/this host** env instead.
2. On web `/chat`: **hard-lock the folder** once a chat has started.
3. On web `/chat`: show **% context used** on the session title.

## Research (parallel Explore agents, before coding)

- **Slack bridge** is pure **Socket Mode** — one Slack app-token, **no port**, no
  dev/prod toggle. `REPO_DIR`/`SLACK_AGENT_CWD` default to `~/agent-me`;
  `AGENT_BACKEND=claude` runs the agent **in the repo** (so branch/workspace
  answers work), while `codex` ran in `STATE_DIR/chat-cwd` (empty, no git) — the
  source of the confusion. Two bridges on the same token = Slack load-balances
  events non-deterministically.
- **chat.html** folder picker had **no gating**; it only set cwd for the *next*
  new chat. Title meta showed origin + folder + reltime; **no token/usage** notion
  anywhere. The `claude_agent` runner parsed only `total_cost_usd`/`duration_ms`
  from the CLI `result` event and **dropped `usage`**; the client dropped the
  `result` event entirely.

## Decision

- **Slack**: operator chose **"move entirely to this host"** (not a parallel dev
  Slack app, not shared-token-stop-prod). Socket Mode means only the bridge is
  singular; every other surface (dashboard :8765 prod / :8787 dev, tunnel, brief)
  is HTTP and already runs dev+prod in parallel, so consolidating the bridge
  **doesn't affect A100's other services**. Cutover (stop A100 bridge, install +
  enable the unit here) is **ops, left for explicit operator go** — stopping a
  live prod bridge is disruptive/outward-facing. No bridge **code** change needed:
  `.env` already has `AGENT_BACKEND=claude`; the unit is host-portable. Validated
  by a non-connecting import on this host.
- **Folder lock**: make the picker reflect the **current** chat and lock it once
  started; allow re-pointing only an unstarted chat (PATCH `cwd`, backend refuses
  once `claude_started`/`turns>0`/running/live). This is the ChatGPT-like model
  and makes the (already pinned) `--resume` cwd explicit instead of an illusion.
- **Context %**: sum the CLI `usage` (input + cache_read + cache_creation +
  output) into `context_tokens`, persist on the meta, render `ctx N%` over a
  configurable window (`CHAT_CONTEXT_WINDOW`, default 1,000,000 = opus-4.8). A
  killed turn (0) never clobbers the last good reading.

## Implementation

- `claude_agent/runner.py`: `_context_tokens(usage)`; RESULT event + `TurnResult`
  carry `context_tokens`. `claude_agent/models.py`: `TurnResult.context_tokens`.
- `chat/models.py`: `SessionMeta.context_tokens` (in `public_dict`).
  `chat/config.py`: `context_window` ← `CHAT_CONTEXT_WINDOW`.
  `chat/turn_runner.py`: persist `context_tokens` (skip 0). `chat/service.py`:
  `folder_locked` + `update_cwd`. `app.py`: PATCH `cwd` branch + pass
  `chat_context_window`.
- `templates/chat.html`: `contextWindow` x-data arg; `folderLocked`,
  `pickerFolders`, `onFolderChange`, `contextPct`, `ctxTitle`; picker
  `:disabled`/🔒; title `ctx N%`; capture `context_tokens` from the `result` event;
  `selectedFolder` tracks the selected chat.
- `configs/.env.example`: documented `CHAT_CONTEXT_WINDOW`.

## Verification

- `uv run ruff check` clean; `uv run python -m pytest -q` → 300 passed (new:
  `test_context_tokens_tracked_and_exposed`, `test_update_cwd_locked_after_start`,
  `test_http_folder_repoint_unstarted`, `tests/test_claude_agent_runner.py`).
- `node --check` on the chat `<script>`; `/chat` renders on :8787 with the new
  x-data arg. Headless browser unavailable here (missing `libatk`) → operator
  hard-refresh + visual check.

## Cutover executed (2026-06-29, operator-authorised)

- A100 (`ipp1-2252`/`local-thaphan`): `systemctl --user stop agent-me-bridge`;
  moved the real unit aside (`…service.movedaside`) and `systemctl --user mask`
  → `/dev/null` symlink, because `agent-me-watch` (tracks `main`) would
  `systemctl restart` the bridge on the next main commit. Mask makes that restart
  fail ("Unit is masked") → robustly down. Dashboard (:8765) + watcher untouched.
- This host: installed the unit to `~/.config/systemd/user/`, `enable --now`,
  linger on. Verified `bridge_starting agent=claude cwd=/home/thaphan/agent-me`,
  Socket Mode session established, `⚡️ Bolt app is running!`, no errors.
- Code fix: `slack_bridge/app.py::main_async` `bridge_starting` log hardcoded
  `agent="codex"` (+ codex model) — corrected to log the real `AGENT_BACKEND`,
  active model, and cwd (was the last stray "Codex" in the bridge boot log).
- Reverse if ever needed: A100 `systemctl --user unmask agent-me-bridge` + move
  the unit back; this host `systemctl --user disable --now agent-me-bridge`.
