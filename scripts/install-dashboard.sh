#!/usr/bin/env bash
# scripts/install-dashboard.sh — install the Phase 4 dashboard + Tailscale Funnel.
#
# Idempotent: re-running re-copies unit files, re-enables, and re-runs the
# `tailscale funnel` config. Safe to run multiple times.
#
# What it sets up (USER-scope systemd units):
#   ~/.config/systemd/user/agent-me-dashboard.service
#   ~/.config/systemd/user/agent-me-funnel.service
#
# Public URL after this completes:
#   https://<host>.<your-tailnet>.ts.net
#
# Prerequisites this script handles:
#   - tailscale package install (apt, idempotent)
#   - `tailscale up` if not already authed (interactive — opens browser)
#   - `tailscale set --operator=$USER` so user-level funnel calls work
#   - DASHBOARD_TOKEN generation if missing from configs/.env
#   - Tailscale Funnel feature enabled on this device
#
# What it does NOT handle (by design):
#   - Signing up for a Tailscale account (1 minute, https://login.tailscale.com)
#   - Approving the device on the Tailscale admin console (one-time, browser)
#   - Enabling Funnel in the Tailscale admin if your tailnet has it disabled
#     (default: enabled for personal tailnets)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
ENV_FILE="$REPO_ROOT/configs/.env"

bold()   { printf "\033[1m%s\033[0m\n" "$1"; }
ok()     { printf "\033[32m✓ %s\033[0m\n" "$1"; }
warn()   { printf "\033[33m⚠️  %s\033[0m\n" "$1"; }
fail()   { printf "\033[31m❌ %s\033[0m\n" "$1" >&2; exit 1; }
prompt() { printf "\033[36m? %s\033[0m " "$1"; }

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

# ── Step 0 — uv sync to pull the new dashboard deps ────────────────────
bold "Step 0 — sync Python deps (starlette, uvicorn, jinja2, …)"
( cd "$REPO_ROOT" && uv sync )
ok "uv sync done"

# ── Step 1 — install tailscale if needed ───────────────────────────────
bold "Step 1 — Tailscale daemon"
if command -v tailscale >/dev/null 2>&1; then
    ok "tailscale already installed ($(tailscale version | head -1))"
else
    warn "tailscale not installed; running official installer (needs sudo)"
    if ! curl -fsSL https://tailscale.com/install.sh | sudo bash; then
        fail "tailscale install failed. Manual fallback: see https://tailscale.com/download"
    fi
    ok "tailscale installed"
fi

# ── Step 2 — auth + operator ──────────────────────────────────────────
bold "Step 2 — Tailscale auth"
if tailscale status >/dev/null 2>&1; then
    ok "tailscaled is authenticated ($(tailscale status --self=true --peers=false 2>/dev/null | head -1 || echo on))"
else
    warn "Not authenticated. Running 'sudo tailscale up' — a URL will print."
    warn "Open it in your Mac browser (SSH port-forward already if remote)."
    sudo tailscale up
    ok "tailscale authenticated"
fi

# Set the current user as a tailscale operator so `tailscale funnel`
# (called from a user systemd unit) works without sudo.
if tailscale set --operator="$USER" 2>/dev/null; then
    ok "user $USER set as tailscale operator"
else
    warn "couldn't set operator (may need sudo). Trying with sudo..."
    sudo tailscale set --operator="$USER" || warn "still failed — funnel may need sudo"
fi

# ── Step 3 — DASHBOARD_TOKEN ──────────────────────────────────────────
bold "Step 3 — DASHBOARD_TOKEN"
if [[ ! -f "$ENV_FILE" ]]; then
    fail "configs/.env not found. Run bootstrap.sh + apply secrets first."
fi
if grep -q "^DASHBOARD_TOKEN=" "$ENV_FILE"; then
    ok "DASHBOARD_TOKEN already set in configs/.env"
else
    TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    {
      echo
      echo "# Phase 4 dashboard auth (added by install-dashboard.sh)"
      echo "DASHBOARD_TOKEN=$TOKEN"
    } >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "DASHBOARD_TOKEN generated and added"
    bold ""
    bold "▶ Save this token in ~/agent-me-secrets.md so you can revisit:"
    echo
    echo "    DASHBOARD_TOKEN=$TOKEN"
    echo
