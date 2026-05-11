# Reverse-proxy config — `agent-me.nvidia.com`

This document is for whoever operates the **NVIDIA-internal reverse
proxy** that serves `agent-me.nvidia.com`. It contains drop-in config
snippets (nginx / caddy / traefik) plus the specific behaviours the
backend needs upstream to honour.

The dashboard runs on Colossus (or any internal-NVIDIA systemd Linux
host) at **port 8765** over plain HTTP. The proxy terminates TLS and
forwards everything to that origin. No Slack-side changes — the
bridge talks to Slack via outbound Socket Mode regardless.

## What the backend needs from the proxy

| Requirement | Why |
|---|---|
| **Forward all paths** (`/`, `/source/*`, `/ops`, `/logs`, `/api/**`, `/static/*`) | The dashboard is monolithic — Jinja-rendered HTML and JSON API live on the same origin. |
| **Allow `text/event-stream`** responses on `/api/sse/**` to stream without buffering | SSE is the live channel for log tails, brief refresh progress, and session traces. Buffering breaks the live UX. |
| **Long read timeout** for `/api/sse/**` (≥ 1 hour) | SSE connections stay open for the duration the operator's tab is open. |
| **Forward `X-Forwarded-Proto`, `X-Forwarded-Host`, `X-Forwarded-For`** | Starlette's `request.url` reads these to construct the canonical URL (cookies need `Secure=true` to know the upstream is HTTPS). |
| **Don't strip `Cookie` / `Set-Cookie`** | Browser-based session cookie flow. |
| **Don't strip `Authorization`** (if `DASHBOARD_TOKEN` defense-in-depth is enabled) | Bearer token auth path used by API clients. |
| **Health-check `GET /healthz`** is unauthenticated and returns JSON | Use it for upstream liveness if your proxy supports it. |
| **Buffer size for normal `POST /api/**`** ≥ 64 KB | Approval payloads + brief result blobs occasionally hit ~16-32 KB. |

## Network architecture

```
                   ┌─────────────────────────────┐
                   │  NVIDIA employee (VPN'd)    │
                   │  https://agent-me.nvidia.com│
                   └──────────────┬──────────────┘
                                  │ TLS
                                  ▼
                   ┌─────────────────────────────┐
                   │  Reverse proxy              │
                   │  (this doc's snippets)      │
                   │  agent-me.nvidia.com        │
                   └──────────────┬──────────────┘
                                  │ HTTP, plain
                                  │ X-Forwarded-* set
                                  ▼
                   ┌─────────────────────────────┐
                   │  Colossus internal host     │
                   │  127.0.0.1 / 0.0.0.0:8765   │
                   │  agent-me-dashboard.service │
                   └─────────────────────────────┘
```

## nginx snippet

```nginx
# /etc/nginx/conf.d/agent-me.conf
# Adjust `proxy_pass` target to reach the dashboard host.

upstream agent_me_dashboard {
    # If the proxy and dashboard are on the same host, this is fine.
    # If the dashboard is on a different host (e.g. Colossus), put its
    # IP / hostname here. Plain HTTP is intentional — TLS terminates
    # at this proxy.
    server 1xa100-40.example.nvidia.com:8765;

    # Long keepalive so SSE doesn't churn TCP connections.
    keepalive 16;
    keepalive_timeout 75s;
}

server {
    listen 443 ssl http2;
    server_name agent-me.nvidia.com;

    # ── TLS — use whichever cert source NVIDIA infra publishes ─────
    ssl_certificate     /etc/ssl/certs/agent-me.nvidia.com.crt;
    ssl_certificate_key /etc/ssl/private/agent-me.nvidia.com.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # ── HSTS — only after you're sure the cert is stable. ──────────
    # add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # ── Buffer / size limits ───────────────────────────────────────
    client_max_body_size 4m;            # approval payloads + brief blobs
    proxy_buffer_size    32k;
    proxy_buffers        16 32k;
    proxy_busy_buffers_size 64k;

    # ── SSE endpoints — disable buffering, long timeout ────────────
    location ~ ^/api/sse/ {
        proxy_pass http://agent_me_dashboard;

        proxy_http_version 1.1;
        proxy_buffering off;            # critical: don't buffer SSE
        proxy_cache off;
        proxy_request_buffering off;
        chunked_transfer_encoding on;

        proxy_read_timeout    3600s;    # 1h; SSE keeps connection open
        proxy_send_timeout    3600s;
        proxy_connect_timeout 5s;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;

        # The dashboard sets up an EventSource per pane; let it through
        # without re-encoding.
        proxy_set_header Connection "";
    }

    # ── Everything else — normal proxy semantics ───────────────────
    location / {
        proxy_pass http://agent_me_dashboard;

        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_set_header Upgrade           $http_upgrade;   # forward-compat for ws
        proxy_set_header Connection        $connection_upgrade;

        proxy_connect_timeout 5s;
        proxy_read_timeout    60s;
        proxy_send_timeout    60s;
    }

    # ── Optional: pin /healthz at the proxy without forwarding ─────
    # location = /healthz { return 200 "ok\n"; add_header Content-Type text/plain; }
}

# Required for the `Upgrade` header dance above:
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

## Caddy snippet

```caddy
# /etc/caddy/Caddyfile fragment

