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
                ┌─────────────────────────────────────┐
                │  Browser (NVIDIA-VPN'd)             │
                │  https://agent-me.nvidia.com        │
                └─────────────────┬───────────────────┘
                                  │ HTTPS
                                  ▼
                ┌─────────────────────────────────────┐
                │  NVIDIA-internal reverse proxy      │
                │  (operated externally to this repo) │
                │  - terminates TLS                   │
                │  - sets X-Forwarded-Proto, -For,    │
                │    -Host                            │
                │  - SSE pass-through (no buffering)  │
                │  See design/reverse-proxy-config.md │
                └─────────────────┬───────────────────┘
                                  │ HTTP, plain
                                  │ (private network)
                                  ▼
       ┌────────────────────────────────────────────┐
       │  Colossus host (24/7)                      │
       │                                            │
       │  ┌──────────────────────────────────┐      │
       │  │ agent-me-dashboard.service       │      │
       │  │  uvicorn + Starlette ASGI app    │      │
       │  │  bind 0.0.0.0:8765               │      │
       │  │  --forwarded-allow-ips='*'       │      │
       │  │   - GET  /          (overview)   │      │
       │  │   - GET  /source/<id>            │      │
       │  │   - GET  /ops                    │      │
       │  │   - GET  /logs                   │      │
       │  │   - POST /api/refresh/<id>       │      │
       │  │   - GET  /api/sse/logs/{w,s,sn}  │      │
       │  │   - GET  /api/sse/refresh/<job>  │      │
       │  │   - GET  /healthz (unauth)       │      │
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

**One new systemd unit (always):**

- `agent-me-dashboard.service` — runs `uvicorn agent_me.dashboard.app:app
  --host 0.0.0.0 --port 8765 --forwarded-allow-ips=*`. Listens on every
  interface so the upstream reverse proxy on the NVIDIA private network
  can reach it. Trusts X-Forwarded-* headers — the proxy is the source
  of those, and only NVIDIA-VPN traffic reaches the proxy.

**One opt-in unit:**

- `agent-me-funnel.service` — runs `tailscale funnel` for a backup
  Tailscale Funnel URL (`*.<tailnet>.ts.net`). Disabled by default;
  enable with `./scripts/install-dashboard.sh --tailscale` if you also
  want to reach the dashboard from outside the NVIDIA VPN (e.g. when
  travelling without VPN).

**The bridge service is not modified.** Bridge keeps its own SQLite
connection (WAL mode), keeps its own `claude -p` invocations, keeps
its Socket Mode WebSocket. The dashboard is a strict additive.

## Why a reverse proxy on `agent-me.nvidia.com` (locked decision, 2026-05-11)

The original design (commit `b567b3f`) chose **Tailscale Funnel** as
the public URL because the brief was "no own DNS, no bandwidth cap, no
interstitial". That worked but introduced extra moving parts (tailscaled
daemon, Tailscale account, funnel admin console). On 2026-05-11 the
operator stood up an **NVIDIA-internal reverse proxy** at
`agent-me.nvidia.com` that already does:

- TLS termination with NVIDIA-internal CA certs.
- Access control via NVIDIA VPN — only employees can reach the
  hostname; non-VPN clients fail at the network layer.
- DNS via the standard `*.nvidia.com` zone — already trusted by every
  NVIDIA browser.

That removes every reason Tailscale was picked. We keep the Tailscale
unit + install flag because it's still useful for the "check
dashboard from a phone without VPN" case.

| Option | Status | Notes |
|---|---|---|
| **NVIDIA reverse proxy + VPN** | **Default since 2026-05-11** | TLS, DNS, access control all handled upstream. Most operator-friendly. |
| Tailscale Funnel | Opt-in via `--tailscale` | Backup public URL when off-VPN. |
| Cloudflare / ngrok / SSH tunnel | Not configured | Old options, kept in design history (commit `b567b3f`). |

**Pre-deploy verification:** see `design/reverse-proxy-config.md`
("Sanity checklist for the proxy admin"). After the proxy is live and
this dashboard is running, hit `curl -sSL https://agent-me.nvidia.com/healthz`
from a VPN'd machine — should return `{"ok": true, ...}`.

## Auth

Two layered options the operator picks at install time. Both honour
the `X-Forwarded-Proto` header so cookies set `Secure=true` correctly
when the proxy terminates TLS.

