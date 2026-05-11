# 2026-05-11 — Proxy host deployed at 10.25.186.74 (Caddy, HTTP-only)

_Cursor session ran on the proxy host itself (hostname `agent-me`,
`10.25.186.74`). Operator invoked the playbook
`design/deploy-proxy-on-host.md` and asked the session to walk it
through with the real values. End state:
`http://agent-me.nvidia.com` reverse-proxies to the Colossus dashboard.
TLS deliberately skipped — operator is the only consumer for now and
will revisit the NVIDIA-internal cert process later._

## End state on the proxy host

| Where | What |
|---|---|
| Host | hostname `agent-me`, IP `10.25.186.74`, Ubuntu, single-user `thaphan` |
| Proxy software | Caddy `v2.11.2` (already installed, user-local at `~/.local/bin/caddy`) |
| Caddyfile | `~/.config/caddy/Caddyfile` (full contents inline below) |
| Systemd unit | `~/.config/systemd/user/caddy.service` (`active (running)`, `enabled`, `Linger=yes` ✓) |
| Bind | `*:80` (HTTP) — no `:443`, no TLS |
| Upstream | `ipp1-2252.ipp1a1.colossus.nvidia.com:8765` (= Colossus host where `agent-me-dashboard.service` runs as per `deploy/agent-me-dashboard.service`) |
| Proxy → Colossus RTT | ~227 ms (cross-DC); keepalive is set on both branches to amortize |
| Health check | Caddy hits `/healthz` every 30 s (`fails: 0` as of session end) |
| DNS | `agent-me.nvidia.com → 10.25.186.74` already in place by NVIDIA infra; this session did not touch DNS |
| TLS | **Deliberately not done.** Operator chose HTTP-only since they're the only user; cert path documented at end of this file |

## What the operator confirmed mid-session (3 AskQuestion rounds)

1. **Upstream:** point at the Colossus dashboard
   (`ipp1-2252.ipp1a1.colossus.nvidia.com:8765`, Python uvicorn / Starlette
   ASGI). Earlier in the session the agent saw a *local* Node Express
   listening on `127.0.0.1:7373` at `/home/thaphan/projects/agent-me-dashboard/`,
   noted the divergence from the playbook (which describes the Python
   uvicorn:8765 backend), and paused to confirm. The local Node app
   appears to be unrelated to this repo — left untouched, not proxied to.
2. **Cert source:** `skip_https` — HTTP-only for now.
3. **Privileged port:** `sudo_ready` — operator entered sudo password
   once for `setcap` to let user-local Caddy bind port 80 without root.

## What the session actually ran (replayable, top-to-bottom)

### 0. Read-only diagnostics (no side effects)

```bash
getent hosts agent-me.nvidia.com           # → 10.25.186.74
command -v nginx caddy traefik             # → ~/.local/bin/caddy only
ss -ltn                                    # nothing on :80 or :443
systemctl --user list-units --type=service | grep agent-me
# → agent-me-dashboard.service active (Express, 127.0.0.1:7373) — NOT the upstream we want
ping -c 2 10.117.8.41                      # 227 ms, route OK
curl -fsS http://10.117.8.41:8765/healthz  # {"ok":true,...} — confirmed Colossus reachable
getcap ~/.local/bin/caddy                  # empty — needs setcap
sysctl net.ipv4.ip_unprivileged_port_start # 1024 (so :80 still privileged)
loginctl show-user thaphan -p Linger       # Linger=yes (good — survives logout)
```

### 1. Wrote the user-level config files (no sudo)

```bash
mkdir -p ~/.config/caddy ~/.config/systemd/user ~/.local/state/caddy
# then wrote ~/.config/caddy/Caddyfile and ~/.config/systemd/user/caddy.service
# (full contents in the next section)
~/.local/bin/caddy validate --config ~/.config/caddy/Caddyfile --adapter caddyfile
# → "Valid configuration"
~/.local/bin/caddy fmt --overwrite ~/.config/caddy/Caddyfile
```

### 2. Operator ran (one-time sudo)

```bash
sudo setcap cap_net_bind_service=+ep /home/thaphan/.local/bin/caddy
getcap /home/thaphan/.local/bin/caddy
# → /home/thaphan/.local/bin/caddy cap_net_bind_service=ep
```

This is the **only** sudo command in the whole flow. After this, the
user-local Caddy binary can bind `:80` without root. The unit file is
plain user-systemd; no system unit, no `/etc/caddy/`, no `sudo
systemctl`.

### 3. Started the service

