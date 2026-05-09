# agent-me — Current State

_Last updated: 2026-05-09 by Claude (Opus 4.7)_

## Phase

**Phase 0 — Kickoff & alignment.** Folder scaffolded, key decisions made, no code yet.

## Decisions locked

| Topic | Choice |
|---|---|
| Runtime host | Brev cloud CPU instance (24/7) |
| Primary interface | Slack DM/channel |
| Config repo | Personal GitHub, private |
| Default model | Claude Opus 4.7 (1M ctx) |

## Done

- [x] Project named **agent-me** ("myself, but in agent mode" — kebab-case, lowercase, public-shareable)
- [x] Folder scaffold at `~/agent-me/` with `discussions/`, `design/`, `configs/`, `skills/`, `scripts/`
- [x] `CLAUDE.md` orientation doc
- [x] `discussions/2026-05-09-kickoff.md` initial design draft
- [x] `.claude/settings.json` with `bypassPermissions` (project-scoped)
- [x] `discussions/ideas.md` for ongoing idea capture

## Next up (in order)

1. **Confirm Slack app strategy** — own Slack app vs reuse existing NVIDIA bot? Need bot token + signing secret + channel ID.
2. **Create personal GitHub private repo** — `agent-me`. User pushes initial scaffold.
3. **Provision Brev instance** — choose region/size, install: claude CLI, node, git, tmux/systemd unit for the always-on session.
4. **Port `~/daily-brief/` into agent-me** as first sub-agent.
5. **Build Orchestrator skeleton** — slash command `/route` that dispatches to sub-agents.
6. **Slack ↔ Claude bridge** — small Node service that forwards Slack messages to `claude -p` and posts replies back.

## Blockers / pending input

- Slack app permission: does user need NVIDIA IT approval to register a workspace bot, or use a personal Slack? **→ ask next session.**
- GitHub PAT scope: classic vs fine-grained? **→ user decides when creating repo.**
- Brev region preference (latency vs cost)? **→ default to us-west-2 unless user says otherwise.**

## Open questions / parking lot

- Memory architecture: keep using auto-memory or externalize to a DB the agent owns?
- Secrets management on Brev: 1Password CLI, sops + age, HashiCorp Vault?
- Audit log: log every action the agent takes for after-the-fact review?
- Web UI dashboard later (Phase 4+)?
