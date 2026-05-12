#!/usr/bin/env bash
# Open the Brev request form with the same persistent Chrome profile used by
# the Codex maas-playwright MCP. Run this only from a real host GUI session
# (for example SSH with X forwarding, remote desktop, or a local desktop).

set -euo pipefail

BREV_FORM_URL="${BREV_FORM_URL:-https://nvidia.tfaforms.net/32}"
STATE_DIR="${AGENT_ME_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-me}"
PROFILE_DIR="${AGENT_ME_PLAYWRIGHT_PROFILE_DIR:-$STATE_DIR/playwright-profile}"
OUTPUT_DIR="${AGENT_ME_PLAYWRIGHT_OUTPUT_DIR:-$STATE_DIR/playwright-output}"
DEBUG_PORT="${BREV_CHROME_DEBUG_PORT:-9333}"

mkdir -p "$PROFILE_DIR" "$OUTPUT_DIR"

chrome_bin="${CHROME_BIN:-}"
if [[ -z "$chrome_bin" ]]; then
    chrome_bin="$(command -v google-chrome-stable || command -v google-chrome || command -v chromium || true)"
fi
if [[ -z "$chrome_bin" ]]; then
    echo "Chrome/Chromium not found. Install it with: npx playwright install chrome" >&2
    exit 2
fi

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    cat >&2 <<EOF
No GUI display is available in this shell.

Run this from a host desktop session or SSH with X forwarding, then sign in
manually in the opened Chrome window. Do not paste passwords into Slack.

Profile that will hold the SSO cookies:
  $PROFILE_DIR
EOF
    exit 3
fi

cat <<EOF
Opening Brev form with persistent host browser profile.

Profile:
  $PROFILE_DIR

After Microsoft/NVIDIA SSO succeeds, close the window and run:
  brev auth

There is no localhost OAuth callback for this Brev web SSO check; cookies stay
inside the host Chrome profile above.
EOF

exec "$chrome_bin" \
    --user-data-dir="$PROFILE_DIR" \
    --no-first-run \
    --no-default-browser-check \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port="$DEBUG_PORT" \
    "$BREV_FORM_URL"
