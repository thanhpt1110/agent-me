# MaaS MCP catalog — production servers

_Source: <https://ipp-safety-tools.gitlab-master-pages.nvidia.com/giza-llm-tools/giza_ai/docs/preprod/tutorial/maas-available-mcps/#production-servers>_

_Captured 2026-05-10. Re-fetch periodically — NVIDIA adds servers
fairly often._

This file is the single source of truth when adding a new MCP to
agent-me. It maps the human-friendly server name to the URL you pass
to `claude mcp add`, the auth method (so you know what `pa login` /
`reauth` will do), and a one-line "what is this useful for" hint.

## Currently registered (15)

`claude mcp list` shows these — already wired into the bridge probe
and brief allowlist:

confluence, gitlab, gdrive, ippsec, jama, jira, mysql, nvbugs (auth
flaky), nvks-prometheus, nsight-cuda, onedrive, pagerduty, sharepoint,
glean, playwright.

## Production catalog

Anything below that we're not using yet is fair game for a future
fan-out subagent.

| Server | URL | Auth | Useful for |
|---|---|---|---|
| NVBugs | `…/maas/nvbugs/mcp` | Azure | bugs assigned to me |
| Jira | `…/maas/jira/mcp` | Jira PAT | tickets I'm on |
| Confluence | `…/maas/confluence/mcp` | Azure | pages I'm mentioned/watching |
| Glean | `…/maas/glean/mcp` | Azure | unified search across ECI |
| Jama Cache | `…/maas/jama_cache/mcp` | Azure | requirements (offline cache) |
| Colossus MySQL | `…/maas/colossus_mysql/mcp` | Azure | DB query |
| Google Drive | `…/maas/gdrive/mcp` | Azure | shared docs |
| OneDrive | `…/maas/onedrive/mcp` | Azure | personal docs |
| SharePoint | `…/maas/sharepoint/mcp` | Azure | team docs |
| **Outlook** | `…/maas/outlook/mcp` | ECI OAuth | email + calendar (*not yet registered*) |
| **Slack** | `…/maas/slack/mcp` | ECI OAuth | DMs / mentions / channel state (*not yet registered*) |
| Gerrit | `…/maas/gerrit/mcp` | Azure | code review (legacy) |
| GitLab | `…/maas/gitlab/mcp` | GitLab PAT | MRs / issues |
| Perforce | `…/maas/p4/mcp` | P4 | source for some legacy projects |
| Nsight Copilot CUDA | `…/maas/nsight_cuda/mcp` | Azure | CUDA docs / samples (current) |
| PagerDuty | `…/maas/pagerduty/mcp` | Azure | on-call / incidents |
| IPPSEC Metrics | `…/maas/ippsec_metrics/mcp` | Starfleet | security posture per repo |
| NVKS Prometheus | `…/maas/nvks_prometheus/mcp` | Azure | cluster metrics |
| Armis | `…/maas/armis/mcp` | Azure | device security inventory |
| SonarQube | `…/maas/sonarqube/mcp` | Azure | static analysis |
| TestPilot | `…/maas/testpilot/mcp` | Azure | test infra |
| CQA | `…/maas/cqa/mcp` | Azure | code QA |
| Swagger Schema Retrieval | `…/maas/swagger_schema_retrieval/mcp` | Azure | API schemas |
| DLSim Assistant | `…/maas/dlsim_assistant_server/mcp` | Starfleet | DL simulation |
| VLM Screen Analysis | `…/maas/vlm_screen_analysis/mcp` | Azure | screenshot reasoning |
| QuerySage | `…/maas/querysage/mcp` | Azure | natural-language SQL |
| Salesforce | `…/maas/salesforce/mcp` | Salesforce | CRM |
| Databricks | `…/maas/databricks/mcp` | Databricks | data jobs |
| Colossus AWX | `…/maas/awx_mcp_server/mcp` | Azure | Ansible jobs |
| SQA-AIPilot | `…/maas/sqa_aipilot/mcp` | Azure | SQA workflows |
| CMI | `…/maas/cmi/mcp` | Azure | CMI |
| Space | `…/maas/space/mcp` | Azure | Space (JetBrains) |
| Ironwise | `…/maas/ironwise/mcp` | Azure | Ironwise |
| NVINFO | `…/maas/nvinfo/mcp` | Azure | NVIDIA people / org / news (we use the CLI version of this) |
| Redmine | `…/maas/redmine/mcp` | Azure | tickets (legacy) |
| NVSpecs | `…/maas/nvspecs/mcp` | Starfleet v2 | NVIDIA specs |
| Blossom Jenkins | `…/maas/jenkins/mcp` | Azure | CI builds |

## How to add one

```bash
claude mcp add --transport http <server-name> '<url>'
# then trigger the OAuth flow:
uv run agent-me-reauth   # spawns 'claude mcp list' under pty, auto-opens browser tabs
# verify:
claude mcp list   # the new entry should show ✓ Connected
```

After adding, also append the new tool prefix to the brief's
`--allowedTools` list in `src/agent_me/scripts/daily_brief.py` (or the
fan-out subagent map, once that lands), and to the bridge's MCP
health probe if it's something the user expects to see in `/mcp`.

## When a server changes auth method

The MaaS docs are the canonical source. If `claude mcp list` starts
showing "Needs authentication" right after a server reauth, re-check
the catalog — sometimes auth methods are migrated (e.g. Azure →
Starfleet) and the OAuth client_id changes.
