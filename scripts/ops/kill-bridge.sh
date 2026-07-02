#!/usr/bin/env bash
# agent-me — force-kill any running bridge processes on this host.
#
# Used when Ctrl-C in the bridge terminal isn't enough (rare, happens
# when the asyncio shutdown path gets stuck on a socket close). Sends
# SIGTERM first for graceful cleanup, then SIGKILL after 3 seconds.

set -euo pipefail

PATTERN="agent-me-bridge"

if ! pgrep -f "$PATTERN" >/dev/null 2>&1; then
  echo "no bridge process matching '$PATTERN' on this host."
  exit 0
fi

echo "matching processes:"
pgrep -af "$PATTERN" || true
echo ""

echo "sending SIGTERM..."
pkill -TERM -f "$PATTERN" || true
sleep 3

if pgrep -f "$PATTERN" >/dev/null 2>&1; then
  echo "still alive — sending SIGKILL..."
  pkill -KILL -f "$PATTERN" || true
  sleep 1
fi

if pgrep -f "$PATTERN" >/dev/null 2>&1; then
  echo "ERROR: process(es) still alive after SIGKILL:"
  pgrep -af "$PATTERN"
  exit 1
fi

echo "all bridge processes stopped."
