# 2026-05-11 — Codex-first migration and MCP capability check

## Why this session existed

User asked whether Codex's current MCP/app capabilities are now good
enough to replace the previous Claude + PA CLI hybrid. The hard
requirements were Teams chat/Graph, Slack messages, Outlook email,
Google Drive, NVBugs/MaaS MCPs, and a practical auth path that can be
bootstrapped on another local machine then copied to this host.

## Capability bench

Codex connector/app tools worked directly for the important message
sources:

- Teams Graph profile/chats/messages/search worked through the Codex
  Microsoft Teams app tools.
- Outlook Graph profile/mail listing worked through the Codex Outlook
  Email app tools.
- Slack DM/channel read worked through the Codex Slack app tools.
- Google Drive recent/fetch worked through the Codex Google Drive app
  tools.
- No Codex first-party NVBugs app tool was available, so NVBugs stays
  on the MaaS MCP path.

PA CLI still works for some sources in headless prompt mode, but on
this host it was weaker for this project shape: Teams, Outlook, and
Google Drive were reachable; Slack was not connected; NVBugs/ECI was
not configured. PA's stdio MCP path remains unusable because it emits
multiple internal initialize responses and violates the single-server
MCP stdio contract.

## Decision

The bridge and daily brief now use Codex as the primary agent runtime:

- `src/agent_me/slack_bridge/app.py` spawns `codex exec --json` and
  `codex exec resume --json`.
- `src/agent_me/scripts/daily_brief.py` uses Codex JSONL output for
  per-source fetches.
- Prompts explicitly direct Codex to use app/MCP tools directly and
  avoid shelling out to PA, Claude, or local CLIs for enterprise reads.
- The SQLite table remains named `claude_sessions` only to avoid a
  migration; it now stores Codex thread/session IDs.

## MaaS MCP auth

Codex CLI can register the NVIDIA MaaS HTTP MCP servers, but
`codex mcp login maas-nvbugs` returned "No authorization support
detected" in `codex-cli 0.130.0`. So native Codex OAuth is not the
auth path for MaaS today.

New auth design:

- `scripts/setup-codex-mcps.sh` registers all 17 MaaS MCPs in Codex.
- HTTP MaaS servers are registered with
  `--bearer-token-env-var AGENT_ME_MCP_TOKEN_<SERVER>`.
- `src/agent_me/mcp_tokens.py` reads `.mcpOAuth` access tokens from
  the existing MaaS credential store (`~/.claude/.credentials.json`
  by default, overrideable via `AGENT_ME_CLAUDE_MCP_CREDENTIALS`) and
  injects those env vars into Codex subprocesses.
- `uv run agent-me-codex-reauth` delegates to the proven MaaS OAuth
  helper to refresh that token store. Claude remains an auth bootstrap
  implementation detail, not the agent backend.
- Reauth skips connector-covered MaaS duplicates by default:
  `maas-gdrive`, `maas-outlook`, and `maas-slack`. Those remain
  registered as fallback MCPs, but Codex uses the richer Google Drive,
  Outlook, and Slack connectors first.

Verified after setup:

- `codex mcp list` shows 16 HTTP MaaS servers with `Auth: Bearer token`.
- `maas-playwright` remains a stdio server.
- `codex mcp get maas-nvbugs --json` includes
  `bearer_token_env_var: AGENT_ME_MCP_TOKEN_MAAS_NVBUGS`.
- Running `scripts/setup-codex-mcps.sh` again is idempotent.

Credential caveat on this host: the current copied credential store has
15 usable MaaS access tokens. The `maas-nvbugs` entry exists but has no
access/refresh token, and `claude mcp list` currently flags both
`maas-gitlab` and `maas-nvbugs` as needing authentication. The Codex
configuration is complete; the remaining work is refreshing/copying the
MaaS token store.

## Source updates

- Bridge runtime, prompt, session resume, `/mcp`, `/version`, and
  `/reauth` now point at Codex. `/mcp` and the periodic health check
  also flag registered bearer-token MCPs whose env var has no local
  token in the copied MaaS credential store.
- Daily brief source fetchers now run through Codex.
- Dashboard MCP health and session trace resolution now use Codex
  config/session paths.
- README, `.env.example`, bootstrap script, and `pyproject.toml`
  document the Codex path.

## Verification

- `python -m compileall src scripts tests`
- `uv run ruff check`
- `uv run pytest` -> 71 passed
- `scripts/setup-codex-mcps.sh` -> idempotent, 17 already present
