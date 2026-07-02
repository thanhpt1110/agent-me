#!/usr/bin/env bash
# Persistent claude for SSH use: wraps claude in a named tmux session so a lost
# SSH connection / closed laptop NEVER kills a running prompt — reattach later
# from SSH (this script again) or from the dashboard web terminal.
#
#   scripts/ops/claude-persistent.sh                 # session "agme-main", cwd = $HOME
#   scripts/ops/claude-persistent.sh qa              # session "agme-qa"
#   scripts/ops/claude-persistent.sh qa --resume     # extra args go to claude (create only)
#
# Detach and leave it running: Ctrl-b d.  List: tmux ls.  End it: exit claude,
# then exit the shell (or `tmux kill-session -t agme-<name>`).
set -euo pipefail

name="agme-${1:-main}"
[ $# -gt 0 ] && shift

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found — install it or run plain 'claude' (not disconnect-safe)" >&2
  exit 1
fi

# -A attaches when the session exists (extra claude args apply on create only).
exec tmux new-session -A -s "$name" -c "${AGME_CLAUDE_CWD:-$HOME}" claude "$@"
