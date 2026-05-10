# Phase 4 — Web dashboard design

**Status:** Draft (2026-05-10). Code lives in `src/agent_me/dashboard/`.
Not deployed yet; implementation is local-only until Phase 3 Colossus
deploy stabilises.

## Goal

A single web page that lets the operator see, at a glance:

1. **Per-source task lists** — the 7 brief sources rendered as tabs:
   Jira, GitLab, Confluence, NVBugs, Slack, Outlook, GitHub. Each tab
   shows the latest cached brief items for that source, grouped the
   same way the Slack brief groups them.
2. **Operations panel** — bridge process status, MCP health (which
   servers need re-auth), recent brief runs (when, how long, errors),
   pending approvals (Phase 2b stub), recent Slack threads.
3. **On-demand refresh** — a button per source (or "refresh all") that
   fans out a fresh brief subagent for that source only and streams
   the result back over SSE.
4. **Live log tail** — bridge.log + brief.log streamed via SSE so the
   operator can debug in real time without `ssh` + `journalctl -f`.

## Non-goals (out of scope for the draft)

- **Write actions on Jira/GitLab/etc.** Phase 2b's approval gate is
  the place for write surface. The dashboard stays read-only over
  the agent-me state DB and read-only over the MCPs (it can re-run
  the brief, which already enforces tool restrictions).
- **Replacing Slack as the chat interface.** Slack stays the primary
  chat UX. Dashboard chat (if added later) is a secondary surface.
- **Multi-user auth / RBAC.** Single-operator project, single bearer
  token suffices.
- **Mobile-first responsive design.** Desktop-first; mobile works but
  isn't optimised.

## Architecture

```
                ┌────────────────────────────────────┐
                │  Browser (operator's Mac)          │
                │  https://<host>.<tailnet>.ts.net   │
                │   ↑ Tailscale Funnel public URL    │
                └─────────────────┬──────────────────┘
                                  │ HTTPS (Tailscale relay)
                                  ▼
       ┌────────────────────────────────────────────┐
       │  Colossus host (24/7)                      │
       │                                            │
       │  ┌──────────────────┐                      │
       │  │ tailscaled       │  (system daemon)     │
       │  │ funnel→ :8765    │                      │
       │  └────────┬─────────┘                      │
       │           ▼ 127.0.0.1:8765                 │
       │  ┌──────────────────────────────────┐      │
       │  │ agent-me-dashboard.service       │      │
       │  │  uvicorn + Starlette ASGI app    │      │
       │  │   - Bearer auth middleware       │      │
       │  │   - GET  /          (overview)   │      │
       │  │   - GET  /source/<id>            │      │
       │  │   - GET  /ops                    │      │
       │  │   - POST /api/refresh/<id>       │      │
       │  │   - GET  /api/sse/logs           │      │
       │  │   - GET  /api/sse/refresh/<job>  │      │
       │  └──────┬────────────┬──────────────┘      │
       │         │ READ-ONLY  │ spawn               │
       │         ▼            ▼                     │
       │  ┌─────────────┐  ┌─────────────────────┐  │
       │  │ state.db    │  │ agent-me-brief      │  │
       │  │ (SQLite WAL │  │ (per-source one-off │  │
       │  │  shared w/  │  │  subprocess)        │  │
       │  │  bridge)    │  └─────────────────────┘  │
       │  └─────────────┘                           │
       │  ┌─────────────────────────────────┐       │
       │  │ bridge.log / brief.log (tail)   │       │
       │  └─────────────────────────────────┘       │
       │                                            │
       │  ┌──────────────────┐                      │
       │  │ agent-me-bridge  │  (UNTOUCHED)         │
       │  │ Socket Mode → Slack                     │
       │  └──────────────────┘                      │
       └────────────────────────────────────────────┘
```

**Two new systemd units, both `--user` like the bridge:**

- `agent-me-dashboard.service` — runs `uvicorn agent_me.dashboard.app:app
  --host 127.0.0.1 --port 8765`. Bound to localhost only; Tailscale is
  the only thing that can talk to it.
