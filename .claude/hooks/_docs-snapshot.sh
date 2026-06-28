#!/usr/bin/env bash
# Shared by capture-docs-baseline.sh (SessionStart) and check-docs.sh (Stop).
# Both MUST use the SAME extraction so pre-existing uncommitted state cancels
# out in the diff and only THIS session's changes remain.

# Print "<blobhash>\t<path>" for every dirty file (staged + unstaged +
# untracked), sorted. Hash is the working-tree content; absent files get a
# sentinel so deletions still register as a change.
docs_snapshot() {
  git status --porcelain 2>/dev/null | cut -c4- | while IFS= read -r path; do
    [ -n "$path" ] || continue
    if [ -f "$path" ]; then
      printf '%s\t%s\n' "$(git hash-object -- "$path" 2>/dev/null || echo MISSING)" "$path"
    else
      printf 'ABSENT\t%s\n' "$path"
    fi
  done | LC_ALL=C sort
}

docs_baseline_file() {
  printf '%s/claude-docs-baselines/%s' "$(git rev-parse --absolute-git-dir)" "${1:-default}"
}
