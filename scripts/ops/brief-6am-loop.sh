#!/usr/bin/env bash
# Daily 06:00 (Asia/Ho_Chi_Minh) dashboard-brief refresher, built to live inside
# a tmux session (see design/persistent-sessions.md):
#
#   env TMUX= tmux new-session -d -s agme-brief bash ~/agent-me.nvidia.com/agent-me/scripts/ops/brief-6am-loop.sh
#
# Sleeps to the next 06:00 VN, runs `agent-me-brief --period day` (which writes
# state/dashboard-cache → the dashboard brief page refreshes), repeats forever.
# While attached: Ctrl-C skips the sleep and runs the brief immediately.
# Stop for good: tmux kill-session -t agme-brief.
# NOTE: the Slack bridge also runs the 06:00 brief — keep both and Slack gets
# two posts; this loop doubles as a drill/backup for the persistent-tmux flow.
set -u
export TZ=Asia/Ho_Chi_Minh
export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/v24.15.0/bin:$PATH"
cd "$(dirname "$0")/.." || exit 1

trap 'echo "[agme-brief] wake requested — running now"' INT

while true; do
  now=$(date +%s)
  target=$(date -d "06:00" +%s)
  [ "$target" -le "$now" ] && target=$(date -d "tomorrow 06:00" +%s)
  echo "[agme-brief] sleeping until $(date -d "@$target") ($(((target - now) / 60)) min) — Ctrl-C to run now"
  sleep $((target - now))
  echo "[agme-brief] running brief at $(date)"
  uv run agent-me-brief --period day || echo "[agme-brief] FAILED at $(date)"
done
