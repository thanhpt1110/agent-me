# Pull request

## Summary

<!-- 1-3 sentences: what does this change, and why? -->

## Type of change

<!-- Tick all that apply -->

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor
- [ ] Docs only
- [ ] Tests only
- [ ] Chore (build, deps, tooling)
- [ ] Security fix

## Related issue / discussion / design doc

<!--
Link the GitHub issue, Discussion, or `design/<topic>.md` ADR this PR
implements or follows up on. For non-trivial changes, prefer a linked ADR.
-->

Fixes #
Related:

## Before / after

<!--
Show the change in behavior. Pick whichever is useful:
- Code snippet for an API change
- Screenshot for a dashboard change
- A `journalctl --user -u agent-me-bridge -n 50 --no-pager` excerpt for a bridge change
- A `uv run agent-me-brief --period day --dry-run` excerpt for a brief change
-->

## Test plan

<!--
What did you actually run locally? CI (`.github/workflows/ci.yml`) runs lint
+ tests on Python 3.12 / 3.13 + pyright (advisory) + CodeQL (security
scanning) on every PR — paste local repro commands and any output that
isn't already covered by the green CI badge.
-->

```
# Paste commands + relevant output here
```

## Checklist

- [ ] `uv run pytest tests/` passes
- [ ] `uv run ruff check src/ tests/` is clean (or only pre-existing warnings)
- [ ] `STATE.md` updated if the change affects the phase / roadmap
- [ ] `design/` ADR added or updated for non-trivial changes
- [ ] `discussions/<date>-<topic>.md` log appended for design-heavy sessions
- [ ] No secrets / tokens / PII in the diff (`grep -i 'token\|secret\|password' diff` is a good final scan)
- [ ] Commit messages follow the "what + why" convention (see CONTRIBUTING §Commit style)

## Notes for the reviewer

<!-- Anything to look at first? Known unknowns? Tricky bits? -->
