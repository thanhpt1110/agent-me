#!/usr/bin/env bash
# scripts/install-systemd.sh — copy deploy/*.service into the user's systemd
#                               unit directory, enable + start them.
#
# Idempotent: re-running re-copies the unit files and re-enables. Use after
# pulling new versions of the units (in practice rare — they don't change much).
#
# Run from repo root, after `bootstrap.sh` and after configs/.env is filled:
#   ./scripts/install-systemd.sh
#
# What it sets up (USER scope, so no sudo for normal operation):
#   ~/.config/systemd/user/agent-me-bridge.service
#   ~/.config/systemd/user/agent-me-watch.service
#
# After this:
#   systemctl --user status agent-me-bridge
#   systemctl --user status agent-me-watch
#   journalctl --user -u agent-me-bridge -f
#
# To stop:    systemctl --user stop  agent-me-bridge agent-me-watch
# To disable: systemctl --user disable agent-me-bridge agent-me-watch
# To remove:  rm ~/.config/systemd/user/agent-me-{bridge,watch}.service

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "\033[32m✓ %s\033[0m\n" "$1"; }
warn() { printf "\033[33m⚠️  %s\033[0m\n" "$1"; }
fail() { printf "\033[31m❌ %s\033[0m\n" "$1" >&2; exit 1; }

# ── Pre-flight ─────────────────────────────────────────────────────────
if ! command -v systemctl >/dev/null 2>&1; then
    fail "systemctl not found — this host doesn't have systemd. Install on a systemd-based Linux distro (Ubuntu / Debian / RHEL / Fedora)."
fi

if [[ ! -d "$REPO_ROOT/deploy" ]]; then
    fail "deploy/ directory not found at $REPO_ROOT/deploy — are you running from the repo root?"
fi

if [[ ! -f "$REPO_ROOT/configs/.env" ]]; then
    warn "configs/.env not found — bridge will fail to start until you upload it. Continuing install (units enabled, bridge will restart-loop until .env exists)."
fi

if ! command -v uv >/dev/null 2>&1; then
    fail "uv not found in PATH. Run ./scripts/bootstrap.sh first."
fi

# ── Linger ─────────────────────────────────────────────────────────────
# Without `loginctl enable-linger`, user services exit when the SSH session
# that started them logs out. With linger, systemd keeps user-service slots
# alive across logins. Required for any "always-on after I disconnect" setup.
bold "Step 1 — enable linger for $USER"
if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    ok "linger already enabled for $USER"
else
    if sudo loginctl enable-linger "$USER" 2>/dev/null; then
        ok "linger enabled for $USER"
    else
        warn "couldn't enable-linger (sudo failed?) — services may stop on logout. Continue, but note: bridge will die when this SSH session ends until you fix this."
    fi
fi

# ── Copy units ─────────────────────────────────────────────────────────
bold "Step 2 — install unit files into $UNIT_DIR"
mkdir -p "$UNIT_DIR"
for unit in agent-me-bridge.service agent-me-watch.service; do
    cp "$REPO_ROOT/deploy/$unit" "$UNIT_DIR/$unit"
    ok "$unit"
done

# ── Reload systemd ─────────────────────────────────────────────────────
bold "Step 3 — daemon-reload"
systemctl --user daemon-reload
ok "systemd --user reloaded"

# ── Enable + start ─────────────────────────────────────────────────────
bold "Step 4 — enable + (re)start services"
for unit in agent-me-bridge.service agent-me-watch.service; do
    systemctl --user enable "$unit" 2>&1 | grep -v "already enabled" || true
    if systemctl --user is-active --quiet "$unit"; then
        systemctl --user restart "$unit"
        ok "$unit (restarted)"
    else
        systemctl --user start "$unit"
        ok "$unit (started)"
    fi
done

# ── Status ─────────────────────────────────────────────────────────────
echo
bold "Step 5 — current status"
systemctl --user status agent-me-bridge.service agent-me-watch.service --no-pager --lines=5 || true

cat <<EOF

─────────────────────────────────────────────────────────────────────────
Installed. Useful commands:

  systemctl --user status  agent-me-bridge agent-me-watch
  systemctl --user restart agent-me-bridge
  systemctl --user stop    agent-me-bridge agent-me-watch
  journalctl --user -u agent-me-bridge -f       # live tail bridge
  journalctl --user -u agent-me-watch  -f       # live tail watcher
  tail -F ~/.local/state/agent-me/bridge.log    # also captured to file

The watcher polls origin/main every 60s. To change interval:
  systemctl --user edit agent-me-watch
  # add:  [Service]  Environment=AGENT_ME_WATCH_INTERVAL_S=30

To force an immediate pull (skip waiting for next cycle):
  cd ~/agent-me && git pull && uv sync && systemctl --user restart agent-me-bridge
─────────────────────────────────────────────────────────────────────────
EOF
