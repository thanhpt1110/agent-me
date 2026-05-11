# Ideas — running log

Append-only. User drops ideas mid-conversation; Claude captures them here without being asked. Triage to design docs / STATE.md `Next up` later.

Format: `- YYYY-MM-DD — <one-line idea>  _(context: where it came from)_`

---

- 2026-05-09 — Always auto-capture user's mid-task ideas into this file without waiting for explicit instruction. _(context: kickoff session, user explicit ask)_
- 2026-05-10 — Consider SvelteKit if the Jinja+Alpine dashboard ever feels too plain. Don't jump to Flutter Web — bundle/UX trade-off is wrong for "open and read report" use case. _(context: Phase 4 FE stack discussion; rejected Flutter Web)_
- 2026-05-10 — Add a chat tab to the dashboard later (separate `chat_sessions` table, never touch bridge's `claude_sessions`). Out of Phase 4 draft scope; come back when Slack chat UX limitations bite. _(context: user mentioned "đôi khi chat" while picking the tunnel)_
- 2026-05-10 — Once Phase 4 lands on Colossus, also publish `agent-me-watch.service` to auto-redeploy dashboard on every git push (same pattern as bridge). _(context: dashboard install script doesn't auto-update yet)_
- 2026-05-10 — Consider Cursor Background Agents for long-running maintenance tasks (auto-redeploy, prompt tuning runs) so they survive across local-machine reboots. _(context: user asked if I could keep working while they shut down their laptop)_
- 2026-05-10 — Phase 2b.1 ideas: safe-Bash auto-allow (`ls`, `git status`, `cat README.md`) to reduce approval fatigue; per-server MCP rules in `approvals.HOOK_MATCHER`; an audit-log export endpoint on the dashboard. _(context: deferred from Phase 2b v1 to keep the surface minimal)_
- 2026-05-10 — Phase 2b: experiment with `permissionDecision: "defer"` instead of file-system semaphore once timeouts feel painful; lets `claude -p --resume <sid>` re-fire the hook so we don't hold a subprocess open across long human waits. Constraint: "single tool per turn" — would need to disable parallel tool batches. _(context: subagent A research; chose semaphore for v1 portability)_
- 2026-05-11 — When the reverse proxy at `agent-me.nvidia.com` gains an `X-Auth-User` header (or any SSO-provided identity), add a dashboard middleware that records who triggered each refresh / approval. Trivial: just log the header value next to existing `query_handled` etc. events. _(context: reverse-proxy pivot; we trust VPN for now but identity is free if proxy adds it)_
- 2026-05-11 — PA CLI hybrid attempt (interrupted): PA's `-p` mode requires PTY/TTY (hangs without one). Workable via `pty.openpty()` like `agent-me-reauth` does, OR use `pa mcp` to expose PA as a Claude MCP server (cleanest — Claude treats PA as another tool). Picked up if PA route becomes the priority again. _(context: 2026-05-11 PA install + bench; pivoted to reverse-proxy work mid-session)_
