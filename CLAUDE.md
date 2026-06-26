# agent-me — Project Instructions

You are working in `~/agent-me/` — _"myself, but in agent mode."_ A public-shareable framework for a personal AI OS. Currently being built/operated by **thaphan@nvidia.com** as the first deployment.

## What this project is

A 24/7 always-on autonomous agent framework, inspired by NVIDIA's Personal Assistant. Goal: orchestrator + sub-agents handle daily/weekly workload across Jira, GitLab, Confluence, Glean, calendar, code review, ops. Configs sync to GitHub. Runtime hosted on cloud host (CPU). Primary interface: Slack DM. The framework should be generic enough that any user can fork → set their own configs → have their own `agent-me`.

## Decisions already made (do not re-litigate unless asked)

- **Runtime host:** cloud host instance (CPU)
- **Interface:** Slack DM / channel
- **Config persistence:** Personal GitHub, private repo
- **Default model:** Claude Opus 4.7 (1M context) — always pick best/thinking-best, no cost limit

## How to start any session here

1. Read `STATE.md` first — it's the single source of truth for "what stage are we at, what's next."
2. Skim newest file in `discussions/` for the most recent context.
3. Glance at `discussions/ideas.md` — user drops ideas there mid-conversation; pull them in if relevant.

## How to end any non-trivial session

1. **Update `STATE.md`** with: what changed, what's now next, any blockers. Keep it short — it's a status board, not a journal.
2. **Append a discussion log** in `discussions/YYYY-MM-DD-<short-topic>.md` if the session involved decisions, debates, or design work.
3. **Append to `discussions/ideas.md`** if user dropped ideas you didn't act on yet — date-stamped, one bullet each.

## Documenting a change is part of "done" (enforced)

Every time you implement, change, or remove a feature here, you **must** also update the docs in the same session — agents read these to orient, so stale docs silently break the next agent:

1. **`STATE.md`** — add a dated addendum (`_Addendum YYYY-MM-DD by <agent> — …_`): what changed, what's now next, blockers, and how you verified (commands run). It's the single source of truth; keep it a status board, not a journal.
2. **`discussions/YYYY-MM-DD-<topic>.md`** — append a short log for anything involving decisions or design.
3. **`design/<doc>.md`** — add/refresh an architecture doc when the change is structural (new module, route, or service).
4. **`configs/.env.example`** — document any new env var, with a comment.
5. **`CLAUDE.md`** (this file) — update the Folder map / conventions when they change.

This holds on **every branch, including `big-update`**. A code change with no `STATE.md` update is **not finished**. A project **Stop hook** (`scripts/check-docs-updated.sh`, wired in `.claude/settings.json`) nudges you at end-of-turn when code changed but `STATE.md` didn't — treat that as a checklist, not noise.

## Auto-capture ideas (user's standing request)

When the user mentions a new idea casually mid-task ("oh, also we should…", "I just thought of…", "if an idea comes up from time to time…"), **don't wait to be told** — append it to `discussions/ideas.md` immediately with today's date and a one-line description, then continue with the current task. Confirm briefly ("noted in ideas.md").

## Workspace conventions

- Everything is bypassPermissions-scoped to this folder via `.claude/settings.json`. Don't second-guess routine actions; just do them.
- This project's auto-memory file is the user's global memory at `~/.claude/projects/-Users-thaphan/memory/`. Project-specific state belongs in `STATE.md`, not memory.
- User-facing chat may be Vietnamese when that is the user's language, but all repo-facing content must be English: code comments, docs, discussion logs, STATE updates, tests, and commit messages. When recording Vietnamese user prompts, summarize them in English instead of preserving raw Vietnamese unless exact wording is required.
- **NVIDIA org policy** disables `--permission-mode bypassPermissions` for `claude` CLI. Use `--dangerously-skip-permissions` (different code path, not blocked) or per-tool `--allowedTools` whitelists when running headless flows.

## Tooling: Python + uv

Project is **Python 3.12+** with [uv](https://docs.astral.sh/uv/) for dependency management. Setup any new clone with:

```bash
cd ~/agent-me
./scripts/bootstrap.sh    # idempotent: uv sync + register all 17 MaaS MCPs + reminders
```

Bootstrap delegates the MCP-registration step to `scripts/setup-mcps.sh`, which is also idempotent — re-run anytime to detect/add new servers without disturbing existing ones. The full setup walkthrough (prerequisites, why each step) lives in `design/setup-on-fresh-host.md`. The end-to-end 24/7 deployment playbook (systemd, watcher, secrets, verification) lives in `design/deploy-on-host.md` — Claude on the deploy host can follow it step-by-step.

Run any first-party script through `uv run <entry-point>` (the venv stays implicit; agents don't need to manually `source .venv/bin/activate`):

```bash
uv run agent-me-bridge         # Slack bridge (Socket Mode, slash commands, MCP health probe)
uv run agent-me-reauth         # MCP re-auth helper — auto-opens auth URLs in browser
uv run agent-me-brief --period day [--dry-run]   # fan-out brief: 7 subagents in parallel
```

Console-script entry points are declared in `pyproject.toml` under `[project.scripts]`. To add a new script:
1. Drop the module under `src/agent_me/<package>/<name>.py` with a `main()` function.
2. Register `<dashed-name> = "agent_me.<package>.<name>:main"` in `pyproject.toml`.
3. `uv sync` to refresh the entry-point shims.

There is no JavaScript/Node code in this repo. Anything you find that references `services/slack-bridge/`, `pnpm`, or `package.json` is stale documentation — fix it when you see it.

## Folder map

```
agent-me/
├── CLAUDE.md            ← you are here (project orientation)
├── STATE.md             ← current stage / next steps (always read first)
├── README.md            ← public-facing description
├── pyproject.toml       ← Python deps + uv entry points
├── uv.lock              ← reproducible Python env
├── .claude/settings.json ← bypassPermissions (project-scoped)
├── configs/             ← runtime configs (.env, .env.example) — gitignored
├── design/              ← architecture docs, ADRs
├── discussions/         ← session logs + ideas.md
├── scripts/             ← shell scripts (bootstrap.sh, etc.)
└── src/agent_me/
    ├── slack_bridge/    ← Python Slack bridge (entry: `uv run agent-me-bridge`)
    ├── dashboard/       ← Starlette web dashboard (entry: `uv run agent-me-dashboard`)
    │   └── chat/        ← native Chat feature (SOLID: models/config/store/runner/jobs/service)
    └── scripts/         ← Python CLI helpers (reauth_mcps.py, etc.)
```

> **Note (big-update branch):** the dashboard now serves a native **Chat** page
> (`/chat`, nav entry) that spawns persistent `claude` CLI sessions over SSE. The
> DEV deployment runs the dashboard on **:8787** (`DEPLOY_ENV=dev`, direct access)
> via `deploy/agent-me-dashboard-dev.service`; production stays on the A100 (:8765).