### Option A — VPN-only (default since 2026-05-11)

Set `DASHBOARD_TRUST_NETWORK=1` in `configs/.env`. The dashboard entry
point will permit a non-loopback bind without `DASHBOARD_TOKEN`. The
trust boundary is the NVIDIA VPN: anyone who can reach
`agent-me.nvidia.com` is, by network policy, an NVIDIA employee.

Trade-off: every NVIDIA-VPN'd employee can see the dashboard. There's
no per-user filtering inside the app. This is acceptable because:

- The Slack flow is independent and still gated by `SLACK_ALLOWED_USER_ID`
  — no one else can DM the bot or trigger writes.
- The dashboard is read-only over the bridge state + brief output; the
  on-demand "Refresh" button kicks one query that doesn't post to Slack.
- Approval-gate buttons (Phase 2b) are Slack-side, not dashboard-side.

A response header `X-Dashboard-Auth: trust-network` confirms the model
is active so the proxy admin can sanity-check.

### Option B — VPN + bearer/cookie (defense-in-depth)

Run `./scripts/install-dashboard.sh --token` to generate
`DASHBOARD_TOKEN`. The middleware then requires:

- **Browser flow**: visit `https://agent-me.nvidia.com/?t=<token>`
  once; server sets a 30-day signed cookie. Subsequent requests
  authenticated via cookie alone.
- **API flow**: `Authorization: Bearer <token>` header.

Use this when multiple NVIDIA employees can VPN but only one operator
should see the dashboard, or when you want belt-and-suspenders for
rotating production credentials.

The token lives in `~/agent-me-secrets.md` (the per-operator vault file
outside the repo). The middleware also adds `X-Dashboard-Auth: cookie`
or `bearer` so you can trace which path authenticated each request.

### Exempt paths (no auth in either option)

- `GET /healthz` — used by the proxy upstream-liveness probe.
- `GET /static/*` — assets, not sensitive.
- `GET /favicon.ico`.
- `GET /login` and `POST /api/login` — needed to set the cookie when
  Option B is in use.

### Why not full SSO?

Operationally, the proxy already enforces VPN; that's effectively SSO
at the network layer. Adding OAuth at the app layer would mean
plumbing tokens, refresh, and identity for a single-operator app.
Not worth it. If an NVIDIA SSO header gateway is ever standardised
(`X-Auth-User` etc.), we can add a lightweight middleware that reads
it; until then VPN + optional shared-secret is the simplest model
that fits.

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
design/reverse-proxy-config.md     # nginx/caddy/traefik snippets for the proxy admin

deploy/agent-me-dashboard.service  # systemd unit (always installed)
deploy/agent-me-funnel.service     # systemd unit (opt-in via --tailscale)

scripts/install-dashboard.sh       # idempotent setup; default = reverse-proxy mode
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
- **Auth: token vs OAuth?** Sticking with VPN + optional token.
  If team usage ever lands, the cleanest path is to add a tiny
  middleware that reads an `X-Auth-User` (or similar) header set by
  the reverse proxy after upstream SSO; until that header exists in
  the NVIDIA proxy stack, VPN is the auth boundary.
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
2. Reverse proxy at `agent-me.nvidia.com` is configured to forward to
   `<colossus-host>:8765` over plain HTTP. See
   `design/reverse-proxy-config.md` for nginx / caddy / traefik
   snippets to send the proxy admin.
3. From an NVIDIA-VPN'd machine: `curl -sSL https://agent-me.nvidia.com/healthz`
   eventually returns `{"ok": true, ...}` once the dashboard service
   is up. (Until then it 502s — that's normal.)
4. `./scripts/install-dashboard.sh` from the repo root. The script:
   - `uv sync` to pull dashboard deps,
   - appends `DASHBOARD_TRUST_NETWORK=1` to `configs/.env`,
   - installs and starts `agent-me-dashboard.service`,
   - waits ~2s and curls `127.0.0.1:8765/healthz`.
   - Add `--token` for defense-in-depth (Option B); add `--tailscale`
     for the optional VPN-less backup URL.
5. Open `https://agent-me.nvidia.com/` in a VPN'd browser.
6. Verify: overview page renders 7 source cards, ⚙ Ops panel opens, 📜
   Logs page streams the watcher journal + bridge events + a session
   trace once you've picked a session from the dropdown.
