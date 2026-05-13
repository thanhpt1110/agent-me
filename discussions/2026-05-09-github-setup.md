# GitHub setup + Slack research — 2026-05-09

## Outcomes

- **GitHub repo live:** https://github.com/thanhpt1110/agent-me — **public template**, MIT license, topics: ai-agent, claude, personal-assistant, automation, mcp.
- **Per-host git identity:** `~/.gitconfig` now uses `includeIf hasconfig:remote.*.url:...` to auto-switch identity based on remote host. github.com → personal (`thanhpt1110 <thanhphantuan1110@gmail.com>`), everything else → NVIDIA default (`Thanh Phan <thaphan@nvidia.com>`). Two patterns added (HTTPS form + SSH form) because the wildmatch glob requires explicit URL prefixes.
- **`gh` CLI:** installed (2.92.0), authenticated as `thanhpt1110` via web browser device flow, `gh auth setup-git` configured the credential helper so HTTPS pushes don't prompt for password.
- **Slack design doc written:** `design/slack-app-setup.md` (12 sections, ~14 KB) — workspace decision, app creation, scopes, Socket Mode rationale, bridge architecture, threading, security checklist, NVIDIA approval notes.

## Key decisions made this session

1. **Visibility:** public template repo (vs private + collaborators) so anyone can `Use this template` and have their own `agent-me`.
2. **License:** MIT.
3. **Git identity strategy:** `includeIf` per-host instead of per-repo local config — global rule, applies to all future repos automatically.
4. **Slack transport:** Socket Mode (no public HTTP endpoint, no ngrok/Cloudflare Tunnel needed; works behind Cloud host NAT).
5. **Bridge stack:** Node.js + `@slack/bolt` Socket Mode SDK, spawn `claude -p` headless with `cwd: ~/agent-me/` so the agent inherits `CLAUDE.md` context per turn.

## Open questions raised by Slack design doc

Listed in `design/slack-app-setup.md` §12 — copy-pasted here for visibility:

1. **Personal vs NVIDIA Slack workspace for v1?** NVIDIA needs admin approval (Vishal Seth) per scope; none of our scopes is on the banned list, so approval should be routine. Personal workspace lets us start immediately.
2. **Sandboxing posture for `claude -p`** — full repo write access (current default) vs read-only research mode? Biggest unresolved security decision.
3. **State store path** — hard-coded `~/agent-me/state.db` vs `XDG_STATE_HOME` for fork friendliness?
4. **Streaming UX** — live `chat.update` ("typing") vs post-once when Claude finishes?

## Issues encountered + fixes

- **First includeIf pattern (`**github.com**`) didn't match.** Fix: split into HTTPS (`https://github.com/**`) and SSH (`git@github.com:**`) patterns. wildmatch with `WM_PATHNAME` flag treats `/` as significant, so leading `**` doesn't behave like a free-form substring match.
- **Initial commit was made before remote was added** — would have been NVIDIA identity if not for the remote being added first to trigger `includeIf`. The order matters: `git remote add` before first commit if you rely on `includeIf` for identity.

## Files added this session

- `~/.gitconfig-github` (new) — supplementary identity for github.com remotes
- `~/.gitconfig` (edited) — added two `includeIf` blocks
- `~/agent-me/.gitignore`, `~/agent-me/LICENSE` (MIT), `~/agent-me/scripts/bootstrap.sh` (placeholder)
- `~/agent-me/README.md` (polished for public template — Quickstart, Architecture, Status, License)
- `~/agent-me/design/slack-app-setup.md` (Slack bridge end-to-end guide)
- `~/agent-me/STATE.md` (updated to Phase 1)

## Next session starting point

Open `design/slack-app-setup.md` §12 and resolve the 4 open questions, then start the Slack app creation flow described in §2.
