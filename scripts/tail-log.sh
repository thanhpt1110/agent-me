#!/usr/bin/env bash
# agent-me bridge log tailer — pretty-prints the JSON file log.
#
# Usage:
#   ~/agent-me/scripts/tail-log.sh           # follow live
#   ~/agent-me/scripts/tail-log.sh -n 200    # last 200 lines, no follow
#   ~/agent-me/scripts/tail-log.sh -e error  # only error+ lines
#
# Requires `jq`. Install with: brew install jq

set -euo pipefail

LOG="${AGENT_ME_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-me}/bridge.log"

if [[ ! -f "$LOG" ]]; then
  echo "no log file at $LOG — is the bridge running?" >&2
  echo "start it with:  uv run agent-me-bridge" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not installed; falling back to raw tail. (install: brew install jq)" >&2
  exec tail -f "$LOG"
fi

FOLLOW="-f"
LINES=""
LEVEL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) LINES="-n $2"; FOLLOW=""; shift 2 ;;
    -e) LEVEL="$2"; shift 2 ;;
    -h|--help) sed -n 's/^# \?//p' "$0" | head -10; exit 0 ;;
    *) shift ;;
  esac
done

# Build a jq filter that pretty-prints and optional filters by level
LEVEL_FILTER=""
if [[ -n "$LEVEL" ]]; then
  case "$LEVEL" in
    error)   LEVEL_FILTER='select(.level == "error" or .level == "critical")' ;;
    warn|warning) LEVEL_FILTER='select(.level == "warning" or .level == "error" or .level == "critical")' ;;
    info)    LEVEL_FILTER='select(.level == "info" or .level == "warning" or .level == "error" or .level == "critical")' ;;
    debug|*) LEVEL_FILTER='.' ;;
  esac
fi

JQ_PRETTY='. as $r | (del(.timestamp,.level,.event,.logger,.thread_name) | to_entries | map("  \(.key)=\(.value | tostring)") | join("")) as $extras | "\($r.timestamp[11:19]) \($r.level | ascii_upcase | .[0:5] | (. + " "*(5 - length))) \($r.event)\($extras)"'

if [[ -n "$FOLLOW" ]]; then
  tail -f "$LOG" | jq -r --unbuffered "${LEVEL_FILTER:+$LEVEL_FILTER |} $JQ_PRETTY"
else
  tail $LINES "$LOG" | jq -r "${LEVEL_FILTER:+$LEVEL_FILTER |} $JQ_PRETTY"
fi