- `agent-me-funnel.service` — runs `tailscale funnel 8765` (foreground).
  Optional — if you `tailscale funnel --bg 8765` once, the config
  persists and you don't need a unit. Keeping a unit makes the public
  URL part inspectable (`systemctl --user status agent-me-funnel`).

**The bridge service is not modified.** Bridge keeps its own SQLite
connection (WAL mode), keeps its own `claude -p` invocations, keeps
its Socket Mode WebSocket. The dashboard is a strict additive.

## Why Tailscale Funnel (locked decision)

We considered four options for "public URL without owning a DNS name":

| Option | Stable URL | Bandwidth | Interstitial | Setup | Verdict |
|---|---|---|---|---|---|
| Cloudflare Quick Tunnel (`*.trycloudflare.com`) | ❌ rotates per restart | unlimited | none | 1 cmd, no account | dev-only, no SSE |
| ngrok free static (`*.ngrok-free.app`) | ✅ | 1 GB/month, 20k req/month | **yes, every 7d** | account + token | sát cap với chat use case |
| **Tailscale Funnel** (`*.<tailnet>.ts.net`) | **✅** | **no cap (fair use)** | **none** | apt + login + 1 cmd | **WINNER** |
| Cloudflare Named Tunnel | ✅ | unlimited | none | requires owned domain on CF | violates "no DNS" req |

**Tailscale Funnel wins** because:

1. No bandwidth cap on the free Personal plan — chat traffic
   (SSE-streamed Claude responses) won't push us into a paid tier.
2. No interstitial page — the operator just clicks the bookmark and
   sees the dashboard.
3. URL is stable and human-readable: `agent-me-host.<tailnet>.ts.net`.
4. The funnel daemon is already enterprise-grade infrastructure;
   uptime is not a concern.
5. Outbound-only from Colossus (port 41641 UDP + DERP relays) — no
   inbound holes punched, same security posture as the bridge.

**Trade-offs accepted:**

- Need to install `tailscale` package on Colossus (one `apt install`,
  documented in `scripts/install-dashboard.sh`).
- Need a free Tailscale account (sign-in via Google/GitHub OAuth,
  takes 1 minute).
- One-time `sudo tailscale up` (auth via browser).
- One-time `sudo tailscale funnel --bg 8765` (saves config).

**Verification before deployment to Colossus:**

```bash
# On Colossus, after Phase 3 stabilises:
curl -fsSL --connect-timeout 5 https://login.tailscale.com -o /dev/null \
  && echo "✓ tailscale.com reachable" || echo "✗ blocked"
curl -fsSL --connect-timeout 5 https://controlplane.tailscale.com -o /dev/null \
  && echo "✓ controlplane reachable" || echo "✗ blocked"
```

If either is blocked, fall back to ngrok (script will detect and
prompt). If both blocked, dashboard remains local-only on Colossus
and operator opens via SSH port-forward.

## Auth

Single shared bearer token, mandatory on every non-static request.
The token comes from `DASHBOARD_TOKEN` in `configs/.env`; if unset
at startup the service refuses to bind a public port (only
`127.0.0.1` allowed).

**Browser flow:**

1. Operator opens `https://<host>.<tailnet>.ts.net/?t=<token>` once.
2. Server sets a long-lived signed cookie (`itsdangerous` URLSafeSerializer).
3. Subsequent requests authenticated via cookie; no `?t=` needed.
4. Cookie scope: HttpOnly, SameSite=Lax, Secure (Tailscale terminates TLS).
5. Logout clears cookie.

**API flow (CLI / curl):**

- Header: `Authorization: Bearer <token>`.

**Why not OAuth / Cloudflare Access / etc.:** single user, single
device, single token = not worth the operational complexity.

The token lives in `~/agent-me-secrets.md` (vault file) once
generated — `scripts/install-dashboard.sh` generates a 32-byte
URL-safe base64 token if `DASHBOARD_TOKEN` isn't set, prints it
once, and reminds the operator to add it to the vault.

