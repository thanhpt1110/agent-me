# agent-me — Project Instructions

You are working in `/data/agent-me.nvidia.com/agent-me` — _"myself, but in agent mode."_ A public-shareable framework for a personal AI OS. Currently being built/operated by **thaphan@nvidia.com** as the first deployment.

> **Host layout (since 2026-07-02):** the only home symlink is
> `~/agent-me.nvidia.com` → `/data/agent-me.nvidia.com` (the workspace umbrella);
> this repo is `<umbrella>/agent-me` and is the **only** clone on the host
> (the old `~/agent-me` symlink was removed — systemd units use
> `%h/agent-me.nvidia.com/agent-me`). Two environments, one repo:
> - **DEV** = this working tree, always on **`big-update`** — serves the dev
>   dashboard :8787 (`agent-me-dashboard-dev.service`) and the Slack bridge.
>   Bounce them freely while iterating.
> - **PROD** = the pinned, gitignored **`.prod/` git worktree on `main`** — serves
>   :8765 (`agent-me-dashboard.service`, Caddy → agent-me.nvidia.com). Never edit
>   files in `.prod/`. Release = merge `big-update` → `main`, push, then
>   `git -C ~/agent-me.nvidia.com/agent-me/.prod pull --ff-only &&
>   (cd ~/agent-me.nvidia.com/agent-me/.prod && uv sync) &&
>   systemctl --user restart agent-me-dashboard`. `.prod` has its own `.venv` and
>   `configs/.env` (`AGENT_ME_REPO_DIR` points at `.prod`).
>
> Sibling dirs: `../devtest-wrapper` (separate repo) and `../state` (shared runtime
> state — SQLite, MCP tokens, approvals, chat-cwd; never a repo, never edit by
> hand). Workspace map: `/data/agent-me.nvidia.com/CLAUDE.md`. Dev tooling needs
> `uv sync --extra dev` (plain `uv sync` strips ruff/pytest).

## What this project is

A 24/7 always-on autonomous agent framework, inspired by NVIDIA's Personal Assistant. Goal: orchestrator + sub-agents handle daily/weekly workload across Jira, GitLab, Confluence, Glean, calendar, code review, ops. Configs sync to GitHub. Runtime hosted on cloud host (CPU). Primary interface: Slack DM. The framework should be generic enough that any user can fork → set their own configs → have their own `agent-me`.

## Decisions already made (do not re-litigate unless asked)

