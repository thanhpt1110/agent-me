# 2026-05-16 - Auto SFA MCP endpoint

## Context

Auto SFA already had two production entry points:

- Slack guided flows, backed by shared parsers/builders.
- Dashboard `/auto-sfa`, backed by the same builders plus the dashboard runner.

The new requirement is to let external agent clients use Auto SFA directly by
adding an MCP server endpoint under the existing `agent-me.nvidia.com` domain.
Users should authenticate once when they add the MCP server in their client,
then run Auto SFA with their own DevTest credentials. The MCP layer must not
call another agent to reinterpret the request after the client has selected a
tool.

## Decisions

- Mount the MCP server on the existing dashboard service at `/mcp/`, so the
  current reverse-proxy/domain setup can route it with no separate process.
- Use the official Python MCP SDK with Streamable HTTP, stateless sessions, and
  JSON responses.
- Authenticate MCP with long-lived Agent Me bearer tokens created by
  `/mcp/setup`. The setup page verifies DevTest username/password once, stores
  the password encrypted server-side, and gives the user install snippets for
  Cursor, Codex, and Claude Code.
- Keep the server deterministic. Agent clients decide which tool to call; the
  MCP server maps that tool directly to existing Auto SFA functions.
- Expose explicit MCP tools:
  - `create_sfa_tasks`
  - `release_sfa_tasks`
  - `get_sfa_job_status` for read-only live progress polling.
- Return structured `needs_input` for incomplete/general requests, with
  `plan_mode_required=true`, so the agent client must clarify before execution.
- Execute complete requests in one tool call after the MCP client/user approves
  the tool invocation. `confirmed` defaults to `true`.
- Tell agent clients to include default choices explicitly in tool arguments
  when possible, especially `release_type="Linux Release"` when the user did
  not ask for `Release`, so the MCP approval UI shows the exact run plan.
- Keep `confirmed=false` as preview/dry-run mode. It returns structured
  `needs_confirmation`, resolved fields, default/alternative options, and a
  legacy `confirmation_token`, but that token is not required for normal
  execution.
- Started jobs return `job_id`, `job_url`, `monitor_tool`, and
  `monitor_arguments`. Agent clients poll `get_sfa_job_status` with
  `since_line_no=next_since_line_no` until `is_terminal=true`.
- Keep release defaults aligned with Slack:
  - Default `Linux Release`, source `50722`.
  - Alternative `Release`, source `47877`.
  - Default finish date is today in `Asia/Ho_Chi_Minh`.
  - Default start date is seven days before finish date.
  - Default complexity is `L2`.
  - Destination folder is auto-resolved unless the caller supplies
    `devtest_folder_id`.
- Surface setup in the Auto SFA UI, not only in docs. The header has a direct
  `MCP Setup` link to `/mcp/setup`.

## Tool Contracts

`create_sfa_tasks`:

- Required: `display_name`, `folder_id`.
- Optional: `template_ids`, `template_ids_enabled`, `win_linux`.
- Default: `win_linux = Linux Only`.
- Natural prompt example: `Create SFA Tasks for "Thanh Phan" in folder "494139"`.
- Runs `magic-auto update-template` through the existing Auto SFA runner.

`release_sfa_tasks`:

- Required: `display_name`, `url_path`.
- Optional: `release_type`, `source_folder_id`, `devtest_folder_id`,
  `start_date`, `finish_date`, `task_ids`, `task_ids_enabled`,
  `complexity_level`, `log_file_provider`.
- Natural prompt example: `Release SFA Tasks for "Thanh Phan" with URL_PATH <link>`.
- Agent clients should also select this tool for wording such as `auto
  template`, `mark template auto`, `release template auto`, or `auto these
  templates` when the intended action is the release/auto flow.

`get_sfa_job_status`:

- Required: `job_id`.
- Optional: `since_line_no`, `tail`, `wait_seconds`.
- Returns: current job status, redacted request details, recent terminal events,
  `recent_lines`, `next_since_line_no`, and `is_terminal`.
- Use `wait_seconds` in the 3-10s range for light long-polling instead of
  rapid repeated polling.

## Auth Behavior

Users do not pass `username` or `password` as tool arguments. They open
`/mcp/setup`, enter DevTest credentials once, and receive an `agm_...` bearer
token. The client sends `Authorization: Bearer <token>` on MCP HTTP requests,
and the server resolves that token to encrypted stored DevTest credentials per
request.

Tokens do not expire by default. The operational model is regenerate/revoke
rather than short expiry; `AUTO_SFA_MCP_TOKEN_TTL_DAYS` can enable expiry if
needed later. The UI derives the endpoint from the current dashboard page
origin, so an HTTP page shows an HTTP MCP URL and an HTTPS page shows an HTTPS
MCP URL. Set `AUTO_SFA_MCP_PUBLIC_BASE_URL` only when the MCP public endpoint
must differ from the page origin. Prefer HTTPS because bearer tokens are
replayable over plain HTTP.

## UI Notes

The Auto SFA page now shows a top-right `MCP Setup` link aligned with the
subtitle. `/mcp/setup` shows the bearer token, one-command install, Cursor
config, Claude command, and Codex config with copy buttons. After token
creation, the browser receives a signed digest cookie; the bearer token itself
is encrypted in the server-side token store. Reopening `/mcp/setup` from the
same browser redisplays the token so it can be reused in another client.
The dashboard footer identifies `NVIDIA VRDC SWQA` and computes the `Last
Update` tag/date from the latest available git release tag, linking that tag
to GitHub.

The dashboard can also reopen a running MCP job with `/auto-sfa?job_id=<id>`.
The Auto SFA SSE route falls back to the MCP runner when the job is not owned
by the dashboard runner, so `job_url` in MCP responses can show the same live
terminal output while the process is still alive. `job_url` derives from the
MCP request origin when no public-base override is set, preserving HTTP for the
current internal `agent-me.nvidia.com` proxy.

## Verification

- `uv run ruff check src tests`
- `uv run pytest -q`
- Focused tests for:
  - MCP bearer-token requirement.
  - MCP bearer token resolving to encrypted stored DevTest credentials.
  - `/mcp/setup` rendering, DevTest verification call, remembered token page,
    and install snippets.
  - MCP token/password store encryption, revoke helper, and installer script
    escaping.
  - MCP tool discovery, including read-only `get_sfa_job_status`.
  - `needs_input` for general release requests.
  - `needs_confirmation` preview mode when `confirmed=false`.
  - Single-call execution for complete create/release requests.
  - Live job progress polling through `get_sfa_job_status`.
  - Dashboard `/auto-sfa?job_id=<id>` fallback for running MCP jobs.
  - Dashboard auth exemption for `/mcp/`.
  - Auto SFA UI direct setup link.
- Live endpoint probes:
  - `/mcp/setup` renders the setup form without dashboard auth.
  - `/mcp/install` returns a valid shell installer.
  - Running the installer in an isolated `/tmp` HOME writes Cursor and Codex
    config.
  - `codex mcp get agent-me --json` parses the generated Codex config with
    persistent `http_headers`.
  - `/mcp/` returns the bearer-token challenge when unauthenticated and lists
    `create_sfa_tasks` / `release_sfa_tasks` / `get_sfa_job_status` through
    the temporary Basic Auth fallback.
  - `/auto-sfa` renders the HTTP-derived `MCP Setup` link.
- Restarted `agent-me-dashboard.service`; service reported `active`.
