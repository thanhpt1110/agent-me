# 2026-07-02 — Single-checkout consolidation (agent-me-dev → agent-me)

## Context

After the A100 → VM migration this workspace held two clones of the same repo:
`agent-me/` (branch `main`, the A100 prod replica with uncommitted WIP, serving
:8765) and `agent-me-dev/` (branch `big-update`, serving :8787 + the Slack
bridge). The operator asked (in Vietnamese): what is `agent-me-dev`? what is
`state/`? — then requested keeping exactly **two repos** (`agent-me`,
`devtest-wrapper`), everything on **`big-update`**, a full cleanup, and docs so
any Claude session can orient instantly.

## Decisions

1. **The `big-update` checkout is the one that survives.** It was clean, pushed,
   and already ran the bridge + dev dashboard. The old `main` checkout's value
   was (a) its uncommitted WIP — verified byte-identical to
   `origin/a100-qa-e2e-wip` (commit 2317932), and (b) its `configs/.env` — every
   var also present in the dev `.env`. Both were additionally preserved in
   `/data/agent-me-main-backup-2026-07-02.tar.gz` before deletion.
2. **Production :8765 now runs `big-update`** from the same working tree as dev.
   Accepted trade-off: one checkout means a "release" is just restarting
   `agent-me-dashboard.service`; the bridge had already been running `big-update`
   in production capacity since the Slack cutover on 2026-06-29.
3. **`~/agent-me` returns as a symlink** to `/data/agent-me.nvidia.com/agent-me`,
   so the repo's own `%h/agent-me` unit files and docs are literal truth again,
   and units are installed verbatim from `deploy/`.
4. **`agent-me-prod.service` retired** for the canonical
   `agent-me-dashboard.service` name — this also fixed caddy/tunnel units whose
   `After=agent-me-dashboard.service` had pointed at a non-existent unit.

## Changes

- Deleted `/data/agent-me.nvidia.com/agent-me` (old main); renamed
  `agent-me-dev/` → `agent-me/`; `uv sync` to rebuild `.venv` for the new path;
  `.env` `AGENT_ME_REPO_DIR` updated; `~/.claude.json` project key migrated.
- `deploy/agent-me-dashboard.service`: description/comments modernised (it is
  the production dashboard, not the Phase-4 read-only view), nvm-node `PATH`
  line and `KillMode=process` added (chat feature spawns `claude` workers).
- `deploy/agent-me-bridge.service`: same `PATH` line — fixes the
  `mcp_list_failed: /usr/bin/env: 'node': No such file or directory` warning
  the bridge logged on every start (systemd user PATH has no nvm bin).
- `deploy/agent-me-dashboard-dev.service`: stale "production stays on the A100"
  comment replaced with the same-checkout release model.
- Stopped the orphaned transient `qa-slack.service` (its
  `~/agent-me-qa-handsfree` tree was already deleted; it errored every 30 s).
- New workspace orientation file `/data/agent-me.nvidia.com/CLAUDE.md`
  (workspace map, services table, release model); STATE.md banner refreshed.

## Verification

- `curl :8765/healthz` → 200, `curl :8787/healthz` → 200, Caddy route → 200.
- Bridge journal: `bridge_starting agent=claude cwd=/data/agent-me.nvidia.com/agent-me`,
  clean `mcp_health_check`, `⚡️ Bolt app is running!`.
- `systemctl --user list-units`: bridge + dashboard + dashboard-dev active;
  `agent-me-prod` gone (reset-failed cleared the residue).

## Follow-up (same day, evening): prod re-pinned to `main` via `.prod` worktree

The operator corrected decision 2: production must **stay on `main`**, isolated
from `big-update`, but still without a second top-level folder. Implemented with
a **git worktree**: `git worktree add .prod main` inside the single clone,
gitignored (`/.prod/`), own `.venv` + `configs/.env`
(`AGENT_ME_REPO_DIR` → `.prod`, state dir still shared).
`agent-me-dashboard.service` now runs from `%h/agent-me/.prod` (KillMode=process
dropped — that was for big-update chat workers, `main` has none). Release flow:
merge `big-update` → `main`, push, `git -C ~/agent-me/.prod pull --ff-only`,
`uv sync` there, restart the unit.

Also fixed along the way: plain `uv sync` during the rename had stripped the
`dev` extra (ruff/pytest gone) — restored with `uv sync --extra dev` and noted
in CLAUDE.md. And an operator question answered: `state/` must NOT be deleted —
it holds the MCP bearer-token DB + fernet key (deleting invalidates every
issued `agme-…` token), Slack thread history, and the approvals queue.

Verified: prod cwd `/proc/<pid>/cwd` = `.prod`, `:8765/chat` 404 (main code),
`:8787/chat` 200 (big-update), Caddy 200, `git status` clean, ruff clean,
pytest green.
