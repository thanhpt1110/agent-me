# Slack Bridge Approval Flow Design

**Goal:** Intercept all Claude Code tool actions (file writes, shell commands, git commits, MCP writes) and pause execution pending async Slack approval before proceeding.

---

## 1. PreToolUse Hook Mechanics

### Registration

Define `PreToolUse` hooks in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|Bash|mcp__.*",
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/slack-approval.sh",
            "timeout": 3600
          }
        ]
      }
    ]
  }
}
```

- **`matcher`**: Filters which tools trigger the hook. Supports tool names (`Write`, `Edit`, `Bash`) and regex patterns (`mcp__.*` for all MCP tools).
- **`type`**: `"command"` spawns a shell script; alternatives include `"http"`, `"mcp_tool"`, `"prompt"`, `"agent"`.
- **`timeout`**: Seconds the hook may run. Default 600s. Set to 3600 to allow 1-hour approval window.

### Hook Input Data

Claude Code passes this JSON on stdin:

```json
{
  "session_id": "abc123",
  "transcript_path": "/home/user/.claude/projects/.../transcript.jsonl",
  "cwd": "/home/user/my-project",
  "permission_mode": "default",
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {
    "command": "rm -rf /tmp/build",
    "description": "Clean build artifacts"
  },
  "tool_use_id": "toolu_01abc",
  "effort": { "level": "medium" }
}
```

**Tool-specific `tool_input` fields:**

| Tool | Key Fields |
|------|-----------|
| **Bash** | `command`, `description`, `timeout`, `run_in_background` |
| **Write/Edit** | `file_path`, `content` (Write) or `old_string`, `new_string` (Edit) |
| **Read** | `file_path`, `offset`, `limit` |
| **MCP tools** | `tool_name` (in hook input), plus server-specific parameters |

### Hook Return Decision

The hook must return JSON on stdout with a `hookSpecificOutput` object:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow|deny|ask|defer",
    "permissionDecisionReason": "explanation",
    "updatedInput": { "field": "new_value" },
    "additionalContext": "context for Claude"
  }
}
```

**Decision semantics:**

| Decision | Behavior | Use Case |
|----------|----------|----------|
| **`allow`** | Execute immediately, skip permission prompt | Pre-approved, low-risk actions |
| **`deny`** | Block tool call; reason shown to Claude | Security policy violation |
| **`ask`** | Prompt user for confirmation | Interactive mode only |
| **`defer`** | Exit gracefully; caller resumes later | Headless mode (`claude -p`) |
| (No response) | If hook times out, treated as non-blocking error | Tool executes; timeout logged in debug |

**Exit codes:**
- **`0`**: Success (proceed based on decision in JSON)
- **`2`**: Block error (non-JSON stderr shown to Claude; no hook JSON required)
- **Other**: Non-blocking error (logged, execution proceeds)

---

## 2. Synchronous-Block Pattern & Timeout Behavior

### Current Mechanics

**PreToolUse hooks block synchronously by default.** Claude's tool execution is held pending until the hook completes. This is deterministic: no special configuration needed.

```bash
#!/bin/bash
# This hook blocks tool execution while waiting for Slack approval
# Claude will not proceed until this script exits
```

**Timeout behavior:**
- If hook exceeds the configured `timeout` (e.g., 3600s), Claude Code logs a non-blocking error.
- The tool **still executes** after timeout—the hook does not hold the execution indefinitely.
- Error shown to Claude: `"Hook timed out after 3600s. Tool execution proceeded."`

### Async Approval Pattern (Recommended for Slack)

For long-lived approvals (e.g., waiting for a Slack button click that may come 30 seconds to 5 minutes later), **use a polling + file-system semaphore pattern**:

```bash
#!/bin/bash
# 1. Send request to Slack bridge (non-blocking HTTP)
# 2. Poll a decision file until approval arrives or timeout
# 3. Return decision to Claude

TOOL_ID="$tool_use_id"
DECISION_FILE="$HOME/.slack-bridge/decisions/$TOOL_ID"

# Send async request to Slack bridge
curl -X POST http://localhost:3033/request \
  -d @- \
  -H "Content-Type: application/json" < /dev/stdin &

# Poll for decision (with timeout)
DEADLINE=$(($(date +%s) + 300))  # 5-minute approval window
while [ $(date +%s) -lt $DEADLINE ]; do
  if [ -f "$DECISION_FILE" ]; then
    # Read and return the decision
    cat "$DECISION_FILE"
    rm "$DECISION_FILE"
    exit 0
  fi
  sleep 1
done

# Timeout—deny by default
jq -n '{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Slack approval not received within 5 minutes"
  }
}'
exit 0
```