- **Runtime host:** cloud host instance (CPU)
- **Interface:** Slack DM / channel
- **Config persistence / working remote:** GitLab `origin` →
  `gitlab-master.nvidia.com/vrdc-swqa/swqa-2e2-workflow/agent-me`. Daily work lives on
  branch **`big-update`**, pushed to GitLab on every commit (a `.git/hooks/post-commit`
  hook auto-pushes). The `github` remote (`github.com/thanhpt1110/agent-me`) is a
  **public mirror only** — never push there from automated flows. `gh` is installed
  at `~/.local/bin/gh` (since 2026-07-02, for the brief's GitHub source) and
  authenticates via `GITHUB_TOKEN` from `configs/.env` — still use plain `git` for
  repo pushes.
- **Default model:** Claude Opus 4.8 — always pick best/thinking-best, no cost limit

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

This holds on **every branch, including `big-update`**. A code change with no `STATE.md` update is **not finished**. A project **Stop hook** (`.claude/hooks/check-docs.sh`, wired in `.claude/settings.json`) nudges you at end-of-turn when code changed but `STATE.md` didn't — treat that as a checklist, not noise. It is **session-scoped**: a `SessionStart` hook (`.claude/hooks/capture-docs-baseline.sh`) snapshots the working tree's dirty files at session start, and the Stop hook diffs against that baseline, so it fires **only for files THIS session changed** — pre-existing uncommitted work, and turns that only do MCP/read-only work, never trigger it. Both share `.claude/hooks/_docs-snapshot.sh`.

## Auto-capture ideas (user's standing request)

When the user mentions a new idea casually mid-task ("oh, also we should…", "I just thought of…", "if an idea comes up from time to time…"), **don't wait to be told** — append it to `discussions/ideas.md` immediately with today's date and a one-line description, then continue with the current task. Confirm briefly ("noted in ideas.md").

## Workspace conventions

- Everything is bypassPermissions-scoped to this folder via `.claude/settings.json`. Don't second-guess routine actions; just do them.
- This project's auto-memory file is the user's global memory at `~/.claude/projects/-Users-thaphan/memory/`. Project-specific state belongs in `STATE.md`, not memory.
- User-facing chat may be Vietnamese when that is the user's language, but all repo-facing content must be English: code comments, docs, discussion logs, STATE updates, tests, and commit messages. When recording Vietnamese user prompts, summarize them in English instead of preserving raw Vietnamese unless exact wording is required.
- **NVIDIA org policy** disables `--permission-mode bypassPermissions` for `claude` CLI. Use `--dangerously-skip-permissions` (different code path, not blocked) or per-tool `--allowedTools` whitelists when running headless flows.

## Code style (repo-wide, enforced)

Keep the codebase and the agent context **clean and undiluted**. Self-explanatory names and short, linear steps over comments.

- **No redundant comments.** Don't add comments that restate what the code already says. Let clear names and structure carry the meaning.
- **Comment only the non-obvious "why"** — a surprising constraint, a workaround, a subtle ordering — and keep it to one short line.
- **Docstrings:** one line where one line suffices. No multi-paragraph essays in code; put rationale in `design/` or `discussions/`, not in source.
- **Explicit names** for functions, variables, and steps so the flow reads top-to-bottom without narration.
- Applies to all new and edited code. When you touch a file, leave it at least as clean as you found it; avoid large comment-only churn that would collide with other in-flight work.

## Research before you implement (enforced)

On every non-trivial prompt, **build context before writing code — don't implement from assumptions.** The user can prompt normally; deep research first is the default, not something they must ask for.

- **Fan out subagents in parallel** to gather context: map the relevant codebase (files, call sites, conventions) with `Explore`/`general-purpose` agents or a `Workflow`; pull task-relevant info from MCP servers (Jira/GitLab/Glean/Confluence/NVBugs…); do web/docs research when the task touches an API, library, or best practice you're not 100% sure of.
- **Synthesize the findings, then implement.** Prefer reusing existing patterns you found over inventing new ones.
- After implementing, **test the real behavior** (next section). Research → implement → test is the standard loop for every change here.

## Testing is part of "done" (enforced)

After implementing or changing a feature, **test its actual behavior** before claiming it works — not just that the code compiles or an API returns 200.

- **Run the real path a user takes.** For HTTP/UI, drive the browser (Alpine init, the click, the result), not only `curl` the endpoint — a 200 from `curl` can still be a dead UI (e.g. a broken `x-data` attribute that kills Alpine). For the dashboard chat use `uv run python scripts/verify_chat_ui.py`; for CLIs run the command; for the agent run a real turn.
- **Confirm every behavior you touched**, including the failure/edge paths (gate locked vs unlocked, empty input, resume, cancel).
- Always run `uv run ruff check` and `uv run python -m pytest -q` green, and add a regression test for any bug you fix.
- Only then report it as working, and say exactly how you verified it.

## Tooling: Python + uv

Project is **Python 3.12+** with [uv](https://docs.astral.sh/uv/) for dependency management. Setup any new clone with:

```bash
cd ~/agent-me.nvidia.com/agent-me
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
    ├── claude_agent/    ← shared `claude` CLI engine (runner/models) used by chat, bridge, brief
    ├── slack_bridge/    ← Python Slack bridge (entry: `uv run agent-me-bridge`); claude_agent.py adapter
    ├── dashboard/       ← Starlette web dashboard (entry: `uv run agent-me-dashboard`)
    │   └── chat/        ← native Chat feature (config/store/jobs/service; runner/models re-export claude_agent)
    └── scripts/         ← Python CLI helpers (daily_brief.py, reauth_mcps.py, etc.)
```

> **Note (big-update branch):** the interactive agent backend is **claude** (was
> codex). One shared engine `src/agent_me/claude_agent/` spawns the `claude` CLI
> (`-p --output-format stream-json --dangerously-skip-permissions`, `--session-id`/
> `--resume`) and is reused by three surfaces:
> - **`/chat`** (dashboard) — persistent sessions over SSE; gated by the operator
>   passcode (`DASHBOARD_OPERATOR_ACTION_CODE`) via a cookie unlock, plus a floating
>   Chat button + mobile-responsive layout. DEV runs on **:8787** (`DEPLOY_ENV=dev`)
>   via `deploy/agent-me-dashboard-dev.service`; production :8765 runs `main` from
>   the pinned `.prod` worktree (`deploy/agent-me-dashboard.service`, Caddy →
>   agent-me.nvidia.com) — chat reaches prod only when `big-update` merges to `main`.
> - **Slack bridge** — DMs run claude in the agent-me repo workspace (terminal-like),
>   per-thread sessions, live progress, final summary in-thread. `AGENT_BACKEND=codex`
>   restores the legacy path.
> - **Brief** — calendar/outlook/slack fan-out via claude + `maas-*` MCP read tools;
>   Teams and the Slack/email mirror stay on codex (no Claude write/Teams connector).