```bash
systemctl --user daemon-reload
systemctl --user enable --now caddy.service
systemctl --user status caddy.service     # active (running)
ss -ltn | grep ':80 '                     # LISTEN *:80
```

### 4. Verified (playbook Step 5, 5/5 green)

```bash
# 4.1 syntax
~/.local/bin/caddy validate --config ~/.config/caddy/Caddyfile --adapter caddyfile
# → Valid configuration

# 4.2 /healthz via the public hostname
curl -sS http://agent-me.nvidia.com/healthz
# → {"ok":true,"uptime_s":1388,"now_ms":1778489582885}

# 4.3 auth header should be trust-network
curl -sSI http://agent-me.nvidia.com/ | grep -i x-dashboard-auth
# → X-Dashboard-Auth: trust-network

# 4.4 HTML title from uvicorn
curl -sS http://agent-me.nvidia.com/ | grep -E "<title>"
# → <title>Overview · agent-me dashboard</title>

# 4.5 Real browser hit captured in the access log
tail -2 ~/.local/state/caddy/agent-me.access.log
# remote_ip 172.29.98.95, UA Edg/148, host agent-me.nvidia.com,
# status 200, size 21696, resp.Server uvicorn  — operator's Edge confirmed end-to-end.
```

### 5. Health-check status on the admin endpoint

```bash
curl -sS http://localhost:2019/reverse_proxy/upstreams
# → [{"address":"ipp1-2252.ipp1a1.colossus.nvidia.com:8765","num_requests":0,"fails":0}]
```

## File contents (verbatim, on the proxy host)

### `~/.config/caddy/Caddyfile`

```caddy
# Reverse proxy for agent-me.nvidia.com
# Generated for proxy host 10.25.186.74 (hostname: agent-me)
# Backend: Python uvicorn agent-me dashboard
#   host:  ipp1-2252.ipp1a1.colossus.nvidia.com (10.117.8.41)
#   port:  8765
#   RTT proxy->colossus ~227ms (cross-DC) — keepalive matters.
# Mode: HTTP-only (no TLS yet) per operator decision; revisit with cert later.
{
	auto_https off
	admin localhost:2019
	log {
		output file /home/thaphan/.local/state/caddy/caddy.log
		format console
	}
}

# Accept connections by hostname OR by raw IP, so that:
#   - http://agent-me.nvidia.com/   (NVIDIA-internal users on VPN)
#   - http://10.25.186.74/          (debugging from any internal box)
#   - http://localhost/             (on-host smoke tests)
http://agent-me.nvidia.com, http://10.25.186.74, http://localhost {
	log {
		output file /home/thaphan/.local/state/caddy/agent-me.access.log
		format console
	}

	# SSE streams: disable response buffering, allow long-lived connections.
	# Mirrors the SSE block from design/reverse-proxy-config.md.
	@sse path /api/sse/*
	handle @sse {
		reverse_proxy ipp1-2252.ipp1a1.colossus.nvidia.com:8765 {
			flush_interval -1
			transport http {
				read_timeout 1h
				write_timeout 1h
				dial_timeout 10s
				keepalive 60s
				keepalive_idle_conns 16
			}
		}
	}

	handle {
		reverse_proxy ipp1-2252.ipp1a1.colossus.nvidia.com:8765 {
			transport http {
				read_timeout 60s
				write_timeout 60s
				dial_timeout 10s
				keepalive 60s
				keepalive_idle_conns 16
			}
			health_uri /healthz
			health_interval 30s
			health_timeout 5s
		}
	}
}
```

### `~/.config/systemd/user/caddy.service`

```ini
[Unit]
Description=Caddy reverse proxy for agent-me.nvidia.com
Documentation=https://caddyserver.com/docs/
After=network-online.target agent-me-dashboard.service
Wants=network-online.target

[Service]
Type=notify
ExecStart=/home/thaphan/.local/bin/caddy run --config %h/.config/caddy/Caddyfile
ExecReload=/home/thaphan/.local/bin/caddy reload --config %h/.config/caddy/Caddyfile --force
TimeoutStopSec=10s
Restart=on-failure
RestartSec=5s
LimitNOFILE=1048576

[Install]
WantedBy=default.target
```

Note: `After=agent-me-dashboard.service` is correct only if the
**proxy host itself** also runs the dashboard. On this host the
`agent-me-dashboard.service` is a *different* (Node) app on
`127.0.0.1:7373` — unrelated to the Colossus uvicorn upstream. The
`After=` doesn't hurt (it's not `Requires=`) but a future agent who
splits proxy and dashboard onto separate hosts should drop that line.

## What is deliberately not in this deployment

