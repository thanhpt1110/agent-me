# scripts — brief + Python CLI helpers

- `daily_brief.py` (`uv run agent-me-brief`) — parallel multi-source brief.
  calendar/outlook/slack fetch via **claude** + `maas-*` MCP read tools
  (`claude_fetcher`); jira/gitlab/nvbugs via direct MaaS MCP JSON-RPC; github via
  `gh`; **teams + the Slack/email mirror stay on codex** (no Claude write/Teams
  connector). Verify read-only with `agent-me-brief --period day --dry-run`.
- `reauth_mcps.py` and friends — MCP token refresh helpers.

Register new console scripts in `pyproject.toml [project.scripts]`.
(Repo-root `scripts/` holds shell helpers; this is the Python package.)
See repo-root `CLAUDE.md` for conventions.
