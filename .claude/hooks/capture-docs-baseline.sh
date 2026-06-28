#!/usr/bin/env bash
# SessionStart hook — snapshot the repo's already-dirty state so the Stop
# docs-nudge can tell THIS session's changes from pre-existing uncommitted work.
# Writes a per-session baseline only when absent, so resume/compact never reset
# it (which would hide edits made before the reset).
set -uo pipefail
input="$(cat 2>/dev/null || true)"
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
command -v git >/dev/null 2>&1 || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
. "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/_docs-snapshot.sh" 2>/dev/null || exit 0

sid="default"
if command -v jq >/dev/null 2>&1; then
  sid="$(printf '%s' "$input" | jq -r '.session_id // "default"' 2>/dev/null || echo default)"
fi

baseline="$(docs_baseline_file "$sid")"
mkdir -p "$(dirname "$baseline")"
find "$(dirname "$baseline")" -type f -mtime +7 -delete 2>/dev/null || true

[ -f "$baseline" ] && exit 0
docs_snapshot > "$baseline" 2>/dev/null || true
exit 0
