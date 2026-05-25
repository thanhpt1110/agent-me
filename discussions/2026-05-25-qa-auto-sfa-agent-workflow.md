# 2026-05-25 - QA to Automated SFA agent workflow packaging

## Context

The user wants to show management how the QA to Automated SFA workflow can be
automated by Agent Me through agent skills/rules, while keeping human review
gates for document intake, test-case review, DevTest/JIRA writes, and script
placement.

## Findings

- `agent-me` already implements the Auto SFA execution layer through the
  dashboard, Slack flow, and MCP tools.
- The MCP boundary is schema-only: external agents must choose
  `create_sfa_tasks` or `release_sfa_tasks` and pass structured arguments.
- `create_sfa_tasks` is template prep for existing templates through
  `magic-auto update-template`; it is not true DevTest template creation.
- `release_sfa_tasks` is the right route for release, auto, and mark-auto
  wording.
- The workflow should support an autopilot prompt so users can say
  `execute qa-auto-sfa-workflow`; the skill then starts intake, reads the
  relevant reference files, and asks only for blocker information at gates.
- The referenced `cloud-service-qa/devtest-helper` repo provides the true
  DevTest template/JIRA tooling:
  - `python3 main.py -f <file.xlsx> -c` for template creation.
  - `python3 main.py -f <file.xlsx> -u` for template update.
  - `python jira/create_jira.py ...` for JIRA task creation.
  - `utils/script_validation_workflow_helper.py` for pytest HTML log upload
    and DevTest script-validation phases.

## Artifacts Added

- `.cursor/skills/qa-auto-sfa-workflow/SKILL.md`
- `.cursor/skills/qa-auto-sfa-workflow/agents/openai.yaml`
- `.cursor/skills/qa-auto-sfa-workflow/references/intake-and-review.md`
- `.cursor/skills/qa-auto-sfa-workflow/references/test-case-excel.md`
- `.cursor/skills/qa-auto-sfa-workflow/references/devtest-and-auto-sfa.md`
- `.cursor/skills/qa-auto-sfa-workflow/references/script-validation.md`
- `.cursor/skills/qa-auto-sfa-workflow/scripts/create_devtest_excel_template.py`
- `design/qa-auto-sfa-agent-workflow.md`
- `artifacts/qa-auto-sfa-agent-rules.yaml`
- `artifacts/qa-auto-sfa-manager-demo.md`

## Notes

No Python runtime was changed in this pass. The new files are docs/rules/skill
artifacts intended to make the manager demo concrete and to guide future agent
execution. The bundled Excel template generator is a standalone skill helper,
not an `agent-me` runtime entry point.
