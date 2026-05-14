# 2026-05-10 - Phase 4 dashboard

_Session run while user waited on Phase 3 Colossus MCP-reauth verification._

## Context

User explicitly asked for a Phase 4 draft while waiting on Colossus
MCP reauth verification: build a web/dashboard surface to track all
tasks by category.

Constraints from user (verbatim, simplified):
1. Track all tasks by category (the 7 brief sources).
2. Tools: anything goes — but **smooth + stable**.
3. **Public link without owning a DNS name**; user explicitly asked to
   verify this before implementation.
4. Must NOT disturb the existing Slack flow.

## Tunnel decision

Compared four options for "public URL without owning DNS":

| Option | Stable URL | Bandwidth/req cap | Interstitial | SSE | Setup |
|---|---|---|---|---|---|
| Cloudflare Quick Tunnel | ❌ random | unlimited | none | ❌ | 1 cmd |
| ngrok free static | ✅ | 1GB / 20K req per month | yes, every 7d | ✅ | account |
| **Tailscale Funnel** | **✅** | **no cap** | **none** | ✅ | apt + login |
| Cloudflare Named Tunnel | ✅ | unlimited | none | ✅ | needs OWN domain |

User initially picked ngrok, then clarified that the dashboard needed
to support daily/weekly reports and occasional chat. Re-evaluating:

- Daily/weekly passive view → ngrok free fine.
- Occasional chat via SSE → could push close to 1 GB/month, plus
  ngrok's interstitial page every 7 days is constant friction for a
  bookmark-driven UX.

→ **Re-recommended Tailscale Funnel.** No bandwidth cap on free
Personal, no interstitial, stable URL. Trade-off: install tailscale
daemon + sign up Tailscale account once. User accepted.

## FE stack decision

User asked whether the frontend should be built in Flutter.

Considered Flutter Web, decided **no**:

- Bundle 3-6 MB vs Jinja+Alpine ~50-200 KB.
- Cold-start 3-7s vs server-rendered ~100-300ms first paint.
- Adds Dart toolchain to a Python-only project.
- Mobile UX of Flutter Web is mediocre — Flutter shines on native
  mobile, not web.

Bandwidth wasn't the deciding factor (Tailscale free has no cap).
The deciding factor was **cold-start UX** for a "open dashboard
quickly to see report" workflow, plus toolchain weight.

If we ever want native-feel later: SvelteKit (~30 KB runtime,
component-based, no Dart) — not Flutter.

→ Sticking with **Jinja2 + Alpine.js + Tailwind CDN** for draft.

## Dashboard scope (drafted)

Read-only + on-demand refresh, NO write actions on Jira/etc.:

- 7 source tabs (Jira, GitLab, Confluence, NVBugs, Slack, Outlook,
  GitHub) — each renders cached items, "Refresh now" button.
- Ops panel: bridge stats, MCP health, recent brief runs, Slack
  threads, live log SSE tail.
- Auth: bearer-token (env `DASHBOARD_TOKEN`); browser handshake via
  `?t=...` once → signed-cookie session for 30 days.
- Refresh fans out one `claude -p` per source via reused
  `daily_brief.run_subagent`, NEVER posts to Slack (bridge's 6am
  cron retains sole Slack-posting role).

## Isolation guarantees (Slack flow untouched)

- Two new systemd `--user` units, both **independent** of
  `agent-me-bridge.service`. No `Requires=`, only `After=`.
- Dashboard opens SQLite with URI `mode=ro` — physically can't
  write. WAL mode means concurrent reads don't block bridge writes.
- Dashboard never spawns a `claude -p` in the bridge's chat-cwd; it
  only spawns brief subagents in the repo cwd (same as the
  existing 6am cron).
- Dashboard binds to `127.0.0.1:8765` only; Tailscale Funnel
  proxies. Bridge's Socket Mode WebSocket is unrelated.

## What was built

```
design/dashboard-design.md         (architecture + tunnel + auth)
src/agent_me/dashboard/
├── __init__.py
├── app.py                  (Starlette ASGI + 14 routes + SSE)
├── auth.py                 (bearer-token middleware + signed cookies)
├── state_reader.py         (SQLite RO + brief cache + log tail)
├── brief_runner.py         (single-flight on-demand brief job)
├── templates/              (base, index, source, ops, login + nav partial)
└── static/app.css|.js
deploy/agent-me-dashboard.service
deploy/agent-me-funnel.service     (Tailscale Funnel)
scripts/install-dashboard.sh       (idempotent installer)
pyproject.toml                     (+5 deps, +1 entry point agent-me-dashboard)
```

## Smoke tests passed

- `python -m py_compile` on all 5 dashboard modules: OK.
- Import test: all 14 routes registered, all 7 sources mounted,
  state-dir resolved, DB-missing handled gracefully (returns empty
  stats with warning logs).
- Live boot test (uvicorn, no DASHBOARD_TOKEN): `/healthz` 200,
  `/` 200 (auth disabled), `/api/state` returns valid JSON with all
  7 source snapshots.
- Auth boot test (DASHBOARD_TOKEN set): no token → 401, wrong
  bearer → 401, correct bearer → 200, `?t=correct` → 303 redirect
  with cookie, `/healthz` always 200.
- Linter: zero errors reported by Cursor's lint.

## What's NOT done in this session

- **Not deployed to Colossus.** Waiting on Phase 3 to stabilize
  (user is still verifying MCP reauth). Install path:
  `scripts/install-dashboard.sh` on Colossus when ready.
- **No daily_brief.py edits.** Reused via in-process import to avoid
  touching Slack-posting flow. Optional `--source <id>` and
  `--no-post` flags in `daily_brief.py` could come later if we want
  out-of-process isolation for the dashboard.
- **No tailscale install on this dev host.** Local smoke ran direct
  on `127.0.0.1:8765`. Tailscale install is in the Colossus playbook.
- **Phase 2b approval gate buttons.** Just shows a count.
- **MCP re-auth from browser.** Still CLI-only via `agent-me-reauth`.
- **Markdown export, search, dark-mode toggle, mobile drawer.**

## Pre-deploy checklist (for future Claude session on Colossus)

1. `claude mcp list` → all 17 ✓ Connected.
2. `curl https://login.tailscale.com` from Colossus → 200/302.
3. `apt install tailscale` (or upstream installer).
4. `sudo tailscale up` (browser auth via SSH port-forward).
5. `sudo tailscale funnel --bg 8765`.
6. `./scripts/install-dashboard.sh` from repo root.
7. Visit printed URL with `?t=<token>` once to set session cookie.

## Open questions

- Should the dashboard auto-redeploy via the existing
  `agent-me-watch.service`? Probably yes — pattern is identical;
  watcher already runs `systemctl --user restart agent-me-bridge`,
  could extend to `agent-me-dashboard` too. Captured in ideas.md.
- Should we add a chat tab? Out of Phase 4 draft scope; if added,
  it gets its own `chat_sessions` table separate from the bridge's
  `claude_sessions`. Captured in ideas.md.
- Cursor Background Agents for keeping work running across user's
  laptop reboots? Useful pattern for prompt-tuning runs and
  unattended deploys. Captured in ideas.md.