fi

# ── Step 4 — install unit files ───────────────────────────────────────
bold "Step 4 — install systemd units"
mkdir -p "$UNIT_DIR"
for unit in agent-me-dashboard.service agent-me-funnel.service; do
    cp "$REPO_ROOT/deploy/$unit" "$UNIT_DIR/$unit"
    ok "$unit copied to $UNIT_DIR"
done

systemctl --user daemon-reload
ok "systemd --user reloaded"

# ── Step 5 — start dashboard ──────────────────────────────────────────
bold "Step 5 — start dashboard"
systemctl --user enable agent-me-dashboard.service 2>&1 | grep -v "already enabled" || true
if systemctl --user is-active --quiet agent-me-dashboard.service; then
    systemctl --user restart agent-me-dashboard.service
    ok "agent-me-dashboard restarted"
else
    systemctl --user start agent-me-dashboard.service
    ok "agent-me-dashboard started"
fi

# Wait briefly for it to bind 127.0.0.1:8765
sleep 2
if curl -fsSL --connect-timeout 3 http://127.0.0.1:8765/healthz >/dev/null 2>&1; then
    ok "dashboard responding on 127.0.0.1:8765/healthz"
else
    warn "dashboard not yet healthy on :8765. Check logs:"
    warn "  journalctl --user -u agent-me-dashboard -n 50 --no-pager"
fi

# ── Step 6 — start funnel ─────────────────────────────────────────────
bold "Step 6 — Tailscale Funnel"
# Persistent funnel config: this is the canonical state after `--bg`.
# We run it persistent so a `systemctl --user restart agent-me-funnel`
# isn't strictly needed, but the unit is a nice inspection point.
if tailscale funnel status 2>/dev/null | grep -q "https://"; then
    ok "funnel already configured ($(tailscale funnel status | grep https | head -1))"
else
    warn "configuring funnel for http://127.0.0.1:8765"
    if tailscale funnel --bg --https=443 --set-path=/ http://127.0.0.1:8765; then
        ok "funnel configured (persistent)"
    else
        fail "funnel config failed. Likely cause: Funnel disabled for this tailnet. Enable at https://login.tailscale.com/admin/settings/funnel"
    fi
fi

systemctl --user enable agent-me-funnel.service 2>&1 | grep -v "already enabled" || true
if systemctl --user is-active --quiet agent-me-funnel.service; then
    systemctl --user restart agent-me-funnel.service
    ok "agent-me-funnel restarted"
else
    systemctl --user start agent-me-funnel.service
    ok "agent-me-funnel started"
fi

# ── Step 7 — print public URL + token reminder ───────────────────────
echo
bold "Step 7 — open the dashboard"
PUBLIC_URL=""
if FN_STATUS="$(tailscale funnel status 2>/dev/null)"; then
    PUBLIC_URL="$(echo "$FN_STATUS" | grep -oE 'https://[a-z0-9.-]+\.ts\.net[^ ]*' | head -1)"
fi
TOKEN_FROM_ENV="$(grep '^DASHBOARD_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"

cat <<EOF

─────────────────────────────────────────────────────────────────────────
Phase 4 dashboard installed.

Public URL  :  ${PUBLIC_URL:-<run 'tailscale funnel status' to see>}
First visit :  append ?t=<token> to set the auth cookie

  Token     :  ${TOKEN_FROM_ENV}

  i.e.      :  ${PUBLIC_URL:-https://<host>.<tailnet>.ts.net}/?t=${TOKEN_FROM_ENV}

Useful commands:

  systemctl --user status agent-me-dashboard agent-me-funnel
  journalctl --user -u agent-me-dashboard -f
  journalctl --user -u agent-me-funnel -f
  tailscale funnel status
  tailscale funnel reset    # take public URL down

To remove:
  systemctl --user stop agent-me-dashboard agent-me-funnel
  systemctl --user disable agent-me-dashboard agent-me-funnel
  rm ~/.config/systemd/user/agent-me-{dashboard,funnel}.service
  tailscale funnel reset

The Slack bridge (agent-me-bridge.service) is unaffected.
─────────────────────────────────────────────────────────────────────────
EOF
