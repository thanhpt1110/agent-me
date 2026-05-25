---
name: qa-auto-sfa-workflow
description: Orchestrate the NVIDIA VRDC SWQA QA-to-Automated-SFA workflow. Use when a user asks to execute qa-auto-sfa-workflow, process a new QA/test request, turn PRDs, test plans, PDFs, Word docs, or Glean documents into reviewed test cases, DevTest template/task inputs, automation script work, or Agent Me Auto SFA create/release/mark-auto runs.
---

# QA Auto SFA Workflow

Use this skill to drive the six-step QA to Automated SFA workflow without
guessing which tool owns each step.

## Autopilot Mode

If the user says only "execute qa-auto-sfa-workflow", "process this test
request", or provides an incomplete new test request, do not require a perfect
prompt. Start an intake bootstrap, read the references needed for the current
step, and ask only for information that blocks the next gate.

Default behavior:

- Start at Step 1 unless the user explicitly names a later step.
- Create a dated work folder under the current workspace, preferably
  `artifacts/qa-auto-sfa/<YYYYMMDD-HHMM>-<request-slug>/`.
- If source docs or links are present, begin collecting/converting them
  immediately and build the intake manifest.
- If no source is present, ask one short intake question requesting the source
  docs or links, feature/release name, and desired end point if it is not
  Step 1.
- Continue with reasonable defaults for non-blocking fields and record them as
  assumptions in the review artifact.
- Stop only at the required human gates listed below or when a missing value
  prevents the next artifact from being generated.

Minimum intake question when no usable source is available:

```text
Send the PRD/test-plan source for this request: local file path, Glean/Drive/
SharePoint link, or pasted text. Also include the feature/release name and tell
me if you want me to stop after Step 1 or continue to Excel after approval.
```

Gate-specific missing information:

- Step 2 Excel: ask for a sample workbook only if the user requires an exact
  existing template. Otherwise use the fallback DevTest headers.
- Step 3 DevTest/JIRA: ask for target DevTest folder path, create/update mode,
  and JIRA epic only when preparing write commands.
- Step 4 scripts: ask for automation repo, folder, naming pattern, and marker
  before editing code.
- Step 5/6 Auto SFA: ask for `display_name`, `url_path`, and any required
  source/task IDs that cannot be inferred before calling MCP tools.

Do not spawn subagents just to run the workflow. Use this skill's self-checklist
first; only use subagents if the user explicitly asks for parallel agent work.

## Workflow Contract

1. **Review Test Plan**: collect PRD/test-plan inputs, convert source files to
   Markdown, extract scope and coverage obligations, and ask for review before
   generating executable test cases.
2. **Write Test Cases**: generate an Excel workbook with the DevTest template
   schema. Use `openpyxl` for populated `.xlsx` output so headers, wrapping,
   row order, and future formatting are controlled. If a ST2110/API sample
   workbook is provided, treat its headers and sheet layout as the source of
   truth. If no sample/workbook exists, create a new workbook with the fallback
   DevTest headers from `references/test-case-excel.md`; the bundled
   `scripts/create_devtest_excel_template.py` can bootstrap that blank file.
3. **Create DevTest Templates and Tasks**: use
   `cloud-service-qa/devtest-helper` for true DevTest template CRUD and JIRA
   task creation. Use Agent Me `create_sfa_tasks` only for the existing
   Auto SFA template-prep/update-template flow.
4. **Script Completed Test Cases**: draft script work from the test plan and
   test case Excel, but require a human decision for the target automation
   repo, folder, naming pattern, and test marker before editing code.
5. **Release SFA Tasks**: call Agent Me `release_sfa_tasks` with structured
   arguments only after the user approves the resolved run plan.
6. **Mark as Automated**: route "mark auto", "mark template auto", and
   "auto these templates" wording to `release_sfa_tasks`; show the returned
   `job_url` for live terminal progress.

## Routing Rules

- If the user provides local `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.html`, or
  similar product docs, read `references/intake-and-review.md`.
- If the user says the docs are in Glean, Drive, SharePoint, or another MCP
  source, retrieve the files first, then convert or summarize into the same
  intake bundle.
- If the user asks for test case writing or an Excel output, read
  `references/test-case-excel.md`.
- If the user asks to create DevTest templates, template folders, or JIRA
  tasks, read `references/devtest-and-auto-sfa.md`.
- If the user asks to implement scripts, validate scripts, upload pytest HTML
  logs, or choose where scripts should live, read
  `references/script-validation.md`.
- If the user asks to run, release, mark auto, or show Auto SFA progress, use
  the Agent Me MCP/dashboard tool contracts in
  `references/devtest-and-auto-sfa.md`.

## Human Gates

Do not bypass these gates:

- Intake manifest approval before deriving test cases from documents.
- Test case review before creating or updating DevTest templates.
- Explicit approval before DevTest/JIRA writes.
- Target automation repo/folder approval before writing test scripts.
- MCP/client approval before `create_sfa_tasks` or `release_sfa_tasks`.

## Output Bundle

For a complete workflow run, produce these artifacts under a dated work folder:

```text
<workdir>/
  intake/manifest.md
  intake/*.md
  review/test-plan-review.md
  test-cases/devtest-test-cases.xlsx
  test-cases/test-case-review.md
  devtest/devtest-helper-runbook.md
  scripts/script-plan.md
  auto-sfa/auto-sfa-plan.md
  auto-sfa/job-links.md
```

Keep secrets out of artifacts. Redact DevTest passwords, bearer tokens, CQA
tokens, JIRA PATs, and private credential values.

## Bundled Script

When there is no sample workbook and no existing Excel file, run:

```bash
python .cursor/skills/qa-auto-sfa-workflow/scripts/create_devtest_excel_template.py \
  test-cases/devtest-test-cases.xlsx
```

Then populate rows using `openpyxl` or ask the user to review the blank template
before continuing.
