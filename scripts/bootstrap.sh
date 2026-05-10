#!/usr/bin/env bash
# scripts/bootstrap.sh — first-time setup on a fresh host (e.g. Brev).
#
# Run from the repo root after `git clone`:
#   ./scripts/bootstrap.sh
#
# Idempotent — safe to re-run after partial failure.
#
# Steps:
#   1. Verify prerequisites (claude, uv, gh, jq) — error if missing
#   2. Install Python deps via `uv sync`
#   3. Verify configs/.env exists (or copy from .env.example with warning)
#   4. Register all MaaS MCP servers via scripts/setup-mcps.sh
#   5. Print next-step instructions for the interactive parts:
#        - claude /login (one-time, per-machine)
#        - uv run agent-me-reauth (interactive OAuth tabs)
#        - source configs/.env + uv run agent-me-bridge

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
warn() { printf "\033[33m⚠️  %s\033[0m\n" "$1"; }
ok()   { printf "\033[32m✓ %s\033[0m\n" "$1"; }
fail() { printf "\033[31m❌ %s\033[0m\n" "$1" >&2; exit 1; }

# ── 1. prerequisites ───────────────────────────────────────────────────
bold "Step 1 — prerequisites"
need_install=()
for cmd in claude uv gh jq; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        need_install+=("$cmd")
    else
        ok "$cmd: $(command -v "$cmd")"
    fi
done
if [[ ${#need_install[@]} -gt 0 ]]; then
    echo
    fail "Missing tools: ${need_install[*]}

Install hints:
  claude   → npm install -g @anthropic-ai/claude-code
  uv       → curl -LsSf https://astral.sh/uv/install.sh | sh
  gh       → brew install gh   (Mac) | apt install gh (Debian) | dnf install gh (RHEL)
  jq       → brew install jq   (Mac) | apt install jq (Debian) | dnf install jq (RHEL)

After installing, re-run: ./scripts/bootstrap.sh"
fi

# ── 2. python deps ─────────────────────────────────────────────────────
echo
bold "Step 2 — Python dependencies (uv sync)"
uv sync
ok "uv sync complete (.venv populated)"

# ── 3. configs/.env ────────────────────────────────────────────────────
echo
bold "Step 3 — configs/.env"
if [[ ! -f configs/.env ]]; then
    cp configs/.env.example configs/.env
    warn "Created configs/.env from template — fill in real Slack tokens before running the bridge."
    warn "  Edit:    \$EDITOR configs/.env"
    warn "  Or restore from secrets vault:  scp <vault-host>:~/agent-me-secrets.md ~/"
    warn "Bridge will refuse to start until SLACK_BOT_TOKEN is set."
else
    ok "configs/.env exists"
    if grep -q "REPLACE-ME" configs/.env; then
        warn "configs/.env still has REPLACE-ME placeholders — bridge will refuse to start until filled."
    fi
fi

# ── 4. register MCP servers ────────────────────────────────────────────
echo
bold "Step 4 — register MaaS MCP servers"
bash "$REPO_ROOT/scripts/setup-mcps.sh"

# ── 5. interactive next steps ──────────────────────────────────────────
echo
bold "Step 5 — what to do next (interactive)"
cat <<'NEXT'
The remaining steps need a human at a browser:

  a) Log in to Claude Code (one-time per machine):
        claude /login
     (or set ANTHROPIC_API_KEY for headless deploys)

  b) Authenticate every MaaS MCP server (NVIDIA SSO):
        uv run agent-me-reauth
     The helper auto-opens browser tabs for each stale server. Sign in
     to NVIDIA SSO; tokens persist ~24h.

  c) Verify everything is healthy:
        claude mcp list                       # should show all ✓ Connected
        uv run agent-me-brief --period day --dry-run

  d) Start the bridge (foreground, Ctrl-C to stop):
        uv run agent-me-bridge

For 24/7 deploy on Brev, also see design/setup-on-fresh-host.md.
NEXT
