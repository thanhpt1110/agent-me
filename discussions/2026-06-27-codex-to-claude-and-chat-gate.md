# 2026-06-27 — codex→claude agent backend, /chat password gate, mobile UX (branch `big-update`)

## Goal
Operator wants to drive agent-me from anywhere without the terminal: prompt over
Slack like a remote terminal, and use the dashboard `/chat` behind a password.
Switch the interactive agent backend from **codex → claude** everywhere, run it on
this workspace, and make the chat surface prominent + mobile-friendly.

## Decisions
- **One shared engine, three surfaces.** Extracted `src/agent_me/claude_agent/`
  (the `claude` CLI spawner + event model, generalised from the dashboard chat
  runner with `allowed_tools`/`append_system_prompt`/`permission_mode`). `/chat`,
  the Slack bridge, and the brief all use it, so they behave identically. The
  dashboard chat `runner`/`models` re-export from it to avoid a fork.
- **Slack agent runs in the repo workspace.** Reverses the old "chat-cwd, not
  REPO_DIR" decision: the operator explicitly wants terminal-grade remote work
  (edit files, run shell/git, call MCP). Sessions persist per Slack thread
  (`claude_sessions` table; mint a uuid on turn 1, `--resume` after). `AGENT_BACKEND`
  toggles claude (default) vs codex for rollback.
- **/chat gate reuses the operator passcode**, cookie-based. `EventSource` can't
  send headers, so a header-only gate can't protect SSE — instead `POST
  /api/chat/unlock` validates `DASHBOARD_OPERATOR_ACTION_CODE` and sets a signed
  `agent_me_chat_access` cookie that every `/api/chat/*` + SSE call checks. Gate is
  off when no passcode is configured (never lock the operator out).
- **Brief is hybrid by necessity.** `maas-slack`/`maas-outlook` are read-only MCPs
  and there is no Claude Teams MCP, so calendar/outlook/slack reads moved to claude
  while **Teams and the Slack/email mirror stay on codex** (operator-approved
  "fallback codex" for the connectors Claude can't cover).
- **Chat prominence in `base.html`, not `nav.html`.** A floating Chat button lives
  in `base.html` (this session's file) to avoid colliding with the qa-handsfree
  session's active nav/CSS pass. Global responsive + menu-icon best-practice is
  delegated to that session (it owns nav/app.css/templates).
- **Repo-wide clean-code rule** added to `CLAUDE.md` at the operator's request:
  no redundant comments, explicit names, rationale in docs not source.

## Multi-session coordination
A second session (`~/agent-me-qa-handsfree`) was prompted to also work in this
repo and ran a broad dashboard refactor concurrently. We partitioned via
`COORDINATION.md`: it owns dashboard UI/probe files, this session owns the
`claude_agent` engine, the Slack bridge, the brief, and the chat gate (`app.py`,
`chat.html`, `base.html`, which it reverted to HEAD for us). A full working-tree
backup was taken before any risk (`~/agent-me-conflict-backup-<ts>/`).

## Verified
`ruff` clean; full `pytest` green (new `test_chat_gate.py`, `test_slack_bridge_claude.py`);
real claude turn + `--resume`; live `/chat` turn on :8787 (gate→unlock→PONG);
live brief calendar fetch via claude+`maas-outlook` returned a real meeting.

## Next
Global responsive/menu pass (qa-handsfree session); full brief `--dry-run`
verification; a Claude Teams/Slack-write path to retire the codex fallback.
