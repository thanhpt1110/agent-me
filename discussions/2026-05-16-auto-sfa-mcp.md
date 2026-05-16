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
- Authenticate MCP with HTTP Basic Auth using DevTest username/password. The
  server reads credentials from the request context, passes them to
  `magic-auto`, and redacts the password from all public responses.
- Keep the server deterministic. Agent clients decide which tool to call; the
  MCP server maps that tool directly to existing Auto SFA functions.
- Expose exactly two tools:
  - `create_sfa_tasks`
  - `release_sfa_tasks`
- Return structured `needs_input` for incomplete/general requests, with
  `plan_mode_required=true`, so the agent client must clarify before execution.
- Return structured `needs_confirmation` for complete requests before side
  effects. The response includes a signed `confirmation_token`; execution
  requires `confirmed=true` and the same token.
- Keep release defaults aligned with Slack:
  - Default `Linux Release`, source `50722`.
  - Alternative `Release`, source `47877`.
  - Default finish date is today in `Asia/Ho_Chi_Minh`.
  - Default start date is seven days before finish date.
  - Default complexity is `L2`.
  - Destination folder is auto-resolved unless the caller supplies
    `devtest_folder_id`.
- Surface the endpoint in the Auto SFA UI, not only in docs. The header has an
  `MCP` hover dropdown with a code block and icon-only copy button.

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

## Auth Behavior

Users do not pass `username` or `password` as tool arguments. They configure
their MCP client once with DevTest username/password. The client then sends
Basic Auth on MCP HTTP requests, and the server uses those credentials per
request.

There is no server-side session expiry because the server does not store a
session. Credentials remain usable until DevTest rejects them or the user
changes the client configuration. If the public proxy is HTTP-only, set
`AUTO_SFA_MCP_PUBLIC_BASE_URL=http://agent-me.nvidia.com` so the UI shows the
real endpoint, but prefer HTTPS because Basic Auth is replayable over plain
HTTP.

## UI Notes

The Auto SFA page now shows the MCP endpoint in the top-right dropdown aligned
with the subtitle. Hovering over `MCP` keeps the menu open while moving to the
copy button. The copy button uses `navigator.clipboard.writeText` on secure
contexts and falls back to a temporary textarea plus `document.execCommand("copy")`
for HTTP contexts.

The note text is:

```text
Use DevTest credentials to connect Agent Me MCP.
```

`Agent Me` is styled as a small NVIDIA-green badge so it remains prominent in
both light and dark themes.

## Verification

- `uv run ruff check src tests`
- `uv run pytest -q`
- Focused tests for:
  - MCP Basic Auth requirement.
  - MCP tool discovery.
  - `needs_input` for general release requests.
  - `needs_confirmation` for complete create/release requests.
  - Confirmation-token execution path.
  - Dashboard auth exemption for `/mcp/`.
  - Auto SFA UI endpoint/dropdown/copy markup.
- Official MCP Python client smoke:
  - Initialize local `/mcp/`.
  - List tools.
  - Call `release_sfa_tasks` preview with Basic Auth.
- Browser verification:
  - Open live `/auto-sfa`.
  - Hover MCP dropdown.
  - Force Clipboard API failure.
  - Click copy button.
  - Verify fallback `document.execCommand("copy")` received
    `https://agent-me.nvidia.com/mcp/`.
  - Check `Agent Me` badge computed color/background in light and dark theme.
- Restarted `agent-me-dashboard.service`; service reported `active`.
