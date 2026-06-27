# Security policy

How to report a vulnerability in the **agent-me** framework — the bridge,
hooks, dashboard, scripts, and configs in this repo. Personal forks and
upstream dependencies are out of [scope](#scope).

## Reporting a vulnerability

**Please do not open a public issue for security bugs.**

Preferred channel — [GitHub Security Advisories](https://github.com/thanhpt1110/agent-me/security/advisories/new).
Click "Report a vulnerability" and fill in the form. Reports stay private
until a fix is ready.

Email fallback: **thanhphantuan1110@gmail.com** (subject "agent-me security").
PGP not set up. Personal side project — first-response is best-effort, a few
business days (see [timeline](#disclosure-timeline)).

## Scope

**In scope:**

- Bridge or dashboard code paths that let an unintended user execute write
  tools (the bridge is operator-only; one Slack user gates writes via
  `SLACK_ALLOWED_USER_ID`).
- Phase 2b approval-gate bypasses (see below).
- Auth / session weaknesses around `DASHBOARD_TOKEN` or the signed-cookie
  session.
- Hook script issues — semaphore races, decision-file forgery, TOCTOU bugs in
  the `PreToolUse` flow.
- Default configs that leak tokens or other secrets.

**Out of scope** (report upstream, not here):

- Forks and personalizations — those are your own deployments; fix them in
  your fork.
- Bugs in **Claude Code**, the **Slack SDK**, individual **MCP servers**, or
  NVIDIA-internal infrastructure.
- Issues requiring physical access to the operator's host.
- "What if the operator approves a malicious tool call?" — the operator is the
  trust root by design.

## Supported versions

There is no formal release cadence yet; the project tracks `main`.

| Version       | Supported                              |
|---------------|----------------------------------------|
| `main` (HEAD) | ✓ — fixes land here first              |
| older commits | ✗ — please rebase or fork              |

No LTS branch, no backports. Pin a commit and you own the rebase.

## Secrets and credentials

The repo assumes **no real secret ever lives in git**:

- `configs/.env` is gitignored; the committed template is `configs/.env.example`.
- Real secrets live in `~/agent-me-secrets.md` outside the repo, `chmod 600`.
- MCP tokens are per-user OAuth, refreshed via `agent-me-reauth`; never
  committed.
- Dashboard auth uses `DASHBOARD_TOKEN` (single shared secret) plus a signed
  cookie session.

If you accidentally commit a secret: **rotate first** (a committed-then-deleted
secret is leaked — rotation is the only real fix; your local
`~/agent-me-secrets.md` should already point at each provider's rotation URL),
then scrub history with `git filter-repo` and force-push. `git revert` is
**not** sufficient — the secret stays in history.

## Phase 2b approval gate (write-tool gating)

When `APPROVAL_GATE=1`, the `PreToolUse` hook intercepts every write tool
call, writes a decision request to a file-system semaphore, and waits for the
operator to approve or deny via Slack buttons. Off by default. The threat
model is narrow — it protects the operator from a sub-agent silently executing
destructive write tools.

Bypasses are **in scope**: any code path that issues a write tool without going
through `PreToolUse`, a way to forge an "approve" decision file, a semaphore
race that lets a tool execute before the decision lands, or a way for a
non-operator Slack user to issue an approve.

## Disclosure timeline

Best-effort, one-maintainer project:

- **Acknowledge** within ~5 business days.
- **Triage + fix plan** within 14 days for critical issues.
- **Public disclosure** after a fix lands on `main`. Reporters who want credit
  will be named in the advisory and the fix commit — tell us how, or that
  you'd rather stay anonymous. If a fix needs materially longer than 14 days,
  we'll say so on the private advisory thread.