1. **TLS / HTTPS.** Operator self-use, `https://agent-me.nvidia.com`
   will fail (connection refused on `:443`). Path forward when ready:
   - Gen CSR on the proxy host:
     ```bash
     mkdir -p ~/.config/caddy/private && chmod 700 ~/.config/caddy/private
     openssl req -new -newkey rsa:2048 -nodes \
       -keyout ~/.config/caddy/private/agent-me.nvidia.com.key \
       -out   ~/.config/caddy/private/agent-me.nvidia.com.csr \
       -subj  "/CN=agent-me.nvidia.com/O=NVIDIA Corporation" \
       -addext "subjectAltName = DNS:agent-me.nvidia.com"
     ```
   - Submit the `.csr` to **whatever** NVIDIA internal CA process applies
     (operator hasn't yet identified the exact team / Confluence runbook
     / ServiceNow form / Vault PKI path — Glean MCP wasn't available in
     this session so the agent didn't bring concrete answers).
   - When `.crt` arrives, place it next to the key, then in Caddyfile
     replace `http://agent-me.nvidia.com` with:
     ```caddy
     agent-me.nvidia.com {
         tls /home/thaphan/.config/caddy/private/agent-me.nvidia.com.crt \
             /home/thaphan/.config/caddy/private/agent-me.nvidia.com.key
         ...  # keep the rest of the block
     }
     ```
     `systemctl --user reload caddy.service` is zero-downtime.
   - Caddy automatically serves an HTTP→HTTPS redirect on `:80` once
     the hostname is configured for `tls`.
2. **logrotate** for `~/.local/state/caddy/agent-me.access.log`. Will
   grow unbounded. Either install systemd `logrotate` (system-level,
   needs sudo once) or switch the Caddyfile `log` directive to
   `output file ... { roll_size 100mb roll_keep 5 }` (zero-sudo).
3. **HSTS, IP allow-list, rate-limit.** All optional hardening from
   the playbook §"Hardening / defense-in-depth"; not needed for
   single-operator use.
4. **Backend-on-proxy-host.** The `agent-me-dashboard.service` running
   locally (Node Express, `127.0.0.1:7373`) is *not* the agent-me
   dashboard from this repo. Left alone; not part of this deployment.

## Why Caddy and not the playbook's preferred nginx

Playbook ranks nginx first ("most likely already installed at NVIDIA").
On this specific host nginx is **not** installed; Caddy v2.11.2 was.
Switching distributions / installing nginx via apt would have required
multiple sudo prompts and no real benefit for a single-operator
deployment. Caddy's syntax is also a smaller surface (≈55 lines vs
~80 lines for the equivalent nginx config).

## Notes for the next Cursor agent reading this repo

If the operator asks about `agent-me.nvidia.com`:

1. The proxy is on a **different host** from Colossus
   (proxy = `10.25.186.74` / hostname `agent-me`; backend =
   `10.117.8.41` / `ipp1-2252.ipp1a1.colossus.nvidia.com`).
2. Setup is **HTTP-only**. Telling the operator to "open
   `https://agent-me.nvidia.com`" will fail — it's `http://` until the
   cert work above lands.
3. All proxy state is in user-systemd at the operator's `$HOME` — no
   `/etc/caddy/`, no system-level service. SSH in as `thaphan`,
   inspect `~/.config/caddy/`, `~/.config/systemd/user/caddy.service`,
   `~/.local/state/caddy/`. Use `systemctl --user` for everything.
4. To replicate on a fresh proxy host: read this file top-to-bottom,
   then `design/deploy-proxy-on-host.md` for the abstract version of
   the same playbook.

## Cross-refs

- `design/deploy-proxy-on-host.md` — the abstract playbook this session
  followed (Step 3b Caddy branch).
- `design/reverse-proxy-config.md` — Caddy snippet bank; the SSE block
  in the Caddyfile above is a direct port.
- `deploy/agent-me-dashboard.service` — backend systemd unit on
  Colossus; defines the `0.0.0.0:8765` bind and
  `DASHBOARD_TRUST_NETWORK=1` env that this proxy depends on.
- `discussions/2026-05-11-reverse-proxy-pivot.md` — backend-side pivot
  (Tailscale Funnel → reverse proxy) that created the
  `DASHBOARD_TRUST_NETWORK` + `X-Dashboard-Auth` header surface this
  proxy verifies in step 4.3 above.
- `discussions/2026-05-11-end-to-end-deploy-walkthrough.md` —
  predecessor "what the docs landed" entry; this discussion is its
  follow-up "what actually got deployed on the proxy host".
