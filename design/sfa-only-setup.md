# SFA-Only Reference Setup

This guide is the shareable entry point for teams that only need the QA to
Automated SFA workflow. It intentionally avoids the personal-assistant parts of
`agent-me`, including Slack chat, daily briefs, Outlook/Teams/Google sources,
and personal state.

## Reference Links

Expected GitLab projects after publish:

```text
agent-me:
https://gitlab-master.nvidia.com/vrdc-swqa/swqa-2e2-workflow/agent-me

devtest-wrapper:
https://gitlab-master.nvidia.com/vrdc-swqa/swqa-2e2-workflow/devtest-wrapper
```

The subgroup landing page is:

```text
https://gitlab-master.nvidia.com/vrdc-swqa/swqa-2e2-workflow
```

## What To Read

Start with these files in `agent-me`:

```text
artifacts/qa-auto-workflow.png
artifacts/qa-auto-sfa-manager-demo.md
design/qa-auto-sfa-agent-workflow.md
.cursor/skills/qa-auto-sfa-workflow/SKILL.md
.cursor/skills/qa-auto-sfa-workflow/references/
```

These are the SFA-specific runtime files:

```text
src/agent_me/auto_sfa.py
src/agent_me/auto_sfa_mcp.py
src/agent_me/auto_sfa_mcp_store.py
src/agent_me/dashboard/auto_sfa_runner.py
src/agent_me/dashboard/templates/auto_sfa.html
```

The paired execution repo is `devtest-wrapper`, which provides
`dtoperator.py`, `configs.json`, and the DevTest operations used by the
dashboard and MCP tools.

## Scope Boundary

Included for SFA:

- QA intake and review skill: `.cursor/skills/qa-auto-sfa-workflow/`
- DevTest Excel/test-case contract: `references/test-case-excel.md`
- DevTest/JIRA handoff contract: `references/devtest-and-auto-sfa.md`
- Auto SFA dashboard: `/auto-sfa`
- Auto SFA MCP endpoint: `/mcp/`
- DevTest wrapper execution through `devtest-wrapper`

Not required:

- `agent-me-bridge`
- Slack app tokens or Slack Socket Mode
- Daily brief commands and source connectors
- Personal Outlook, Teams, Google Drive, GitHub, or Slack connectors
- Personal conversation/session history

## Minimal Clone

Clone the two repos side by side:

```bash
git clone https://gitlab-master.nvidia.com/vrdc-swqa/swqa-2e2-workflow/agent-me.git
git clone https://gitlab-master.nvidia.com/vrdc-swqa/swqa-2e2-workflow/devtest-wrapper.git
```

Install the `agent-me` Python environment:

```bash
cd agent-me
uv sync --dev
cp configs/.env.example configs/.env
```

Install `devtest-wrapper` according to that repo's README, then verify this
command works from the `devtest-wrapper` checkout:

```bash
uv run dtoperator.py --help
```

## Minimal Environment

For an SFA-only dashboard host, set only the values below in
`configs/.env`. Leave Slack and daily-brief values unset unless that host will
also run the personal-agent bridge.

```bash
AGENT_ME_REPO_DIR=/path/to/agent-me
AUTO_SFA_REPO_DIR=/path/to/devtest-wrapper
AGENT_ME_STATE_DIR=/path/to/agent-me-state
DASHBOARD_OPERATOR_ACTION_CODE=REPLACE-ME

# Optional when the dashboard is published behind a proxy.
AUTO_SFA_MCP_PUBLIC_BASE_URL=https://agent-me.example.nvidia.com
AUTO_SFA_MCP_ALLOWED_HOSTS=agent-me.example.nvidia.com,agent-me.example.nvidia.com:*
AUTO_SFA_MCP_ALLOWED_ORIGINS=https://agent-me.example.nvidia.com

# Recommended for stable production tokens.
AUTO_SFA_MCP_CONFIRMATION_SECRET=REPLACE-ME
AUTO_SFA_MCP_CREDENTIAL_KEY=REPLACE-ME
```

The previous environment variable name is still accepted as a compatibility
alias, but new installs should use `AUTO_SFA_REPO_DIR`.

## Run Only The SFA Surface

Start the dashboard only:

```bash
uv run agent-me-dashboard --host 0.0.0.0 --port 8778
```

Use these pages:

```text
/auto-sfa   - Create SFA Tasks and Release SFA Tasks
/mcp/setup  - Create a bearer token for Cursor, Codex, or Claude Code
/mcp/       - Streamable HTTP MCP endpoint
```

Do not start `uv run agent-me-bridge` unless the host also needs Slack chat and
approval workflows.

## Agent Skill Installation

For Cursor, keep the repo copy:

```text
.cursor/skills/qa-auto-sfa-workflow/
```

For Codex on a user workstation, copy the same skill into that user's Codex
skills directory:

```bash
mkdir -p ~/.codex/skills
cp -R .cursor/skills/qa-auto-sfa-workflow ~/.codex/skills/
```

Then the user can start an intake with:

```text
execute qa-auto-sfa-workflow
```

The skill gates external writes at manifest review, test-case review,
DevTest/JIRA write approval, script repo/folder approval, and MCP tool
approval.

## Smoke Test

1. Open `/auto-sfa`.
2. Use personal DevTest credentials in the dashboard or create an MCP token
   from `/mcp/setup`.
3. Run a preview or approved Create SFA Tasks flow against a known template
   folder.
4. Confirm the terminal stream and run history appear in the dashboard.
5. From an MCP client, call `release_sfa_tasks` only after the client approval
   UI shows the resolved run plan.