**Key points:**
- The hook **itself blocks** (synchronously) until approval arrives or timeout.
- Claude's execution is held the entire time.
- The Slack bridge owns the IPC mechanism (file-system semaphore, shared SQLite, etc.).
- No re-invocation needed; one call per tool action.

---

## 3. Hook → Bridge IPC Patterns

The hook is a shell script; the bridge is a long-running service. Three patterns:

### Option A: File-System Semaphore (Recommended for MVP)

**Pros:**
- Simple, no extra services (except the bridge itself).
- Atomic operations via `mkdir` / `touch`.
- Works across privilege boundaries.
- Debuggable (files on disk).

**Cons:**
- Polling adds latency (1-5s per check).
- Race conditions if not careful (use `mkdir` for atomic lock).

**Implementation:**

```bash
# Hook: ~/.claude/hooks/slack-approval.sh
TOOL_ID="$tool_use_id"
REQUEST_FILE="/tmp/slack-bridge/requests/$TOOL_ID.json"
DECISION_FILE="/tmp/slack-bridge/decisions/$TOOL_ID"

# Write request
mkdir -p /tmp/slack-bridge/{requests,decisions}
cat /dev/stdin > "$REQUEST_FILE"

# Signal bridge
curl -s http://localhost:3033/request \
  -d "{\"tool_id\": \"$TOOL_ID\", \"request_file\": \"$REQUEST_FILE\"}" &

# Poll for decision
DEADLINE=$(($(date +%s) + 300))
while [ $(date +%s) -lt $DEADLINE ]; do
  if [ -f "$DECISION_FILE" ]; then
    cat "$DECISION_FILE"
    rm "$DECISION_FILE"
    exit 0
  fi
  sleep 2
done

echo '{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "timeout"}}' >&1
exit 0
```

### Option B: Unix Domain Socket

**Pros:**
- Low-latency, no polling.
- Bidirectional communication.
- Works on localhost only (secure).

**Cons:**
- Requires socket library (bash + `nc` with `-U` flag, or Python subprocess).
- More complex error handling.
- Less portable (Linux/macOS only; Windows needs WSL).

**Sketch:**

```bash
# Hook: sends request, reads response
request=$(cat)
tool_id=$(jq -r '.tool_use_id' <<< "$request")

# Send request and block for response
echo "$request" | nc -U /tmp/slack-bridge.sock > /tmp/response_$tool_id.json

# Return response
cat /tmp/response_$tool_id.json
```

### Option C: HTTP Loopback with Long-Polling

**Pros:**
- Language-agnostic.
- Can add auth headers (Bearer token).
- Easy to mock/test.

**Cons:**
- HTTP overhead; slower than sockets.
- Hook must implement long-polling loop.
- Requires timeout handling.

**Sketch:**

```bash
request=$(cat)
tool_id=$(jq -r '.tool_use_id' <<< "$request")

# POST request, long-poll response
response=$(curl -s --max-time 300 \
  -X POST http://localhost:3033/approval \
  -d "$request" \
  -H "Content-Type: application/json")

echo "$response"
```

### Option D: Named Pipes (FIFO)

**Pros:**
- Truly bidirectional, no polling.
- Atomic operations.
- Simple in bash.

**Cons:**
- One reader/one writer semantics.
- Not ideal for concurrent requests.

---

## 4. Read-Only vs. Write-Touching Tools

### Strategy

**Only require approval for write/side-effect tools.** Reads (`Read`, `Grep`) and read-only MCP tools pass silently.

### Implementation via Matcher

Use a restrictive matcher in `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|Bash|mcp__jira__.*create|mcp__gitlab__.*merge",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/slack-approval.sh",
            "timeout": 3600
          }
        ]
      }
    ]
  }
}
```

This matcher **only fires for:**
- `Write`, `Edit` (file mutations)
- `Bash` (shell commands, many side effects)
- MCP tools matching `mcp__jira__.*create` or `mcp__gitlab__.*merge` (explicit writes)

