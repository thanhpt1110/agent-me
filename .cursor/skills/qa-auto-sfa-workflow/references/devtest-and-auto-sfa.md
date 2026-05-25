# DevTest Helper and Agent Me Auto SFA

Use this reference for step 3, step 5, and step 6.

## Current Capability Split

There are two different systems:

- `cloud-service-qa/devtest-helper`: creates/query/updates DevTest templates
  from Excel/CSV and creates JIRA tickets from templates.
- `agent-me` Auto SFA: prepares existing templates for SFA and releases SFA
  tasks through `devtest-wrapper`/`dtoperator.py`.

Do not describe Agent Me `create_sfa_tasks` as true template creation. In this
repo it maps to `devtest-wrapper update-template`, which updates matching existing
templates.

## DevTest Helper Commands

Repository:

```text
https://gitlab-master.nvidia.com/cloud-service-qa/devtest-helper.git
```

Setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Template folder:

```bash
python3 main.py -d "\\Nvidia Root Folder\\Library\\...\\MyFolder"
```

Create templates from Excel:

```bash
python3 main.py -f <devtest-test-cases.xlsx> -c
```

Query templates by ID:

```bash
python3 main.py -f <output.xlsx> -q -i "5999793,4999793"
```

Update templates by ID:

```bash
python3 main.py -f <devtest-test-cases.xlsx> -u
```

Known constraints:

- Template create and folder create use the old DevTest API and require a
  GUID-style `API_TOKEN`.
- Query/update/folder lookup use a CQA-to-DevTest bearer token flow.
- Environment variables include `API_TOKEN`, `CQA_ACCESS_TOKEN`, and
  `PROJECT_ID` with default project `1072`.
- If the token path is not ready, generate the Excel and runbook as a dry-run
  artifact, then ask the human to provide credentials or run the command.

## JIRA Tasks From Templates

The current repo exposes JIRA automation under `jira/`.

Environment:

```bash
export JIRA_PAT="<token>"
export JIRA_PROJECT="DGXCAT"
```

Verify epic:

```bash
python jira/create_jira.py --verify-epic DGXCAT-10922
```

Create one ticket:

```bash
python jira/create_jira.py -t 5312876 -e DGXCAT-10922
```

Create tickets from a DevTest folder:

```bash
python jira/create_jira.py -f "\\Folder\\Path" -e DGXCAT-10922
```

Use `--list-only` before writes when the user wants a preview.

## Agent Me Auto SFA Tools

Agent Me exposes `/mcp/` with two schema-only tools:

### `create_sfa_tasks`

Purpose: prepare existing templates before release by running
`devtest-wrapper update-template`.

Required:

```text
display_name
folder_id
```

Optional:

```text
template_ids
template_ids_enabled
win_linux
confirmed
```

Default:

```text
win_linux = "Linux Only"
```

### `release_sfa_tasks`

Purpose: release/auto SFA tasks and handle wording such as "mark auto",
"mark template auto", "release template auto", or "auto these templates".

Required:

```text
display_name
url_path
```

Optional:

```text
release_type
source_folder_id
devtest_folder_id
start_date
finish_date
task_ids
task_ids_enabled
complexity_level
log_file_provider
auto_resolve_destination
confirmed
```

Defaults:

```text
release_type = "Linux Release"
source_folder_id = 50722
finish_date = today in Asia/Ho_Chi_Minh
start_date = finish_date - 7 days
complexity_level = "L2"
```

Rules:

- Never pass a `prompt` argument to Agent Me Auto SFA MCP tools.
- Map user wording into structured fields before calling.
- Use `confirmed=false` only for preview/dry-run.
- After start, show `job_url` for live dashboard progress. Do not poll MCP
  tools unless the user asks for a one-time status check.
