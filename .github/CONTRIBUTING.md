# Contributing to agent-me

Thanks for opening the contributing guide. This file covers the two
ways to use this repo, the development workflow, and what kinds of
changes are welcome upstream.

## Two ways to use this repo

`agent-me` is a public, fork-friendly framework. Most readers will
take **path 1**; **path 2** is for people who want to ship a fix or
feature back to this upstream.

**Path 1 — fork and personalize** _(most common)_

You want your own 24/7 personal AI agent. Use this repo as a
template:

1. Click "Use this template" on GitHub → create your own repo.
2. Follow the [Quickstart in `README.md`](README.md).
3. Set your secrets in `~/agent-me-secrets.md` (outside the repo).
4. Deploy on your own host per `design/deploy-on-host.md`.

You don't need to read the rest of this file.

**Path 2 — contribute upstream** _(rarer, valuable)_

You found a framework-level bug, want to add an MCP integration,
write a design doc, or improve scripts/docs. Read on.

## Before you open a pull request

- **Discuss large changes in an issue first.** Anything more than a
  small fix benefits from a 2–3 message thread before code lands.
- **Read [`STATE.md`](STATE.md)** to see current phase + what's in
  flight. Avoid stepping on work-in-progress unless you've
  coordinated.
- **Skim [`discussions/ideas.md`](discussions/ideas.md)** for ideas
  already captured but not yet built — your change might already
  have a reservation.
- **Use `design/<topic>.md`** for ADR-style design docs. Anything
  that touches the bridge spawn path, the approval gate, the
  dashboard shape, or the brief fan-out should land an ADR.

## Local development setup

Prerequisites: `claude` CLI, [`uv`](https://docs.astral.sh/uv/),
`gh`, `jq`, Python 3.12+, Node (for `claude` itself + Playwright
MCP).

```bash
git clone git@github.com:<you>/agent-me.git
cd agent-me
./scripts/bootstrap.sh
```

`bootstrap.sh` is idempotent: it runs `uv sync`, copies
`configs/.env.example` to `configs/.env`, and registers all 17 MaaS
MCP servers at user scope. Re-run any time. The long version with
prerequisite walkthroughs is in `design/setup-on-fresh-host.md`.

Common entry points:

```bash
uv run agent-me-bridge      # Slack bridge (Socket Mode)
uv run agent-me-dashboard   # Phase 4 web dashboard
uv run agent-me-brief --period day [--dry-run]
uv run agent-me-reauth      # opens NVIDIA-SSO auth tabs
```

## Code style and checks

Lint with `ruff`, type-check with `pyright` (standard mode), test
with `pytest`. All three are configured in `pyproject.toml` under
`[project.optional-dependencies].dev` — `uv sync --extra dev` pulls
them in.

```bash
uv run ruff check src/agent_me/dashboard/ src/agent_me/slack_bridge/approvals.py tests/
uv run ruff format --check src/ tests/
uv run pyright src/ tests/        # advisory; many pre-existing warnings
uv run pytest tests/               # 68 tests, ~3s
```

CI (`.github/workflows/ci.yml`) runs all four on every PR. The
strict scope (`lint` + `test` jobs) gates merge; `lint-advisory` and
`type` surface findings as yellow checks but don't block.

If you touch `src/agent_me/slack_bridge/app.py` or
`src/agent_me/scripts/`: those have pre-existing lint warnings that
the CI advisory job already knows about. New code should be clean;
don't loosen `ruff` rules.

## Commit style

Read `git log --oneline -20` for tone. Three things matter:

1. **Imperative subject**, ≤72 chars (`Add X`, not `Added X` or
   `Adds X`). No conventional-commits prefix needed (`feat:` etc.).
2. **Body explains why, not what.** The diff shows what changed;
   the commit message should justify it. Bullets are fine for
   multi-part changes.
3. **One commit per logical change.** If your PR has 5 commits and
   they each tell a story, keep them. If they're noise (`fix typo`,
   `address review`), squash before review.

The repo deliberately keeps decision history in commits +
`discussions/` rather than a separate changelog. Detailed messages
matter.

## Pull request flow

1. Fork → branch → push → open PR.
2. Fill out the PR template (`.github/PULL_REQUEST_TEMPLATE.md`).
   It asks for summary, type of change, related design doc, test
   plan, and a checklist.
3. CI runs lint + tests on Python 3.12 and 3.13. Wait for green
   before requesting review.
4. One round of review usual. The maintainer may ask you to update
   `STATE.md` or add an ADR if the change is design-heavy.
5. Squash-or-rebase merge depending on commit quality (see "Commit
   style" above).

## What kinds of contributions are welcome

- Bug fixes in framework code.
- New MCP integrations under `daily_brief.py`'s source list.
- New `design/<topic>.md` ADRs proposing changes.
- Improvements to `scripts/` (`bootstrap.sh`, `install-systemd.sh`,
  `agent-me-watch.sh`, etc.).
- Documentation improvements — `README.md`, `design/`, `CLAUDE.md`,
  this file.
- Tests for currently-untested code (the bridge module is largely
  uncovered today; the dashboard, approvals module, and helpers
  have ~68 tests).
- New dashboard pages, log viewports, or operational panels.

## What's deliberately not accepted upstream

- Operator-specific configs. Those go in your fork's
  `configs/.env`, never in the public repo.
- Personal prompts or personalities baked into prompt templates.
  Use forks or per-user config.
- PRs that pull NVIDIA-internal content (Jira tickets, bug numbers,
  internal URLs, NDA'd material) into the public repo. The
  framework targets NVIDIA-internal hosts but the code stays
  public; user-specific data must not.

## Reporting bugs and asking questions

- **Bugs and feature requests:** open a [GitHub
  issue](https://github.com/thanhpt1110/agent-me/issues). Use the
  templates — they ask for environment + repro steps.
- **General questions / "how do I set this up":** prefer GitHub
  Discussions (when enabled) or open an issue with the `question`
  label.
- **Security issues:** do not open public issues — see
  [`SECURITY.md`](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant
2.1](CODE_OF_CONDUCT.md). Enforcement contact:
**thanhphantuan1110@gmail.com**.

---

_Thanks for taking the time. This started as one person's personal
AI OS; if you're shipping your own fork or contributing upstream,
the project is better for it._
