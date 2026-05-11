# 2026-05-11 — End-to-end deploy walkthrough + proxy host playbook

_Same day as the reverse-proxy pivot. Operator wants to clone repo on
the proxy host and have a Cursor session walk it through `nginx`/cert
config end-to-end without back-and-forth._

## What landed in this commit

Three docs + one walkthrough log + a smoke-deploy run.

### 1. `design/deploy-proxy-on-host.md` (NEW)

Step-by-step playbook the proxy-host Cursor session reads. Mirrors
the structure of the existing `design/deploy-on-host.md` (which
covers the backend host) so an agent flipping between the two has a
familiar shape.

Sections:
- Architecture refresher (3-layer browser → proxy → backend diagram)
- "Things the human needs to tell you" — backend hostname/port +
  TLS cert source. The doc explicitly tells the agent to pause and
  ask if these aren't shared, instead of guessing.
- Outbound network sanity check (must reach
  `<backend>:8765/healthz` from the proxy host).
- Step 1 — clone repo (so config snippets are local).
- Step 2 — pick proxy software (nginx > caddy > traefik).
- Steps 3a/3b/3c — copy-paste config blocks per software with
  `BACKEND_HOST` / `BACKEND_PORT` placeholders + a `sed` to
  substitute them.
- Step 4 — TLS cert. Deliberately **non-prescriptive** because
  NVIDIA's CA process varies (pre-issued / cert-manager / Vault /
  ACME). The doc tells the agent to pick from the table or fail loud.
- Step 5 — five `curl` verification commands covering local proxy
  test, end-to-end VPN test, auth header, SSE flush, HTML render.
- Things-that-can-go-wrong table with 7 common failure modes mapped
  to fixes (DNS, 502, auth-disabled-instead-of-trust-network, SSE
  buffering, cert chain).
- Hardening / defense-in-depth (HSTS, IP allow-list, rate-limit,
  optional `DASHBOARD_TOKEN`).
- Cross-references back to `reverse-proxy-config.md` and
  `dashboard-design.md`.

### 2. `design/deploy-on-host.md` step 9 added

The existing backend playbook stopped at step 8 (auto-deploy verify).
Step 9 is the dashboard install (`./scripts/install-dashboard.sh`)
with verification curls. Includes the three flags
(`--tailscale` / `--token` / default reverse-proxy).

### 3. `README.md` step 8 added

Quickstart now lists the dashboard install as an optional step + the
two-host setup pointer (`deploy-on-host.md` for backend,
`deploy-proxy-on-host.md` for proxy).

### 4. `STATE.md` updated

Adds the "reverse-proxy pivot + end-to-end deploy doc" entry under
Done with concrete numbers from the local smoke test (9 threads / 18
messages / 7 sessions / 7 pending approvals — real bridge data, not
fixtures). Also notes the docs are written so a Cursor agent on the
proxy host can carry the work through.

## Smoke-deploy verification

Live test on this host (`/localhome/local-thaphan/agent-me`) before
writing the doc, to make sure the install path actually works:

```
─ /healthz ─                {"ok": true, "uptime_s": 2, ...}
─ X-Dashboard-Auth ─        trust-network
─ / (HTML) ─                21,694 bytes
─ /logs ─                   17,066 bytes
─ /ops ─                    17,783 bytes
─ /source/jira ─            12,663 bytes
─ /api/state ─              uptime: 2s, 9 threads (9 active 24h),
                            7 sessions, 7 sources
─ /api/sse/logs/slack ─     real bridge events streaming live:
                            • message_received from 1778486578.375479
                            • query_failed (msg_too_long) from
                              1778485901.591509 — *exactly the bug
                              the operator's PA-hybrid in-flight work
                              fixes via chunk_for_slack()*
─ ss -tlnp ─                LISTEN 0.0.0.0:8765 (uvicorn)
```

Two things the smoke test exposed worth keeping:

1. **Bridge SSE shows real failures the operator is fixing.** The
   `msg_too_long` errors visible in the dashboard's Slack-interactions
   tab are the exact reason the operator is mid-flight on
   `chunk_for_slack()` in the bridge module. Demonstrates the
   "dashboard as live debugger" use case is real.
2. **`X-Dashboard-Auth: trust-network`** confirmed working. That's
   the new header the proxy admin should look for in the curl
   verification step 5.2 of `deploy-proxy-on-host.md`.

## Operator's plate, after this lands

1. **On the backend host (Colossus):**
   ```bash
   cd ~/agent-me && git pull
   ./scripts/install-dashboard.sh   # default reverse-proxy mode
   ```
   The auto-deploy watcher already restarts agent-me-bridge +
   agent-me-dashboard on every push.

2. **On the proxy host (whoever serves agent-me.nvidia.com):**
   ```bash
   git clone https://github.com/thanhpt1110/agent-me.git
   cd agent-me
   # Open Cursor here and say:
   #   "Read design/deploy-proxy-on-host.md and walk me through it.
   #    Backend host is <hostname>; cert source is <option>."
   ```
   Cursor reads the playbook + drives nginx/caddy/cert config.
   Operator confirms the verify curls return green.

3. **Smoke from a VPN'd Mac:**
   ```bash
   curl -sSL https://agent-me.nvidia.com/healthz
   # Open https://agent-me.nvidia.com in browser.
   ```

## Slack bridge PA-hybrid work intentionally still uncommitted

Same as the previous reverse-proxy commit: `slack_bridge/app.py` and
`slack_bridge/approvals.py` carry the operator's mid-flight PA hybrid
(`SYSTEM_PROMPT_TEMPLATE`, `chunk_for_slack`, `Bash` auto-allow,
HOOK_MATCHER anchoring). Left for the operator to commit under their
own message describing the PA design rationale.

## Cross-refs

- `design/deploy-on-host.md` — backend host playbook.
- `design/deploy-proxy-on-host.md` — **NEW** proxy host playbook.
- `design/reverse-proxy-config.md` — config snippet bank.
- `design/dashboard-design.md` — backend architecture.
- Previous pivot log: `discussions/2026-05-11-reverse-proxy-pivot.md`.
