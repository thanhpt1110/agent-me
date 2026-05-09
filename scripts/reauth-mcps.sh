#!/usr/bin/env bash
#
# agent-me — MCP re-auth helper
#
# Purpose: detect every MCP server in `claude mcp list` flagged
# `! Needs authentication`, generate a single Claude prompt that triggers
# each server's OAuth refresh, copy the prompt to your clipboard, and
# launch the interactive `claude` REPL ready for you to paste it.
#
# Why interactive (not headless): the OAuth callback handler runs only
# while a `claude` REPL is alive. `claude -p` exits immediately so the
# callback can't complete, even if a URL prints.
#
# Usage:
#   ~/agent-me/scripts/reauth-mcps.sh
#
# After paste-and-Enter, Claude prints one auth URL per stale server.
# Cmd-click each URL → finish NVIDIA SSO → return to terminal. Type
# `/exit` to leave the REPL when all servers show ✓.

set -euo pipefail

# ---------------------------------------------------------------------------
# 1. Detect stale servers
# ---------------------------------------------------------------------------

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: \`claude\` not on PATH" >&2
  exit 2
fi

LIST_OUT=$(claude mcp list 2>&1 || true)

# Lines like:  maas-jira: https://... (HTTP) - ! Needs authentication
STALE=$(printf '%s\n' "$LIST_OUT" \
  | grep "Needs authentication" \
  | awk -F: '{print $1}' \
  | sed 's/[[:space:]]//g' \
  | grep -v '^$' \
  | sort -u)

if [ -z "$STALE" ]; then
  echo "All MCP servers authenticated. Nothing to do."
  exit 0
fi

count=$(printf '%s\n' "$STALE" | wc -l | tr -d ' ')
SERVER_CSV=$(printf '%s\n' "$STALE" | tr '\n' ',' | sed 's/,$//' | sed 's/,/, /g')

# ---------------------------------------------------------------------------
# 2. Build the Claude prompt
# ---------------------------------------------------------------------------

PROMPT=$(cat <<EOF
Please refresh OAuth authentication for these MCP servers by calling a
read-only tool (search, list, get, or health_check) from each: $SERVER_CSV.

For every server, when its auth URL appears, print the URL on its own line
prefixed with the server name. Do nothing else after that.
EOF
)

# ---------------------------------------------------------------------------
# 3. Show the plan + copy prompt to clipboard (macOS)
# ---------------------------------------------------------------------------

cat <<EOF

================================================================
  agent-me — MCP re-auth helper
================================================================

Detected $count stale server(s):
$(printf '%s\n' "$STALE" | sed 's/^/  - /')

Claude prompt (auto-copied to clipboard if pbcopy is available):
----------------------------------------------------------------
$PROMPT
----------------------------------------------------------------

EOF

if command -v pbcopy >/dev/null 2>&1; then
  printf '%s' "$PROMPT" | pbcopy
  echo "(Prompt copied. In the next REPL, press Cmd-V then Enter.)"
else
  echo "(pbcopy not found — copy the block above manually.)"
fi

cat <<EOF

Steps:
  1. claude REPL launches in 3 seconds.
  2. Cmd-V (paste prompt) + Enter.
  3. Claude prints one auth URL per stale server. Cmd-click each →
     complete NVIDIA SSO in browser.
  4. Type /exit when all servers come back ✓.

EOF

sleep 3

# ---------------------------------------------------------------------------
# 4. Launch claude REPL (this script's process is replaced by claude)
# ---------------------------------------------------------------------------

exec claude