## Isolation from the Slack bridge

The bridge keeps writing to `~/.local/state/agent-me/state.db` and
the dashboard reads from the same file. SQLite WAL mode + shared
file locking handles this safely:

- Bridge opens DB with normal R/W connection (existing code).
- Dashboard opens DB with `mode=ro&immutable=0` URI connection — i.e.
  read-only but allows snapshot reads of WAL-committed data.
- Multiple readers + one writer is the canonical SQLite WAL
  pattern; no contention beyond ~µs WAL header reads.
- Dashboard never holds a long-running transaction; every query
  uses a fresh `SELECT` and closes.

**Failure modes considered:**

- Bridge restarts mid-dashboard-load → dashboard query may return
  stale snapshot (acceptable; no consistency guarantee needed).
- Dashboard crashes → bridge unaffected (separate process).
- DB file corrupted → both die. Mitigation: bridge already runs
  in a systemd unit with `Restart=on-failure`; dashboard does
  same. SQLite corruption is rare with WAL.

**No code path in the dashboard writes to the bridge's DB.** The
bridge's state is the bridge's. If the dashboard ever needs to
persist its own state (job queue for "refresh all" buttons, etc.)
it gets its own DB at `${STATE_DIR}/dashboard.db`.

## Refresh-on-demand (single-flight)

Each "Refresh <source>" button POSTs `/api/refresh/<source>`. Server
holds an `asyncio.Lock` per source; if a refresh is in flight,
new requests get `409 Conflict` with the existing job id.

The job spawns `agent-me-brief --period day --source <source>
--no-post --json-out <path>` (a new flag we'll add to `daily_brief.py`
for dashboard use). Output is read back, parsed into `BriefItem`s,
cached at `${STATE_DIR}/dashboard-cache/<source>.json`, and pushed
over SSE to subscribed browsers.

Why not "refresh all" → just iterate sources. The 6am cron still
posts to Slack as before; dashboard refresh is `--no-post`, so it
doesn't double-post.

## SSE channels

Two SSE endpoints:

- `GET /api/sse/logs` — tails `bridge.log` + `brief.log` JSON-line by
  line, multiplexes both with a `source` field. Client subscribes
  on Ops panel page.
- `GET /api/sse/refresh/<job_id>` — streams progress events from a
  running refresh job (started → fetching → parsed → done).

Implementation note: Starlette has `StreamingResponse` but for SSE
we use the `sse-starlette` package (already trivial to add; pure
Python, no native deps).

## URL routes (draft)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | Overview page — count badges per source, last brief age, MCP health summary |
| `GET` | `/source/<id>` | Per-source page — full item list, group filter, refresh btn |
| `GET` | `/ops` | Ops panel — MCP table, bridge status, recent jobs, log live tail |
| `GET` | `/api/state` | JSON dump of SQLite snapshot (threads, sessions, approvals counts) |
| `GET` | `/api/source/<id>` | JSON: cached brief items for source |
| `POST` | `/api/refresh/<id>` | Start refresh job; returns `{"job_id": "..."}` |
| `GET` | `/api/sse/logs` | SSE: log lines |
| `GET` | `/api/sse/refresh/<job_id>` | SSE: job progress |
| `GET` | `/healthz` | Unauthenticated; returns `{"ok": true, "uptime": ...}` |
| `GET` | `/static/*` | CSS/JS bundles |

`<id>` is one of: `jira`, `gitlab`, `confluence`, `nvbugs`, `slack`,
`outlook`, `github`.

## UI stack

- **Templating:** Jinja2 (FileSystemLoader from `templates/`).
- **CSS:** Tailwind via CDN (`<script src="https://cdn.tailwindcss.com">`)
  for zero build step; we'll switch to compiled tailwind once the
  layout settles.
- **JS:** Alpine.js via CDN. Stores hold per-source items, refresh
  state, log buffer. SSE handled with native `EventSource`.
