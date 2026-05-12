# 2026-05-12 — Daily Brief Source Hardening

## Context

The user reported that NVBugs was working in the daily brief, but Jira and
Outlook were unreliable. Later they decided Confluence should not be included
in the brief at all, and asked for GitLab to focus on merge requests and code
reviews:

- open MRs authored by the user that are awaiting review
- open MRs where the user is assigned as reviewer
- recently merged MRs from the last 3 days

The goal was reliability over minimal constraints: each platform can have its
own strict prompt/fetch path if that prevents tool conflicts or wrong query
behavior.

## Decisions

- Confluence is removed from the active daily/weekly/monthly brief fan-out.
  Its MCP registration and normal chat/write routing remain available.
- Jira, GitLab, and NVBugs brief reads bypass Codex tool discovery and call
  MaaS MCP JSON-RPC endpoints directly with bearer tokens from the refreshed
  credential store.
- NVBugs uses exactly two structured searches:
  `QAEngineerFullName = "Thanh Phan"` and
  `ActionReqByFullName = "Thanh Phan"`, then merges/dedupes by bug id.
- GitLab uses the MaaS GitLab MR tool with `scope="me"` and explicit role/state
  groups:
  - `state="opened"`, `role="author"` -> `authored_waiting_review`
  - `state="opened"`, `role="reviewer"` -> `review_requested`
  - `state="merged"` with author/reviewer/assignee roles, filtered to the last
    3 days -> `recently_merged`
- Outlook Email keeps the Codex app connector path, but the source prompt is
  list-first and forbids OData/search filters that caused
  `ErrorInvalidUrlQueryFilter`. The fetcher retries with a list-only prompt for
  filter and transient connector transport failures.
- Dashboard brief refresh source metadata now mirrors the 7 active brief
  sources: Jira, GitLab, NVBugs, Slack, Outlook, Outlook Calendar, GitHub.

## Verification

Commands run:

```bash
uv run pytest
uv run ruff check .
uv run python - <<'PY'  # source-specific GitLab smoke
uv run agent-me-brief --period day --dry-run
git diff --check
```

Results:

- Full tests: `101 passed`
- Ruff: all checks passed
- GitLab source-specific smoke: exit 0, no error, currently 0 matching items
- Full brief dry-run: exit 0, `n_subagents=7`, `err_count=0`
- Full brief source counts from the final smoke:
  - Jira: 12
  - GitLab: 0
  - NVBugs: 5
  - Slack: 0
  - Outlook: 7
  - Outlook Calendar: 3
  - GitHub: 3

## Runtime

After pushing, both local services were restarted and checked:

```bash
systemctl --user restart agent-me-bridge.service
systemctl --user restart agent-me-dashboard.service
systemctl --user is-active agent-me-bridge.service
systemctl --user is-active agent-me-dashboard.service
```

Both services reported `active`, and the bridge log showed a clean startup with
`need_auth_count=0`.

## Commit

Implemented in:

```text
0fa7329 Harden daily brief source fetchers
```

Author: `Thanh Phan <thaphan@nvidia.com>`

Co-authored-by: `Codex <codex@openai.com>`