**`Read`, `Grep`, `WebFetch`, `WebSearch`, MCP read tools are not matched—they execute without hook.**

### Inside the Hook: Conditional Approval

Optionally, the hook itself can discriminate further:

```bash
#!/bin/bash
input=$(cat)
tool_name=$(jq -r '.tool_name' <<< "$input")
tool_input=$(jq '.tool_input' <<< "$input")

# Some Bash commands are safe (ls, echo, etc.)
if [[ "$tool_name" == "Bash" ]]; then
  command=$(jq -r '.tool_input.command' <<< "$input")
  if [[ "$command" == ls* ]] || [[ "$command" == echo* ]]; then
    echo '{"hookSpecificOutput": {"permissionDecision": "allow"}}'
    exit 0
  fi
fi

# Otherwise, require Slack approval
# ... poll Slack bridge ...
```

---

## 5. Tool Name Reference

### Built-in Tools in PreToolUse Hooks

- `Bash` — Shell commands
- `Write` — Create new files
- `Edit` — Modify existing files
- `Read` — Read files (use matcher to **exclude** from approval)
- `Glob` — Find files
- `Grep` — Search content (use matcher to **exclude**)
- `WebFetch`, `WebSearch` — Web tools (typically read-only)
- `Agent` — Spawn subagent
- `AskUserQuestion` — Prompt user

### MCP Tool Names

MCP tools are named `mcp__<server>__<tool>` in hook input and matchers:

- `mcp__jira__jira_create_issue` (from the Jira MCP server)
- `mcp__gitlab__gitlab_merge_request_create`
- `mcp__github__.*` (regex to match all GitHub tools)

**Example matcher:**
```json
"matcher": "mcp__.*"  // All MCP tools
"matcher": "mcp__jira__.*"  // All Jira tools
"matcher": "mcp__jira__jira_create_issue"  // Specific tool
```

**Confirmation:** MCP tool calls **flow through PreToolUse hooks** with the same mechanism as built-in tools. The `tool_name` in hook input will be `mcp__server__tool`.

---

## 6. Alternative: Stream Parsing Approach

If PreToolUse hooks prove insufficient (e.g., you need to peek at tool results before committing), consider parsing Claude Code's JSON stream directly.

### How It Works

Claude Code outputs JSONL when run with `claude -p` (plan mode) or via the API. Each line is a JSON event:

```jsonl
{"type": "message_start", "message": {...}}
{"type": "content_block_start", "content_block": {"type": "tool_use", "id": "toolu_01", "name": "Bash"}}
{"type": "content_block_delta", "delta": {"type": "input_json_delta", "input_json": "{\"command\": \"rm -rf /\""}}
...
{"type": "message_delta", "delta": {"stop_reason": "tool_use"}}
{"type": "tool_result", "content": [...], "tool_use_id": "toolu_01"}
```

A bridge could:
1. **Stream → Parse**: Monitor `claude -p` output for `tool_use` events.
2. **Signal Stop**: Send SIGSTOP to Claude process.
3. **Post to Slack**: Show tool and ask for approval.
4. **Signal Resume**: Send SIGCONT (if approved) or SIGTERM (if denied).

### Pros

- **Inspects tool results**: Can approve/deny based on what a tool returned (e.g., "don't commit these files").
- **Post-execution gating**: Block after the tool ran but before Claude sees the result.
- **Language-independent**: Works with any Claude API client.

### Cons

- **Complex**: Requires process signaling, JSONL parsing, state machine.
- **Timing-sensitive**: Race conditions between SIGSTOP and the tool completing.
- **Fragile**: Depends on Claude's output format stability.
- **Headless only**: Only works with `-p` mode or API streaming.
- **Late gating**: User sees the command ran; can't prevent side effects like `rm` or API calls.

### Recommendation on Stream Parsing

**Use PreToolUse hooks instead.** They gate **before** execution, preventing side effects. Stream parsing is useful only if you need post-result approval, which is rarer and more complex.

---

## 7. Concrete Recommendation for v1

### Selected Approach: **PreToolUse Hook + File-System Semaphore**

**Rationale:**

1. **Hook registration** is declarative (one-time in `settings.json`).
2. **Synchronous blocking** is built in; no custom state machine needed.
3. **File-system semaphore** (polling via `ls` + `cat`) is simple and debuggable.
4. **Timeout is safe**: If approval takes >1 hour, deny by default; user can re-request.
5. **Works in headless mode** (`claude -p`) without process signals.
6. **Matcher discrimination** (read-only tools excluded) minimizes Slack noise.

