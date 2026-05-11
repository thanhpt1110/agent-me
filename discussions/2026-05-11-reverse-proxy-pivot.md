# 2026-05-11 — Phase 4 dashboard pivot: Tailscale Funnel → NVIDIA reverse proxy

_~30 minute session. Context: operator stood up an NVIDIA-internal
reverse proxy at `agent-me.nvidia.com` and wants the dashboard to use
it instead of Tailscale Funnel._

## Why the pivot

The original Phase 4 plan (commit `b567b3f`, design at
`design/dashboard-design.md`) chose Tailscale Funnel for the public
URL because the brief was "no own DNS, no bandwidth cap, no
interstitial". That's still all true.

What changed: the operator now has a real DNS name (`agent-me.nvidia.com`)
on a real reverse proxy operated by NVIDIA infra. Properties:

- TLS terminated by the proxy with NVIDIA-internal CA certs.
- Access controlled via NVIDIA VPN — non-VPN clients fail at the
  network layer before even reaching the proxy.
- Standard `*.nvidia.com` zone, trusted by every NVIDIA browser.

That removes every reason Tailscale was picked. The Tailscale path
is still useful for "check dashboard from a phone without VPN", so
we kept it as `--tailscale` opt-in instead of deleting.

## Five trade-offs the operator confirmed (AskQuestion this session)

1. **Proxy target** → forward to `<colossus>:8765` (current dashboard
   port). Just need to flip bind from `127.0.0.1` to `0.0.0.0`.
2. **TLS** → proxy does TLS, BE plain HTTP. Standard.
3. **FE deploy** → keep monolithic Jinja same-origin. Don't split
   into static SPA. Simplest, current code works.
4. **Auth** → VPN-only by default. `DASHBOARD_TRUST_NETWORK=1` is the
   new env flag that tells the entry point a non-loopback bind is OK
   without `DASHBOARD_TOKEN`. Token stays as opt-in defense-in-depth.
5. **SSE through proxy** → unknown; we ship config snippets for the
   proxy admin (nginx/caddy/traefik) with explicit
   `proxy_buffering off` + 1h read timeout for `/api/sse/*`.

## What landed in this commit

- `src/agent_me/dashboard/auth.py` — new `DASHBOARD_TRUST_NETWORK=1`
  env-var path through `auth_required_for_public_bind()`. When set,
  the entry point no longer requires `DASHBOARD_TOKEN` for a
  non-loopback bind. Middleware now reports
  `X-Dashboard-Auth: trust-network` / `cookie` / `bearer` /
  `disabled` so the proxy admin can sanity-check without inspecting
  the body.
- `src/agent_me/dashboard/app.py` — new `--forwarded-allow-ips` flag
  (default `127.0.0.1`, env override `FORWARDED_ALLOW_IPS`); set to
  `*` in the systemd unit. uvicorn now also passes
  `proxy_headers=True` so Starlette's `request.url.scheme` reads as
  `https` when the proxy says so. That matters for `Secure=true`
  cookies and any redirect URL Starlette constructs.
- `deploy/agent-me-dashboard.service` — `ExecStart` flips to
  `--host 0.0.0.0 --port 8765 --forwarded-allow-ips=*`. Comment
  block at the top explains the trust model and how to revert to
  loopback for dev.
- `scripts/install-dashboard.sh` — rewritten:
  - Default mode = reverse-proxy (no Tailscale install).
  - `--tailscale` flag = also enable Tailscale Funnel as a backup.
  - `--token` flag = generate `DASHBOARD_TOKEN` (defense-in-depth).
  - Always appends `DASHBOARD_TRUST_NETWORK=1` to `configs/.env`.
  - Final printout points at `https://agent-me.nvidia.com` and
    optionally the `*.ts.net` URL if `--tailscale` was set.
- `design/reverse-proxy-config.md` — **new**. Drop-in nginx, caddy,
  and traefik snippets; sanity-check curl commands; what the backend
  needs from the proxy (X-Forwarded-*, SSE pass-through, long
  timeouts, no buffering on `/api/sse/*`); auth model; what's
  deliberately out of scope.
- `design/dashboard-design.md` — replaced "Why Tailscale Funnel
  (locked decision)" with "Why a reverse proxy on agent-me.nvidia.com
  (locked decision, 2026-05-11)". Architecture diagram redrawn.
  Auth section split into Option A (VPN-only) + Option B (VPN +
  token). Pre-deploy checklist updated.
- `STATE.md` — Phase 4 locked decisions updated; Roadmap §4
  redirected to reverse-proxy mode; date stamp bumped.
- `discussions/ideas.md` — captured the SSO-header future-work and
  the PA CLI hybrid investigation that got interrupted by this
  pivot.

## What is deliberately not in this commit

- **No removal of Tailscale code** — `deploy/agent-me-funnel.service`
  and the funnel branch in `install-dashboard.sh` stay, just gated
  behind `--tailscale`. Useful for off-VPN backup access; deleting
  forecloses an option for no benefit.
- **No SSE polling fallback** — the user picked "SSE: unknown, doc
  config for proxy admin". If the deployed proxy turns out not to
  support SSE, we'll add polling later. Document the symptoms in
  `design/reverse-proxy-config.md` so the diagnosis is fast.
- **No CORS middleware** — picked monolithic Jinja same-origin, so
  no cross-origin requests. If the FE ever splits to a different
  host, add `starlette.middleware.cors.CORSMiddleware` in app.py.
- **No identity-aware logging** — the proxy doesn't yet pass an
  `X-Auth-User` header. Captured in `ideas.md` so when it lands,
  it's a 5-minute middleware add, not a forgotten future-work.

## PA CLI sidetrack (interrupted by this pivot)

Earlier this session the operator wanted to revisit the PA hybrid.
We installed PA CLI v0.1.105 (`~/.pa/bin/pa`), authed via
`pa login --vm`, got 10/13 services connected (Slack + Glean still
absent). Benchmarks revealed PA's `-p` mode hangs without a TTY —
the previous "PA hybrid revert" likely hit this. Workable two ways:

1. Use `pty.openpty()` to spawn PA the same way `agent-me-reauth`
   already does (PTY-aware spawn from Python).
2. Use `pa mcp` (a real subcommand!) to expose PA as a Claude MCP
   server. `claude mcp add pa --scope user -- ~/.pa/bin/pa mcp`.
   Cleanest fit: Claude treats PA as another tool, no fallback
   logic in the bridge.

Mid-investigation the operator pivoted to the reverse-proxy work,
so PA exploration is captured in `ideas.md` for whenever it
becomes the priority again.

## Next moves on the operator's plate

1. Hand `design/reverse-proxy-config.md` to whoever runs
   `agent-me.nvidia.com`. Pick one of the three snippets; the
   sanity-checklist at the bottom of the doc tells the proxy admin
   when their config is correct.
2. After Phase 3 Colossus stabilises:
   - `git pull` on Colossus picks this commit up via the watcher.
   - `./scripts/install-dashboard.sh` (no flags = reverse-proxy mode)
     will run on next reboot, or the user can run it manually now.
3. From a VPN'd Mac browser, open `https://agent-me.nvidia.com/`.
   Expected: 7 source cards, ⚙ Ops panel, 📜 Logs page with three
   live tabs (watcher / slack interactions / claude session trace).
