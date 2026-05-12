# Brev fill/screenshot test flow

_2026-05-12_

## Context

The first `/brev <org_id>` smoke from Slack failed before the Brev form loaded:
Codex canceled `maas-playwright` tool calls at the MCP approval layer. After
passing explicit Codex config overrides for the Playwright browser tools, the
next blocker was runtime Chrome availability. Installing the Playwright Chrome
runtime allowed navigation to start, but the live page currently redirects to
Microsoft/NVIDIA SSO, so an authenticated browser session is still required
before a real form fill can complete.

## Decision

Keep `/brev <org_id>` in a test stage for now:

- Use a dedicated Codex session with `maas-playwright`.
- Pass Playwright tool approval configs into `codex exec` for the Brev flow.
- Fill only reference/sample values from the local `brev/` screenshots plus the
  supplied org id.
- Capture and return a screenshot of the filled state.
- Do not click the final submit button.
- Do not send the post-submit Slack notification.

The submit path remains in code behind the existing `BREV_SUBMITTED` marker for
a later prompt revision, but the active Brev prompt now emits `BREV_FILLED`
only after it has filled the form and captured a screenshot.