agent-me.nvidia.com {
    # Caddy auto-issues / renews via internal CA + DNS-01 if available.
    # Replace with `tls /path/cert /path/key` for explicit certs.

    @sse path /api/sse/*
    handle @sse {
        reverse_proxy 1xa100-40.example.nvidia.com:8765 {
            flush_interval -1            # critical: stream tokens, no buffer
            transport http {
                read_timeout 1h
                write_timeout 1h
            }
        }
    }

    # Catch-all for HTML pages + non-SSE JSON API
    handle {
        reverse_proxy 1xa100-40.example.nvidia.com:8765 {
            transport http {
                read_timeout 60s
                write_timeout 60s
            }
        }
    }

    # Always set X-Forwarded-* — Caddy does this by default but pin it.
    request_header X-Forwarded-Proto {scheme}
    request_header X-Forwarded-Host  {host}

    log {
        output file /var/log/caddy/agent-me.access.log
    }
}
```

## Traefik snippet

```yaml
# traefik dynamic config (file provider) or labels equivalent.

http:
  routers:
    agent-me:
      rule: "Host(`agent-me.nvidia.com`)"
      entryPoints: [websecure]
      service: agent-me
      tls:
        certResolver: nvidia-internal-ca
      middlewares: [agent-me-headers]

    # Separate router for SSE so we can attach the no-buffer middleware
    agent-me-sse:
      rule: "Host(`agent-me.nvidia.com`) && PathPrefix(`/api/sse`)"
      entryPoints: [websecure]
      service: agent-me
      tls:
        certResolver: nvidia-internal-ca
      middlewares: [agent-me-headers, agent-me-sse-passthrough]
      priority: 100  # win the route over the catch-all

  services:
    agent-me:
      loadBalancer:
        servers:
          - url: "http://1xa100-40.example.nvidia.com:8765"
        passHostHeader: true
        responseForwarding:
          # critical for SSE: don't accumulate chunks before flushing
          flushInterval: -1ms

  middlewares:
    agent-me-headers:
      headers:
        customRequestHeaders:
          X-Forwarded-Proto: "https"
        # Traefik already forwards X-Forwarded-For / -Host by default.

    agent-me-sse-passthrough:
      buffering:
        # Streaming responses — don't accumulate
        retryExpression: "IsNetworkError() && Attempts() < 1"
        maxResponseBodyBytes: 0      # 0 = unlimited, no buffer
```

## Sanity checklist for the proxy admin

After deploying, run these against `https://agent-me.nvidia.com` from
an NVIDIA-VPN'd machine:

```bash
# 1. Liveness — should be { "ok": true, "uptime_s": ... }
curl -sSL https://agent-me.nvidia.com/healthz | jq .

# 2. Headers — should set `X-Dashboard-Auth: trust-network` (or `cookie`/
#    `bearer` if DASHBOARD_TOKEN is also set). If you see `disabled`, the
#    backend is missing DASHBOARD_TRUST_NETWORK=1 in its env.
curl -sSL -I https://agent-me.nvidia.com/

# 3. SSE flush — should drip lines as they happen, not block until close.
#    `--no-buffer` on curl shows you what the browser would see.
curl -sSL --no-buffer https://agent-me.nvidia.com/api/sse/logs/watcher \
    --max-time 5

# 4. Static assets cache headers (sanity, not critical)
curl -sSL -I https://agent-me.nvidia.com/static/app.css
```

## Auth model (with the reverse proxy in place)

The default install uses VPN as the trust boundary:

- `DASHBOARD_TRUST_NETWORK=1` in `configs/.env` → backend permits the
  non-loopback bind without a token.
- The `/healthz` route is exempt from auth (always reachable).
- The dashboard middleware injects `X-Dashboard-Auth: trust-network`
  on every response so you can confirm the model is active.

For defense-in-depth, run `./scripts/install-dashboard.sh --token` to
also generate a `DASHBOARD_TOKEN`. Then any request still needs either
the bearer header or the signed cookie set via `?t=<token>` once.
Useful when multiple NVIDIA employees can VPN but only one operator
should see the dashboard.

## Things deliberately out of scope of this doc

- ACLs / IP allow-lists at the proxy. If your deployment needs per-LDAP
  group access, configure that in the proxy itself (e.g. nginx
  `auth_request`, Traefik forward-auth, Caddy oauth). The backend
  doesn't currently inspect any SSO header.
- WebSocket upgrade for future bridge channels — the snippets above
  forward `Upgrade` / `Connection` headers, but no current endpoint
  uses WS. Document the intent here so it doesn't get lost.
- TLS certificate procurement. Use whatever NVIDIA-internal CA + DNS
  process you already use for `*.nvidia.com` services.
- IPv6 listeners (add `listen [::]:443 ssl` if needed; the backend
  doesn't care).
