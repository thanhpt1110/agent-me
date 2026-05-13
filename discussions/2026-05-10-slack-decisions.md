# Slack interface decisions resolved — 2026-05-10

Worked through the 4 open questions in `design/slack-app-setup.md` §12.
Important misconception cleared along the way: Slack apps **do not** require
admin approval if you install them into a workspace you own. Approval is only
needed when installing into someone else's workspace (e.g., NVIDIA's).

## Final picks for the upstream deployment

| # | Question | Pick |
|---|---|---|
| 1 | Workspace | **Personal Slack workspace** (user creates a fresh one, is admin) |
| 2 | Sandboxing | **Review-by-default + per-thread auto-approve toggle** (Claude Code-style permission model, Slack buttons as UI) |
| 3 | State path | **ENV var with XDG default**: `${AGENT_ME_STATE_DIR:-${XDG_STATE_HOME:-~/.local/state}/agent-me}` |
| 4 | Streaming | **Hybrid**: 🔄 placeholder → progress every ~30s → final via `chat.update` |

## Why these picks

- **Personal workspace** unlocks zero-approval iteration. Telegram was a
  faster setup but user prefers Slack UI for familiarity. NVIDIA workspace
  was excluded — the agent is for autonomous personal workload, not team
  collaboration.
- **Review-with-toggle** matches the user's mental model from Claude Code
  (`bypassPermissions` per project). The Slack-native button UI makes
  approval cheap (one tap) and the auto-approve toggle preserves agentic
  flow when trust is established.
- **ENV + XDG** keeps the framework fork-friendly without locking forkers
  into a path under the repo. Default just works on Linux/macOS.
- **Hybrid streaming** avoids the rate-limit pitfalls of token-by-token
  `chat.update` while still giving a "thinking…" affordance.

## Architectural implication

Choosing Slack means we need a Node bridge service running on the always-on
host (Cloud host, per Phase 0 decision). The bridge owns:

- Slack Bolt SDK in Socket Mode (no public HTTP endpoint required)
- Per-thread session state in SQLite at the XDG path
- Action interception layer that posts approval prompts and waits for the
  button click before invoking the underlying tool
- Spawn `claude -p` headless with `cwd: ~/agent-me/` so the agent inherits
  `CLAUDE.md` + auto-memory

The action interception is the most novel piece — Claude Code itself
doesn't have a Slack-aware permission hook, so the bridge will need to
either:

(a) Wrap `claude -p` with a custom hook that intercepts pre-tool-use events,
    posts to Slack, and blocks until approval, or

(b) Run a local stop-and-wait pattern in the bridge: parse Claude's stream
    for tool-use events, pause the subprocess, post approval, resume.

Option (a) is cleaner — Claude Code supports `PreToolUse` hooks. To research
in the next session.

## Misconception cleared

User initially asked: "Do we need to publish the app to NVIDIA's Slack
marketplace and get admin approval?"

No. Slack apps are not "published" anywhere unless you explicitly submit to
the public Slack App Directory (a separate, optional step for distribution).
Creating an app at api.slack.com/apps and installing it into your own
workspace is a private operation — only you see/use it.

## Next steps

1. User creates a personal Slack workspace (free) at <https://slack.com/get-started>.
2. User creates a new Slack app at <https://api.slack.com/apps> with the
   scopes listed in `design/slack-app-setup.md` §3, enables Socket Mode (§4),
   subscribes to events (§5), and captures the bot token + app token.
3. Tokens stored in `~/agent-me/configs/.env` (gitignored — see `.gitignore`).
4. Build the bridge service at `~/agent-me/services/slack-bridge/` per §8.
5. Deploy to a cloud host (separate Phase 3 work).

This doc + design-doc edits should land in the same commit.
