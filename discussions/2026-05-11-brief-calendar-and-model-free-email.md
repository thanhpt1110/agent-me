# 2026-05-11 — Brief Calendar Source + Model Free Email Draft Rule

## Context

The user asked to extend daily/weekly/monthly briefs with meeting context and
to make Outlook email handling recognize the `Model Free 2.0` subject pattern.
The same session also locked a commit convention: Thanh Phan is the primary
author and Codex is a co-author when Codex implements changes.

## Decisions

- Daily/weekly/monthly briefs remain read-only fetch jobs.
- Brief jobs now include an `Outlook Calendar` source in addition to Jira,
  GitLab, Confluence, NVBugs, Slack, Outlook Email, and GitHub.
- Calendar scope:
  - `day`: today's meetings in `Asia/Ho_Chi_Minh`.
  - `week`: next 7 calendar days.
  - `month`: next 30 calendar days.
- Calendar items should include start/end time, organizer, location, online
  status, and a short body/agenda summary if the connector exposes it.
- Calendar meetings do not count as deadline priorities; they stay in their
  own source thread/message.
- NVBugs brief prompt must search QA Eng / QA owner by both shortname
  `thaphan` and full name `Thanh Phan`, plus ARB-related involvement under
  either identity.
- Only important multi-source brief outputs mirror to `thaphan@nvidia.com`
  through the Codex Slack connector. Ordinary Slack chat does not mirror.
- `Model Free 2.0` email action is a normal-chat standing rule, not a brief
  behavior. When the user asks to fetch/search/read/check email related to
  them and a matching Outlook thread subject contains `Model Free 2.0`
  case-insensitively, Codex should create a reply-all draft, not send it.

## Outlook Draft Body

```text
Received. Will start testing today

Best regards
Thanh Phan
```

Shortcut command added: `model free draft`.

It searches Outlook Email for the latest received matching message, fetches
the exact thread, and creates a reply-all draft tied to that message. It should
return a concise status with subject, sender, received time, and source link if
available.

## Smoke Test

Command:

```bash
uv run agent-me-brief --period day --dry-run
```

Latest result after prompt hardening: exit 0, 141s wall-clock, 6 total items,
4 source errors. Earlier run also proved the same calendar path, but had a
different mix of MaaS failures. Treat this as evidence that app connectors are
working and HTTP MaaS exposure/auth still needs follow-up.

Worked:

- Outlook Calendar: 3 meetings returned with start/end, organizer, location,
  and body summaries.
- GitHub: 3 items.
- GitLab returned successfully with 0 items after avoiding projectless issue
  queries.
- Slack returned successfully with 0 items.

Known source failures:

- Jira: `mcp__maas-jira__jira_search is not available in this session's callable tools.`
- Confluence: `Found 0 tools.`
- NVBugs: `NVBugs search v2 tool unavailable: tool_search returned 0 tools`
  and no callable namespace exposed.
- Outlook Email: one smoke run hit `ErrorInvalidUrlQueryFilter`; prompt was
  updated to avoid complex OData filters and retry with simpler search/list
  calls.

Direct MCP smoke:

- `codex mcp list` shows `maas-jira`, `maas-confluence`, `maas-nvbugs`, and
  other HTTP MaaS servers registered with bearer-token env vars.
- A direct JSON-RPC `tools/list` call to Jira MCP with the local bearer token
  returned HTTP 401 `invalid_token`.
- A minimal `codex exec` Jira test reported only maas-playwright and GitHub
  tools as callable, not the HTTP MaaS servers.

Interpretation: the Codex app connectors are usable from `codex exec` for
Outlook Calendar/Slack/GitHub-style flows, but this host still needs a fix for
HTTP MaaS MCP exposure/auth before Jira/Confluence/NVBugs can be reliable
without a fallback client or reauth.

These failures are now surfaced as fetch errors instead of being collapsed into
`nothing pending`.

## Follow-up: Slack Draft Cancellation

Later Slack testing showed that a generic email prompt with
`Model Free 2.0.4` found the right thread but selected the newest matching
message, which was already a user-authored reply. The Outlook connector then
reported `user cancelled MCP tool call` when the bridge tried to draft against
that self-authored message.

Direct Codex connector testing from this session succeeded when the draft was
tied to the latest inbound non-self message from Sergei Nikolaev. The bridge
now routes Model Free email prompts through the dedicated helper instead of the
generic chat path. The helper extracts the exact requested version, treats
spaces and hyphens in `Model Free` / `model-free` as equivalent, selects the
latest inbound non-self message, and always requests one fresh reply-all draft.
It must not skip because an equivalent user-authored reply or previous draft
already exists.

Follow-up service inspection showed the helper completed successfully, but
Slack held the placeholder at `thinking...` because the dedicated route did not
pass the standard Codex progress callback into `spawn_codex`. The helper now
accepts and forwards a progress callback, and the Model Free message route uses
the same throttled Slack progress renderer as generic chat.

## Follow-up: Deterministic Same-Thread Draft Requests

Further Slack testing showed a second failure mode: after the first Model Free
search, user follow-up messages such as "create another reply-all draft for
this email" did not include the words `Model Free`, so the bridge sent them
through generic chat. Generic chat then selected a different email subject such
as `ga-model-free-nim 2.0.4`, which is wrong for the requested
`Model Free 2.0.4` thread.

The bridge now persists a `model_free_threads` row keyed by Slack `thread_ts`.
When a thread has a remembered Model Free subject, follow-up messages containing
draft/confirm/execute/same-email language route back through the dedicated
Model Free helper with the remembered exact subject pattern. This keeps the
target stable across Slack turns and prevents generic chat from choosing a
nearby but different Model Free-related email.

Manual headless testing also confirmed a separate connector limit: even with a
strict prompt and the correct Sergei Nikolaev source message, `codex exec`
Outlook write calls can return `user cancelled MCP tool call`. The route fix
removes the hallucinated target selection and duplicate-skip behavior; a direct
non-headless connector path or Graph-backed draft fallback is still needed if
Slack-driven Outlook draft creation must bypass that confirmation layer.

## Verification

- `python -m compileall src scripts tests`
- `uv run ruff check`
- `uv run pytest` → 78 passed
- `uv run agent-me-brief --period day --dry-run`

## Commit Convention

Use Thanh as the commit author and add Codex as a co-author:

```text
Author: Thanh Phan <thaphan@nvidia.com>
Co-authored-by: Codex <codex@openai.com>
```
