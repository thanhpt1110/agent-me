# agent-me — Project Instructions

You are working in `~/agent-me/` — _"myself, but in agent mode."_ A public-shareable framework for a personal AI OS. Currently being built/operated by **thaphan@nvidia.com** as the first deployment.

## What this project is

A 24/7 always-on autonomous agent framework, inspired by NVIDIA's Personal Assistant. Goal: orchestrator + sub-agents handle daily/weekly workload across Jira, GitLab, Confluence, Glean, calendar, code review, ops. Configs sync to GitHub. Runtime hosted on Brev cloud (CPU). Primary interface: Slack DM. The framework should be generic enough that any user can fork → set their own configs → have their own `agent-me`.

## Decisions already made (do not re-litigate unless asked)

- **Runtime host:** Brev cloud instance (CPU)
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

## Auto-capture ideas (user's standing request)

When the user mentions a new idea casually mid-task ("oh, also we should…", "tôi vừa nghĩ ra…", "lâu lâu thấy có ý gì mới"), **don't wait to be told** — append it to `discussions/ideas.md` immediately with today's date and a one-line description, then continue with the current task. Confirm briefly ("đã note vào ideas.md").

## Workspace conventions

- Everything is bypassPermissions-scoped to this folder via `.claude/settings.json`. Don't second-guess routine actions; just do them.
- This project's auto-memory file is the user's global memory at `~/.claude/projects/-Users-thaphan/memory/`. Project-specific state belongs in `STATE.md`, not memory.
- Discussion files: Vietnamese OK, code/commits in English.

## Folder map

```
agent-me/
├── CLAUDE.md            ← you are here (project orientation)
├── STATE.md             ← current stage / next steps (always read first)
├── README.md            ← public-facing description
├── .claude/settings.json ← bypassPermissions
├── discussions/         ← session logs + ideas.md
├── design/              ← architecture docs, ADRs
├── configs/             ← runtime configs (will sync to GitHub)
├── skills/              ← custom skills
└── scripts/             ← bootstrap / deploy / sync
```
