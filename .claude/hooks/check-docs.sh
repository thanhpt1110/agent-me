#!/usr/bin/env bash
# Stop hook — advisory nudge to keep docs in sync (CLAUDE.md → "Documenting a
# change is part of done"). NON-blocking: it surfaces a checklist at end-of-turn
# when code changed but STATE.md didn't; it never forces the agent to keep going.
set -uo pipefail
input="$(cat 2>/dev/null || true)"

# Only nudge on the first stop attempt (avoid repeat firing).
active="false"
if command -v jq >/dev/null 2>&1; then
  active="$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null || echo false)"
fi
[ "$active" = "true" ] && exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
command -v git >/dev/null 2>&1 || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Changed paths: staged + unstaged + untracked.
changed="$(git status --porcelain 2>/dev/null | cut -c4-)"
[ -z "$changed" ] && exit 0

# Did any *code* change? (ignore pure doc/markdown edits)
code="$(printf '%s\n' "$changed" | grep -E '^(src/|deploy/|tests/|configs/)|\.(py|html|css|js)$' || true)"
[ -z "$code" ] && exit 0

# STATE.md already touched this session? then we're good.
printf '%s\n' "$changed" | grep -q '^STATE\.md$' && exit 0

reason="Docs discipline (CLAUDE.md \"Documenting a change is part of done\"): you changed code but STATE.md was not updated. Before wrapping up, add a dated addendum to STATE.md (what changed / what's next / how verified); append a discussions/YYYY-MM-DD-<topic>.md log if there were decisions; and update design/ or configs/.env.example if relevant."

if command -v jq >/dev/null 2>&1; then
  jq -n --arg r "$reason" '{
    hookSpecificOutput: { hookEventName: "Stop", additionalContext: $r },
    systemMessage: "Reminder: code changed but STATE.md not updated — see CLAUDE.md docs discipline."
  }'
else
  printf '{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"%s"},"systemMessage":"Reminder: update STATE.md (docs discipline)."}\n' "$reason"
fi
exit 0
