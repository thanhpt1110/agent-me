---
name: parallel-agent-execution
description: Drain a focused slice of the agent-me parallel queue and fan items out to multiple subagents. Use when the user asks to run queued work in parallel, drain queue items, fan out tasks, process pending platform work, or execute a filtered batch with AUTO/DRAFT/PREPARE/PLAYWRIGHT/BLOCKED/HUMAN routing.
---

# Parallel Agent Execution

Use this skill to execute a filtered set of queued work items with parallel subagents.

## Architecture

Python is state plumbing. The agent is the reasoning layer.

- `agent_me.parallel_queue` reads/writes the queue, resolves filters, suggests dispatch classes, claims items, and aggregates judged returns.
- This skill decides whether the suggested class is correct, builds batches, dispatches subagents, checks artifacts, then calls aggregation.

Do not blindly trust `dispatch_class`. It is a hint.

## Queue CLI

Run from the repo root:

```bash
uv run agent-me-parallel-queue plan --view today --priority HIGH
uv run agent-me-parallel-queue dry-run --view all --task-type slack-response
uv run agent-me-parallel-queue claim --view today --priority CRITICAL --run-id <id>
uv run agent-me-parallel-queue aggregate --run-id <id> --returns-file <path>
```

Queue state defaults to:

```text
${AGENT_ME_PARALLEL_QUEUE_DIR:-${AGENT_ME_STATE_DIR:-~/.local/state/agent-me}/parallel-queue}
```

## Dispatch Classes

- `AUTO`: Execute end-to-end if standing authorization covers the work. Do not send Slack/email or mutate risky external systems unless the repo has an explicit approval rule for that action.
- `DRAFT`: Produce a draft only. Do not send, post, merge, approve, or commit.
- `PREPARE`: Produce a dry-run artifact, payload, plan, or evidence bundle without external side effects.
- `PLAYWRIGHT`: Read-only browser inspection is allowed. Form submission or state change becomes `HUMAN_REQUIRED`.
- `BLOCKED`: Do not execute. Write the missing prerequisite.
- `HUMAN`: Write a short decision summary for the operator.

## Workflow

1. Parse the user-requested filters. Refuse a fully open drain unless the user explicitly asks for `--view triage` or confirms broad execution.
2. Run `plan` first and inspect the JSON candidates.
3. Drop items with dependencies, shared-state collisions, or unclear instructions. Mark them `BLOCKED` or keep them queued.
4. Batch the survivors:
   - Default max concurrency: 8.
   - Hard cap: 16 unless the user explicitly asks for more.
   - Avoid running two items concurrently if they target the same external object or Slack thread.
5. Run `claim` for the chosen filter/run id.
6. For each claimed item, craft one subagent prompt using `dispatch-contract.md`. Include why you chose the class, any source context, output directory, and the mandatory return JSON schema.
7. Dispatch all subagents in one assistant message when the batch is parallel-safe.
8. Read each subagent return. Open produced artifacts before accepting success.
9. Pass your judged returns, not raw untrusted returns, to `aggregate`.
10. Summarize completed, failed, blocked, human-required, and draft items for the user.

## Guardrails

- No nested fan-out. Subagents must not invoke this skill.
- No fully open drains by accident.
- No send/post/approve/merge/delete operations from `AUTO`.
- `DONE` without a real artifact should usually be judged `FAILED`.
- If an item needs human approval, create a draft or decision summary and return `HUMAN_REQUIRED`.

## Reference

- `dispatch-contract.md` defines the minimum subagent prompt and return JSON.
