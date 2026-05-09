# agent-me — Current State

_Last updated: 2026-05-09 by Claude (Opus 4.7)_

## Phase

**Phase 1 — GitHub published, Slack next.** Repo is live at https://github.com/thanhpt1110/agent-me as a public template. Slack app design doc written; awaiting user decisions before building the bridge.

## Decisions locked

| Topic | Choice |
|---|---|
| Runtime host | Brev cloud CPU instance (24/7) |
| Primary interface | Slack DM/channel (Socket Mode bridge) |
| Config repo | Personal GitHub, **public template** (`thanhpt1110/agent-me`) |
| Default model | Claude Opus 4.7 (1M ctx) |
| Git identity | `includeIf` per-host: github.com → personal, default → NVIDIA |
| License | MIT |

## Done

- [x] Project named **agent-me** ("myself, but in agent mode" — kebab-case, lowercase, public-shareable)
- [x] Folder scaffold at `~/agent-me/` with `discussions/`, `design/`, `configs/`, `skills/`, `scripts/`
- [x] `CLAUDE.md` orientation doc
- [x] `discussions/2026-05-09-kickoff.md` initial design draft
- [x] `.claude/settings.json` with `bypassPermissions` (project-scoped)
- [x] `discussions/ideas.md` for ongoing idea capture
- [x] `~/.gitconfig` `includeIf` rules for per-host identity (github.com → personal)
- [x] `gh` CLI installed (2.92.0) + authed as `thanhpt1110` + `gh auth setup-git`
- [x] `.gitignore` + `LICENSE` (MIT) + polished public-facing `README.md`
- [x] **GitHub repo published:** public template at https://github.com/thanhpt1110/agent-me
- [x] `design/slack-app-setup.md` — end-to-end Slack app + Socket Mode bridge guide

## Next up (in order)

1. **Resolve Slack app blockers** (in `design/slack-app-setup.md` §12):
   - Personal Slack workspace vs NVIDIA workspace for v1
   - Sandboxing posture for `claude -p` (full repo write vs read-only research)
   - State store path strategy (hard-coded vs `XDG_STATE_HOME`)
   - Streaming UX (live `chat.update` vs post-once)
2. **Create Slack app** — follow steps in design doc, capture tokens.
3. **Build Slack ↔ Claude bridge** — Node + `@slack/bolt` Socket Mode service at `services/slack-bridge/`.
4. **Provision Brev instance** — choose region/size, install: claude CLI, node, git, tmux/systemd unit for always-on session.
5. **Port `~/daily-brief/` into agent-me** as first sub-agent.
6. **Build Orchestrator skeleton** — slash command `/route` that dispatches to sub-agents.

## Blockers / pending input

- 4 open questions in `design/slack-app-setup.md` §12 — see "Next up" #1.
- NVIDIA Slack admin approval (Vishal Seth) if going NVIDIA workspace route — file in parallel with personal workspace dev.
- Brev region preference (latency vs cost)? **→ default to us-west-2 unless user says otherwise.**

## Open questions / parking lot

- Memory architecture: keep using auto-memory or externalize to a DB the agent owns?
- Secrets management on Brev: 1Password CLI, sops + age, HashiCorp Vault?
- Audit log: log every action the agent takes for after-the-fact review?
- Web UI dashboard later (Phase 4+)?
