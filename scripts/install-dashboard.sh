#!/usr/bin/env bash
# scripts/install-dashboard.sh — install the Phase 4 dashboard.
#
# Default mode: behind a reverse proxy you (or a colleague) operate. The
# canonical NVIDIA-internal deployment serves the dashboard at
# `https://agent-me.nvidia.com` (VPN-gated); the proxy terminates TLS
# and forwards X-Forwarded-* headers to this host on port 8765.
# Reverse-proxy config snippets live in `design/reverse-proxy-config.md`
# (nginx / caddy / traefik); send those to whoever operates the proxy.
#
# Opt-in mode: pass `--tailscale` to also publish the same backend via
# Tailscale Funnel (`*.<tailnet>.ts.net`). Useful if you want a public
# URL without a reverse proxy. Off by default — most operators don't
# need both.
#
# Idempotent: re-running re-copies unit files, re-enables, restarts.
#
# What it sets up (USER-scope systemd units):
#   ~/.config/systemd/user/agent-me-dashboard.service          (always)
#   ~/.config/systemd/user/agent-me-funnel.service             (only with --tailscale)
#
# Usage:
#   ./scripts/install-dashboard.sh                # reverse-proxy mode (default)
#   ./scripts/install-dashboard.sh --tailscale    # also enable Tailscale Funnel
#   ./scripts/install-dashboard.sh --token        # also generate DASHBOARD_TOKEN
#                                                 # (defense-in-depth on top of VPN)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
ENV_FILE="$REPO_ROOT/configs/.env"

INSTALL_TAILSCALE=0
GENERATE_TOKEN=0
for arg in "$@"; do
    case "$arg" in
        --tailscale) INSTALL_TAILSCALE=1 ;;
        --token) GENERATE_TOKEN=1 ;;
        --help|-h)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *) printf "Unknown flag: %s\n  See --help.\n" "$arg" >&2; exit 2 ;;
    esac
done

bold()   { printf "\033[1m%s\033[0m\n" "$1"; }
ok()     { printf "\033[32m✓ %s\033[0m\n" "$1"; }
warn()   { printf "\033[33m⚠️  %s\033[0m\n" "$1"; }
fail()   { printf "\033[31m❌ %s\033[0m\n" "$1" >&2; exit 1; }

# ── Pre-flight ─────────────────────────────────────────────────────────
if ! command -v systemctl >/dev/null 2>&1; then
    fail "systemctl not found — Phase 4 dashboard requires a systemd Linux host."
fi
if [[ ! -d "$REPO_ROOT/deploy" ]]; then
    fail "deploy/ directory not found — are you running from the repo root?"
fi
if ! command -v uv >/dev/null 2>&1; then
    fail "uv not found. Run ./scripts/bootstrap.sh first."
fi
if [[ ! -f "$ENV_FILE" ]]; then
    fail "configs/.env not found. Run bootstrap.sh + apply secrets first."
fi

# ── Step 0 — uv sync to pull the dashboard deps ────────────────────────
bold "Step 0 — sync Python deps (starlette, uvicorn, jinja2, …)"
( cd "$REPO_ROOT" && uv sync )
ok "uv sync done"

# ── Step 1 — env: DASHBOARD_TRUST_NETWORK + optional token ─────────────
bold "Step 1 — auth model in configs/.env"

# DASHBOARD_TRUST_NETWORK=1 is what tells the dashboard entry point that
# a non-loopback bind is OK (because upstream gates access via VPN +
# reverse proxy). Idempotent: only append if missing.
if grep -q "^DASHBOARD_TRUST_NETWORK=" "$ENV_FILE"; then
    ok "DASHBOARD_TRUST_NETWORK already configured in configs/.env"
else
    {
        echo
        echo "# Phase 4 dashboard — VPN/reverse-proxy gates access; allow non-loopback bind."
        echo "DASHBOARD_TRUST_NETWORK=1"
    } >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "DASHBOARD_TRUST_NETWORK=1 added to configs/.env"
fi

if [[ "$GENERATE_TOKEN" -eq 1 ]]; then
    if grep -q "^DASHBOARD_TOKEN=" "$ENV_FILE"; then
        ok "DASHBOARD_TOKEN already set"
    else
        TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
        {
            echo "DASHBOARD_TOKEN=$TOKEN"
        } >> "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        ok "DASHBOARD_TOKEN generated (defense-in-depth on top of VPN)"
        bold "▶ Save this token in ~/agent-me-secrets.md so you can revisit:"
        echo
        echo "    DASHBOARD_TOKEN=$TOKEN"
        echo
    fi
else
    ok "DASHBOARD_TOKEN intentionally not generated (VPN-only). Re-run with --token to add."
fi

# ── Step 2 — install dashboard unit ────────────────────────────────────
bold "Step 2 — install systemd unit (dashboard)"
mkdir -p "$UNIT_DIR"
cp "$REPO_ROOT/deploy/agent-me-dashboard.service" "$UNIT_DIR/agent-me-dashboard.service"
ok "agent-me-dashboard.service copied to $UNIT_DIR"

if [[ "$INSTALL_TAILSCALE" -eq 1 ]]; then
    cp "$REPO_ROOT/deploy/agent-me-funnel.service" "$UNIT_DIR/agent-me-funnel.service"
    ok "agent-me-funnel.service copied to $UNIT_DIR"
