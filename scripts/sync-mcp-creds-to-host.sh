#!/usr/bin/env bash
# scripts/sync-mcp-creds-to-host.sh — copy MCP OAuth tokens from the local
#                                      Mac's Keychain to a remote host's
#                                      ~/.claude/.credentials.json.
#
# Usage:
#   ./scripts/sync-mcp-creds-to-host.sh <ssh-alias-or-host>
#
# What it does (Mac-only, by design):
# 1. Extracts the Keychain item `Claude Code-credentials` (plain JSON,
#    {"mcpOAuth": {...}}) — Mac may pop a permission dialog the first
#    time; click "Always Allow".
# 2. Backs up the host's existing ~/.claude/.credentials.json to
#    ~/.claude/.credentials.json.bak.<ts>.
# 3. jq-merges the host's `claudeAiOauth` (host-side `claude /login`
#    state) with the Mac's `mcpOAuth` map.
# 4. Writes the merged file back via scp + chmod 600.
# 5. Writes Codex bearer-token env exports on the host and installs shell
#    startup hooks so future Codex sessions inherit refreshed MCP auth.
# 6. Verifies with `claude mcp list` on the host and prints the count
#    of ✓ Connected vs Needs auth maas-* servers.
#
# Re-run any time the Mac has fresher tokens (e.g. you just reauth'd a
# server there). Each run is idempotent — refreshing already-valid
# tokens just rewrites the same data.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <ssh-alias-or-host>" >&2
    exit 2
fi
HOST="$1"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "\033[32m✓ %s\033[0m\n" "$1"; }
warn() { printf "\033[33m⚠️  %s\033[0m\n" "$1"; }
fail() { printf "\033[31m❌ %s\033[0m\n" "$1" >&2; exit 1; }

# ── Pre-flight ─────────────────────────────────────────────────────────
[[ "$(uname -s)" == "Darwin" ]] || fail "this script extracts credentials from the macOS Keychain — run on your Mac, not on the host"
command -v security >/dev/null || fail "security CLI not found (expected on macOS)"
command -v jq >/dev/null       || fail "jq not found (brew install jq)"
command -v ssh >/dev/null      || fail "ssh not found"
command -v scp >/dev/null      || fail "scp not found"

# ── 1. Extract Mac Keychain blob ───────────────────────────────────────
bold "Step 1 — extract MCP OAuth blob from Keychain"
TMP_MAC=$(mktemp -t mac-mcp-creds)
trap 'rm -f "$TMP_MAC" "$TMP_MERGED"' EXIT
TMP_MERGED=$(mktemp -t merged-creds)

# `security ... -w` prints the password value (the JSON blob) to stdout.
# May trigger a Keychain permission dialog on first call.
if ! security find-generic-password -s 'Claude Code-credentials' -w > "$TMP_MAC" 2>/dev/null; then
    fail "couldn't read 'Claude Code-credentials' from Keychain. If you saw a permission dialog, click \"Always Allow\" and retry. If you've never authenticated MCPs in Claude Code on this Mac, there's nothing to sync — do that first."
fi

# Sanity check: should be JSON with `mcpOAuth` top-level key.
if ! jq -e '.mcpOAuth // empty' "$TMP_MAC" >/dev/null 2>&1; then
    fail "Keychain blob has no 'mcpOAuth' field. The format may have changed in a Claude Code update — open an issue. Blob length: $(wc -c < "$TMP_MAC")B."
fi

mac_count=$(jq '.mcpOAuth | length' "$TMP_MAC")
ok "extracted $mac_count MCP OAuth entries from Keychain (blob: $(wc -c < "$TMP_MAC")B)"

# ── 2. Pull host's existing credentials ────────────────────────────────
bold "Step 2 — fetch host's current ~/.claude/.credentials.json"
TMP_HOST=$(mktemp -t host-creds)
if ! ssh "$HOST" 'cat ~/.claude/.credentials.json 2>/dev/null' > "$TMP_HOST"; then
    fail "ssh $HOST failed — check your SSH config / connectivity"
fi
if [[ ! -s "$TMP_HOST" ]]; then
    warn "host has no existing ~/.claude/.credentials.json (or it's empty). Did you run 'claude /login' on the host? Continuing anyway — Anthropic auth will be empty in the merged file."
    echo '{}' > "$TMP_HOST"
fi
ok "host credentials fetched ($(wc -c < "$TMP_HOST")B)"

# Backup on the host before overwriting.
ssh "$HOST" '
    cp ~/.claude/.credentials.json ~/.claude/.credentials.json.bak.$(date -u +%Y%m%dT%H%M%SZ) 2>/dev/null || true
' && ok "host backup created (.credentials.json.bak.<ts>)"

# ── 3. Merge ───────────────────────────────────────────────────────────
bold "Step 3 — merge (preserve host's claudeAiOauth, overlay mcpOAuth)"
# Right-side wins for overlapping keys, so the Mac's mcpOAuth always
# replaces the host's older one. Anthropic OAuth on the host stays put.
jq -s '.[0] * .[1]' "$TMP_HOST" "$TMP_MAC" > "$TMP_MERGED"

merged_keys=$(jq -r 'keys | join(", ")' "$TMP_MERGED")
ok "merged file has top-level keys: $merged_keys"

# ── 4. Push to host ────────────────────────────────────────────────────
bold "Step 4 — push merged file to host"
scp -q "$TMP_MERGED" "${HOST}:~/.claude/.credentials.json"
ssh "$HOST" 'chmod 600 ~/.claude/.credentials.json'
ok "pushed and chmod'd 600"

# ── 5. Prepare Codex auth env ──────────────────────────────────────────
bold "Step 5 — prepare Codex MCP auth env on host"
if [[ ! -r "$SCRIPT_DIR/install-codex-mcp-env-on-host.sh" ]]; then
    fail "missing helper: $SCRIPT_DIR/install-codex-mcp-env-on-host.sh"
fi
ssh "$HOST" 'bash -s' < "$SCRIPT_DIR/install-codex-mcp-env-on-host.sh"

codex_env_count=$(ssh "$HOST" 'bash -lc ". ~/.config/agent-me/codex-mcp-env.sh 2>/dev/null; env | grep -c \"^AGENT_ME_MCP_TOKEN_\""' 2>/dev/null || true)
echo "  Codex env exports: ${codex_env_count:-0} AGENT_ME_MCP_TOKEN_*"

# ── 6. Verify ──────────────────────────────────────────────────────────
bold "Step 6 — verify with claude mcp list on host"
mcp_out=$(ssh "$HOST" '~/.local/bin/uv run --quiet --no-sync --directory ~/agent-me python -c "import subprocess; print(subprocess.run([\"claude\",\"mcp\",\"list\"],capture_output=True,text=True).stdout)"' 2>&1 || \
          ssh "$HOST" 'export PATH=$HOME/.local/bin:$PATH; claude mcp list' 2>&1)
connected=$(printf '%s\n' "$mcp_out" | grep -cE "^maas-.*✓ Connected" || true)
need_auth=$(printf '%s\n' "$mcp_out" | grep -cE "^maas-.*Needs authentication" || true)
echo "  ✓ Connected:        $connected maas-*"
echo "  ! Needs auth:       $need_auth maas-*"
if [[ "$need_auth" -gt 0 ]]; then
    echo
    printf '%s\n' "$mcp_out" | grep -E "^maas-.*Needs authentication" || true
    echo
    warn "Some maas-* still need auth on the host. Either (a) reauth them on the Mac first then re-run this script, or (b) use the SSH-port-forward + agent-me-reauth path on the host."
fi

bold "Done."
