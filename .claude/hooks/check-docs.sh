#!/usr/bin/env bash
# Stop hook — advisory nudge to keep docs in sync (CLAUDE.md → "Documenting a
# change is part of done"). NON-blocking: it surfaces a checklist at end-of-turn
# and never forces the agent to keep going.
#
# Fires ONLY when THIS session changed code files and didn't touch STATE.md.
# Pre-existing uncommitted work that this session didn't modify never triggers
# it — the current dirty state is diffed against a session-start baseline
# captured by capture-docs-baseline.sh. So MCP-only / read-only turns stay quiet.
set -uo pipefail
input="$(cat 2>/dev/null || true)"

active="false"
sid="default"
if command -v jq >/dev/null 2>&1; then
  active="$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null || echo false)"
  sid="$(printf '%s' "$input" | jq -r '.session_id // "default"' 2>/dev/null || echo default)"
fi
# Only nudge on the first stop attempt (avoid repeat firing).
[ "$active" = "true" ] && exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
command -v git >/dev/null 2>&1 || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
. "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/_docs-snapshot.sh" 2>/dev/null || exit 0

baseline="$(docs_baseline_file "$sid")"
current="$(docs_snapshot)"

# No baseline (session predates the hook / SessionStart didn't run): seed it now
# and stay silent — we can't attribute changes to this session yet.
if [ ! -f "$baseline" ]; then
  mkdir -p "$(dirname "$baseline")"
  printf '%s\n' "$current" > "$baseline"
  exit 0
fi

# Paths that differ between baseline and now = changed THIS session (additions,
# content changes, deletions). comm -3 = lines unique to either side; strip the
# comm column tab, keep the path field, dedup.
changed="$(LC_ALL=C comm -3 <(LC_ALL=C sort "$baseline") <(printf '%s\n' "$current" | LC_ALL=C sort) \
           | sed 's/^\t//' | cut -f2- | LC_ALL=C sort -u)"
[ -z "$changed" ] && exit 0

# Did any *code* change this session? (ignore pure doc/markdown edits)
code="$(printf '%s\n' "$changed" | grep -E '^(src/|deploy/|tests/|configs/)|\.(py|html|css|js)$' || true)"
[ -z "$code" ] && exit 0

# STATE.md updated this session? then we're good.
printf '%s\n' "$changed" | grep -q '^STATE\.md$' && exit 0

reason="Docs discipline (CLAUDE.md \"Documenting a change is part of done\"): this session changed code but STATE.md was not updated. Before wrapping up, add a dated addendum to STATE.md (what changed / what's next / how verified); append a discussions/YYYY-MM-DD-<topic>.md log if there were decisions; and update design/ or configs/.env.example if relevant."

if command -v jq >/dev/null 2>&1; then
  jq -n --arg r "$reason" '{
    hookSpecificOutput: { hookEventName: "Stop", additionalContext: $r },
    systemMessage: "Reminder: this session changed code but STATE.md not updated — see CLAUDE.md docs discipline."
  }'
else
  printf '{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"%s"},"systemMessage":"Reminder: update STATE.md (docs discipline)."}\n' "$reason"
fi
exit 0
