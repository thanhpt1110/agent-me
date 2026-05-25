# QA to Automated SFA Manager Demo

## Goal

Show how Agent Me can drive the upstream QA workflow from PRD intake to Auto SFA
release while keeping human approvals at the points that change scope, create
external records, or modify test repositories.

## Demo Flow

1. Open `artifacts/qa-auto-workflow.png` to show the six-step workflow.
2. Open `.cursor/skills/qa-auto-sfa-workflow/SKILL.md` and highlight the
   autopilot entry point:

   ```text
   execute qa-auto-sfa-workflow
   ```

3. Explain that the agent starts at Step 1, reads only the current-step
   references, asks for missing blocker information, and records assumptions.
4. Show `references/intake-and-review.md` for PRD/test-plan conversion and the
   intake manifest gate.
5. Show `references/test-case-excel.md` and
   `artifacts/devtest-test-case-template.xlsx` for the fallback DevTest workbook
   contract.
6. Show `references/devtest-and-auto-sfa.md` to explain the boundary:
   DevTest/JIRA creation stays with `devtest-helper`; Agent Me MCP handles
   existing template prep and Auto SFA release/mark-auto.
7. Open the Agent Me `/auto-sfa` dashboard or an existing dashboard screenshot
   to show the production execution layer and terminal job URL.

## Key Message

The upstream authoring path is now captured as deterministic agent skills and
rules. The production Auto SFA execution layer already exists in Agent Me, and
the skill keeps humans in control at manifest approval, test-case review,
DevTest/JIRA writes, script placement, and MCP tool approval.