### Implementation Roadmap

#### Phase 1: Hook Registration (1-2 days)

1. **Create** `~/agent-me/.claude/settings.json`:
   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Write|Edit|Bash|mcp__.*",
           "hooks": [
             {
               "type": "command",
               "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/slack-approval.sh",
               "timeout": 3600
             }
           ]
         }
       ]
     }
   }
   ```

2. **Create** `~/agent-me/.claude/hooks/slack-approval.sh`:
   - Reads hook input on stdin.
   - Extracts `tool_use_id`, `tool_name`, `tool_input`.
   - POSTs to Slack bridge (HTTP or file-system queue).
   - Polls for decision file.
   - Returns `hookSpecificOutput` JSON.

3. **Start** the Slack bridge service:
   - Listen for approval requests on `http://localhost:3033/request`.
   - Post message to Slack workspace with Approve/Reject/Auto-approve-all buttons.
   - Write decision to `/tmp/slack-bridge/decisions/$TOOL_ID`.

#### Phase 2: Refinements (2-3 days)

1. **Tool-level discrimination**: Add logic to allow safe `Bash` commands (e.g., `ls`, `git status`) without Slack.
2. **Timeout tuning**: Start with 5-minute window; extend if users request longer approvals.
3. **Logging**: Log all hook decisions to file for audit trail.
4. **Auto-approve-all button**: Set env var or config file that converts all future "ask" → "allow" decisions (for trusted users in trusted sessions).

#### Phase 3: Enhancements (Optional, future)

1. **MCP server filtering**: Different approval rules for different MCP servers (e.g., allow Jira reads, require approval for writes).
2. **User context**: Include user/session info in Slack message for accountability.
3. **Failure recovery**: If Slack bridge crashes, hook denies by default (fail-safe).

### Next Implementation Step

**Parallel work:**
- **Agent A**: Build the hook script (`.claude/hooks/slack-approval.sh`) — inputs stdin, outputs JSON, handles polling.
- **Agent B**: Build the Slack bridge service (`~/agent-me/bridge/slack-bridge.py` or `.js`) — HTTP endpoint, Slack API integration, file-system decision queue.
- **Agent C** (you): Review and finalize hook config in `settings.json`.

**Testing:**
```bash
# Simulate hook with a test request
curl -X POST http://localhost:3033/request \
  -d '{"tool_name": "Bash", "tool_input": {"command": "npm test"}}' \
  -H "Content-Type: application/json"

# Check Slack; click Approve → decision file written
# Hook reads decision file, returns allow to Claude
```

---

## Key Uncertainties & Constraints

1. **Timeout precision**: If Slack button is clicked at second 295 of a 300s window, does the hook see it? (Ans: Yes, polling at 1-2s intervals should catch it.)

2. **Concurrent tool requests**: If Claude spawns 3 tools in parallel, do 3 hook instances run? (Ans: Yes, each with unique `tool_use_id`. The file-system semaphore must handle concurrent reads/writes—use separate decision files per `tool_use_id`, verified above.)

3. **Slack rate limits**: Posting many approval messages quickly—will Slack throttle? (Ans: Yes. Bridge should batch or queue requests. Plan for ~1 message per 2-3 seconds.)

4. **MCP tool discrimination**: The user's configured MCP servers (maas-jira, maas-gitlab, etc.)—do all writes flow through hooks? (Ans: Yes, if matcher includes `mcp__.*`. Verify by running a test MCP write action with hook enabled.)

5. **Auto-approve-all risk**: If enabled, does it really skip **all** future approvals for the session? (Ans: Yes—hook returns "allow" decision without consulting Slack. Caller must document this in Slack message or via env var.)

---

## Summary

**File:** `/Users/thaphan/agent-me/design/approval-hook-design.md`

**Length:** ~500 lines (this document).

**Recommended approach:** PreToolUse hook + file-system semaphore.

**Key constraints found:**
- Hook timeout is 3600s max (1 hour); must set expectation with users.
- Polling adds 1-5s latency per approval.
- Concurrent requests need unique decision files per `tool_use_id`.

**Next steps:** Implement hook script and Slack bridge in parallel; test with simulated tool requests.

