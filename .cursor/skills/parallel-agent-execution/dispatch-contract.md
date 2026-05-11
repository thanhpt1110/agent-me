# Parallel Dispatch Contract

Every subagent gets exactly one queue item. The parent agent is responsible for routing, batching, quality checks, and state aggregation.

## Minimum Prompt Shape

```text
You are executing exactly ONE queue item. Do not spawn further parallel agents.

## Item
id: <item_id>
priority: <priority>
project: <project or unscoped>
task_type: <task_type>
title: <title>
description: <description>

## Routing Decision
dispatch_class: AUTO|DRAFT|PREPARE|PLAYWRIGHT|BLOCKED|HUMAN
why: <one sentence explaining the parent agent's choice>

## Context
<source excerpts, relevant links, recent state, dependency notes>

## Output Directory
<absolute or repo-relative artifact directory>

## Success Criteria
1. Produce a real file under the output directory unless returning BLOCKED or HUMAN_REQUIRED.
2. Do not send, post, approve, merge, delete, or submit forms unless the parent prompt explicitly says this action is approved.
3. Return the JSON schema below as the last block in your response.
```

## Class Instructions

`AUTO`: Execute the task and write evidence/artifacts. If you encounter a risky external write, downgrade to `DRAFT` or `HUMAN_REQUIRED`.

`DRAFT`: Produce a draft file only. Do not send or post it.

`PREPARE`: Produce a dry-run artifact, payload, checklist, or plan. Do not mutate external state.

`PLAYWRIGHT`: Read-only browser inspection is allowed. Form submission requires `HUMAN_REQUIRED`.

`BLOCKED`: Write a short blocker note and set `waiting_on`.

`HUMAN`: Write a short decision summary for the operator.

## Mandatory Return JSON

```json
{
  "item_id": "...",
  "status": "DONE|FAILED|HUMAN_REQUIRED|BLOCKED|SNOOZED",
  "classification_actual": "AUTO|DRAFT|PREPARE|PLAYWRIGHT|BLOCKED|HUMAN",
  "artifacts": ["path/to/file.md"],
  "drafts": [{"channel": "slack|email|other", "path": "path/to/draft.md", "target": "recipient-or-channel"}],
  "external_changes": [{"system": "...", "action": "...", "ref": "..."}],
  "errors": [],
  "notes": "<= 400 chars",
  "next_step": "...",
  "waiting_on": null,
  "snoozed_until": null
}
```

The parent agent must inspect artifacts before marking an item done.