- **No bundler.** No npm. No node_modules. Pure server-rendered HTML
  + sprinkled reactivity.

## File layout

```
src/agent_me/dashboard/
├── __init__.py
├── app.py              # Starlette ASGI app + routes
├── auth.py             # Bearer-token middleware + cookie helpers
├── state_reader.py     # Read-only SQLite + brief cache + log tail
├── brief_runner.py     # Single-flight on-demand brief subprocess
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── source.html
│   ├── ops.html
│   └── partials/
│       ├── source_card.html
│       └── nav.html
└── static/
    ├── app.css         # Tiny on-top-of-tailwind layer
    └── app.js          # Alpine stores + SSE wiring

design/dashboard-design.md         # this file

deploy/agent-me-dashboard.service  # systemd unit
deploy/agent-me-funnel.service     # systemd unit (Tailscale)

scripts/install-dashboard.sh       # idempotent setup
```

## Brief parser changes (additive)

`daily_brief.py` currently fans out 7 subagents and posts to Slack.
We add a `--source <id>` flag that runs only one subagent and a
`--no-post` flag that prints JSON to stdout instead of posting.
This is purely additive: existing `agent-me-brief` callers are
unaffected. The existing `--dry-run` already does most of what
`--no-post` does, but `--no-post` differs in that the **single
source path** still produces a parseable JSON envelope so the
dashboard can ingest it without re-implementing parser logic.

## Open questions / parking lot

- **HTTP cache headers:** static assets cached aggressively
  (1 year), HTML pages `no-cache`, JSON API `no-store`. Set in
  middleware.
- **Refresh-all on schedule?** Probably not — bridge already runs
  the 6am brief which writes to brief.log; dashboard reads from
  there. Manual refresh is the dashboard's superpower; automated
  refresh on a separate schedule is unnecessary.
- **Auth: token vs OAuth?** Sticking with token. If we ever want
  team usage, swap to Cloudflare Access or Tailscale Identity.
- **Persistent jobs:** if the dashboard restarts mid-refresh, the
  spawned brief subprocess gets killed (PR_SET_PDEATHSIG inherited
  through systemd). Acceptable for draft; jobs are 30–40s.
- **Slack/Outlook source isolation:** a refresh from the dashboard
  spawns a fresh `claude -p` with that one source's MCP scope.
  Same auth state as the cron brief — if MCPs need re-auth, both
  fail in the same way.
- **Audit log of who hit refresh:** single user; not needed for
  draft. If we ever expand, log to `dashboard.db`.
- **Mobile UX:** untested for draft; main view is desktop.
- **Adding a chat tab later:** would mean piping `claude -p` from
  the dashboard with its own session-id space (separate from
  Slack threads in the bridge's `claude_sessions` table). Out of
  scope for this draft but explicitly considered: chat-from-web
  uses a different `chat_sessions` table that the bridge doesn't
  touch.

## What's deliberately NOT in this draft

- Phase 2b approval-gate UI buttons (just shows a count).
- MCP re-auth from the browser (still CLI-only via `agent-me-reauth`).
- Brief markdown export.
- Search across briefs.
- Dark mode (defaults to dark; no toggle).
- Mobile drawer nav.

## Pre-deploy checklist (when Colossus is ready)

1. `claude mcp list` → all 17 ✓ Connected (Phase 3 prerequisite).
2. `curl https://login.tailscale.com` from Colossus → 200/302.
3. `apt install tailscale` (or upstream installer).
4. `sudo tailscale up` (browser auth via SSH port-forward).
5. `sudo tailscale funnel --bg 8765`.
6. Generate `DASHBOARD_TOKEN`, add to `configs/.env`.
7. `./scripts/install-dashboard.sh` → installs both systemd units.
8. `systemctl --user start agent-me-dashboard agent-me-funnel`.
9. Open `https://<host>.<tailnet>.ts.net/?t=<token>` in browser.
10. Verify: source tabs render, refresh works, log SSE streams.
