# 2026-06-26 — Native Chat feature on the dashboard (branch `big-update`)

## Context
Goal: prompt the agent from a browser, with each chat = a persistent Claude Code
session running on this VM's workspace. A standalone Node prototype existed
(`chat/`, Express+ws, :8787). This session ported it to a **native dashboard
feature** so it shares the dashboard's nav, NVIDIA styling, auth, and SSE stack —
on the `big-update` branch only (production agent-me stays on the A100).

Informed by two research passes: a codebase deep-dive (dashboard auth/SSE/styling
integration points) and a web deep-research pass (NVIDIA design system, agent-chat
UX best practices, Starlette SSE + SOLID).

## Decisions
- **CLI subprocess, not the Agent SDK.** The `@anthropic-ai/claude-agent-sdk`
  requires `ANTHROPIC_API_KEY`; this host only has the CLI's OAuth login. The
  `claude` CLI reuses that login. (Don't "upgrade" to the SDK without a key.)
- **SSE, not WebSocket.** The dashboard already runs `sse-starlette` + the
  `AutoSFAJob` fan-out + an Alpine `EventSource` idiom. Porting to SSE reuses a
  hardened path; one stream per session: `GET /api/sse/chat/{id}`.
- **Server-owned turns.** A turn runs in an `asyncio.Task` owned by the service;
  closing the browser does not kill the `claude` subprocess. Transcript persists
  to disk, so a reconnecting client catches up.
- **Monotonic `seq` per event.** Lets the client dedup the transcript replay vs
  the live job buffer and survive SSE auto-reconnects (WHATWG Last-Event-ID
  spirit). Implemented in `ChatService._emit` + `ChatTurnJob.next_seq`.
- **Auth reuse, no new SSO.** `/chat` + `/api/chat/*` are non-exempt, so
  `AuthMiddleware` gates them automatically. DEV runs with
  `DASHBOARD_TRUST_NETWORK=1` (direct access). SSO is a separate track.
- **DEV marking is environment-wide.** `DEPLOY_ENV=dev` → Jinja global → a banner
  in `base.html` on every page. Deployed via `deploy/agent-me-dashboard-dev.service`
  on :8787.
- **Retired the Node prototype** (`chat/`) — superseded by the native port; keeping
  both would confuse. History retained in git.

## SOLID layering (see `design/dashboard-chat.md`)
`models` (value types) · `config` (`ChatConfig.from_env`, the only env reader) ·
`store` (`SessionStore` Protocol + `DiskSessionStore`) · `runner` (`ClaudeRunner`
Protocol + `ClaudeCliRunner` subprocess adapter) · `jobs` (`ChatTurnJob` live
fan-out) · `service` (`ChatService` orchestrator; the only thing handlers touch).
Route handlers stay thin in `app.py`.

## UX (from the research pass)
Full-width messages (not bubbles); progressive-disclosure tool rows (one-liner →
args/result, collapsed); thinking collapsed; streaming caret; autoscroll only when
near-bottom + "Jump to latest"; markdown + copy-able code blocks; Stop control;
empty-state example chips; ARIA `role="log" aria-live="polite"` + a separate
sr-only announcer. NVIDIA: green is accent-only, **black text on green** buttons
(AA contrast), JetBrains Mono for tool/code, font stack `NVIDIA Sans → Inter →
system`.

## Verified
`uv run ruff check` clean; `uv run pytest -q` → 216 passed (incl. `tests/test_chat.py`
with a `FakeClaudeRunner`); a real end-to-end turn through `/chat` on :8787
(user → init → text → result → turn_end, resume id captured).

## Next
Deeper SFA/tool wiring into chat; multi-tab live-follow of an in-flight turn;
optional Markdown library; consider a compiled Tailwind build.