fi

systemctl --user daemon-reload
ok "systemd --user reloaded"

# ── Step 3 — Tailscale (opt-in) ───────────────────────────────────────
if [[ "$INSTALL_TAILSCALE" -eq 1 ]]; then
    bold "Step 3 — Tailscale daemon + funnel (opt-in)"
    if command -v tailscale >/dev/null 2>&1; then
        ok "tailscale already installed ($(tailscale version | head -1))"
    else
        warn "tailscale not installed; running official installer (needs sudo)"
        if ! curl -fsSL https://tailscale.com/install.sh | sudo bash; then
            fail "tailscale install failed. Manual fallback: see https://tailscale.com/download"
        fi
        ok "tailscale installed"
    fi

    if tailscale status >/dev/null 2>&1; then
        ok "tailscaled is authenticated"
    else
        warn "Not authenticated. Running 'sudo tailscale up' — a URL will print."
        sudo tailscale up
        ok "tailscale authenticated"
    fi

    sudo tailscale set --operator="$USER" 2>/dev/null \
        || warn "couldn't set tailscale operator; funnel may need sudo"

    if tailscale funnel status 2>/dev/null | grep -q "https://"; then
        ok "funnel already configured"
    else
        warn "configuring funnel for http://127.0.0.1:8765"
        tailscale funnel --bg --https=443 --set-path=/ http://127.0.0.1:8765 \
            || fail "funnel config failed (Funnel disabled for this tailnet?)"
        ok "funnel configured (persistent)"
    fi
fi

# ── Step 4 — start dashboard ──────────────────────────────────────────
bold "Step 4 — start dashboard"
systemctl --user enable agent-me-dashboard.service 2>&1 | grep -v "already enabled" || true
if systemctl --user is-active --quiet agent-me-dashboard.service; then
    systemctl --user restart agent-me-dashboard.service
    ok "agent-me-dashboard restarted"
else
    systemctl --user start agent-me-dashboard.service
    ok "agent-me-dashboard started"
fi

if [[ "$INSTALL_TAILSCALE" -eq 1 ]]; then
    systemctl --user enable agent-me-funnel.service 2>&1 | grep -v "already enabled" || true
    if systemctl --user is-active --quiet agent-me-funnel.service; then
        systemctl --user restart agent-me-funnel.service
        ok "agent-me-funnel restarted"
    else
        systemctl --user start agent-me-funnel.service
        ok "agent-me-funnel started"
    fi
fi

# ── Step 5 — health check ─────────────────────────────────────────────
sleep 2
if curl -fsSL --connect-timeout 3 http://127.0.0.1:8765/healthz >/dev/null 2>&1; then
    ok "dashboard responding on 127.0.0.1:8765/healthz"
else
    warn "dashboard not yet healthy on :8765. Check logs:"
    warn "  journalctl --user -u agent-me-dashboard -n 50 --no-pager"
fi

# ── Step 6 — print URL + next steps ───────────────────────────────────
echo
PUBLIC_URL_PROXY="https://agent-me.nvidia.com"
PUBLIC_URL_TAILSCALE=""
if [[ "$INSTALL_TAILSCALE" -eq 1 ]]; then
    if FN_STATUS="$(tailscale funnel status 2>/dev/null)"; then
        PUBLIC_URL_TAILSCALE="$(echo "$FN_STATUS" | grep -oE 'https://[a-z0-9.-]+\.ts\.net[^ ]*' | head -1)"
    fi
fi

cat <<EOF
─────────────────────────────────────────────────────────────────────────
Phase 4 dashboard installed.

Primary URL  :  $PUBLIC_URL_PROXY
                (NVIDIA-internal reverse proxy → this host:8765;
                 requires NVIDIA VPN. Configured by your proxy admin.
                 If you operate that proxy, see
                 design/reverse-proxy-config.md for nginx/caddy/traefik snippets.)

EOF

if [[ "$INSTALL_TAILSCALE" -eq 1 ]]; then
cat <<EOF
Backup URL   :  ${PUBLIC_URL_TAILSCALE:-<run 'tailscale funnel status' to see>}
                (Tailscale Funnel — useful when off NVIDIA VPN.)

EOF
fi

cat <<EOF
Local check  :  curl http://127.0.0.1:8765/healthz

Useful commands:

  systemctl --user status agent-me-dashboard
  journalctl --user -u agent-me-dashboard -f
$( [[ "$INSTALL_TAILSCALE" -eq 1 ]] && cat <<NESTED
  systemctl --user status agent-me-funnel
  tailscale funnel status
NESTED
)

To remove:
  systemctl --user stop agent-me-dashboard $( [[ "$INSTALL_TAILSCALE" -eq 1 ]] && echo "agent-me-funnel" )
  systemctl --user disable agent-me-dashboard $( [[ "$INSTALL_TAILSCALE" -eq 1 ]] && echo "agent-me-funnel" )
  rm ~/.config/systemd/user/agent-me-dashboard.service
$( [[ "$INSTALL_TAILSCALE" -eq 1 ]] && echo "  rm ~/.config/systemd/user/agent-me-funnel.service" )
$( [[ "$INSTALL_TAILSCALE" -eq 1 ]] && echo "  tailscale funnel reset" )

The Slack bridge (agent-me-bridge.service) is unaffected.
─────────────────────────────────────────────────────────────────────────
EOF
