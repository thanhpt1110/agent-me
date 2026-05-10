#!/usr/bin/env bash
# scripts/agent-me-watch.sh — poll origin/$BRANCH; on new commit, pull,
# uv-sync if pyproject changed, restart agent-me-bridge.
#
# Designed to run under systemd --user as agent-me-watch.service.
# Stand-alone use: just `bash scripts/agent-me-watch.sh` from the repo root.
# Re-runs are safe; the loop is the primary control flow.
#
# Environment overrides (set in the systemd unit or shell):
#   AGENT_ME_WATCH_INTERVAL_S   poll interval, default 60
#   AGENT_ME_WATCH_BRANCH       branch to track, default main
#   AGENT_ME_BRIDGE_UNIT        unit name to restart, default agent-me-bridge
#
# Logs to stdout/stderr (which journald captures under the unit) using
# ISO-8601 timestamps so `journalctl --user -u agent-me-watch -f` reads cleanly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

INTERVAL="${AGENT_ME_WATCH_INTERVAL_S:-60}"
BRANCH="${AGENT_ME_WATCH_BRANCH:-main}"
UNIT="${AGENT_ME_BRIDGE_UNIT:-agent-me-bridge}"

log() { printf '%s [watch] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Graceful shutdown — SIGTERM from systemd should let the current iteration
# complete (or interrupt the sleep) and exit. set -e propagates a failed
# command but we want sleep interruption to be a clean exit.
running=1
trap 'running=0; log "received signal — exiting after current iteration"' TERM INT

log "starting; branch=$BRANCH interval=${INTERVAL}s unit=$UNIT cwd=$REPO_ROOT"

while [[ "$running" -eq 1 ]]; do
    # `git fetch` against an unauthenticated public clone — no creds needed for
    # github.com/thanhpt1110/agent-me. If we ever switch to a private repo,
    # add a deploy key (read-only) at ~/.ssh/agent-me-deploy and remote URL
    # over SSH. NOT a PAT in the URL — that ends up in `git config` cleartext.
    if ! git fetch --quiet origin "$BRANCH" 2>&1; then
        log "fetch failed; will retry next cycle"
        sleep "$INTERVAL" || break
        continue
    fi

    behind=$(git rev-list --count "HEAD..origin/$BRANCH")
    if [[ "$behind" -eq 0 ]]; then
        sleep "$INTERVAL" || break
        continue
    fi

    log "behind by $behind commit(s) — pulling"
    old_sha=$(git rev-parse HEAD)
    if ! git pull --ff-only --quiet origin "$BRANCH"; then
        log "fast-forward pull failed (local state diverged?); skipping"
        sleep "$INTERVAL" || break
        continue
    fi
    new_sha=$(git rev-parse HEAD)
    log "pulled ${old_sha:0:8} → ${new_sha:0:8}"

    # Re-sync python deps only if pyproject.toml or uv.lock changed in the new
    # commits. uv sync is fast on no-op but it's still extra latency on every
    # restart, and we'd rather restart the bridge sooner.
    if git diff --name-only "$old_sha" "$new_sha" | grep -qE '^(pyproject\.toml|uv\.lock)$'; then
        log "pyproject/uv.lock changed — running uv sync"
        if uv sync 2>&1; then
            log "uv sync ok"
        else
            log "uv sync failed — restarting bridge with old venv anyway"
        fi
    fi

    # systemd --user: doesn't need sudo. The bridge unit's Restart=on-failure
    # picks up automatically; restart sends SIGTERM, bridge does its 15s
    # graceful, then comes back with new code.
    if systemctl --user restart "$UNIT"; then
        log "restarted $UNIT"
    else
        log "systemctl restart failed — manual intervention needed"
    fi

    sleep "$INTERVAL" || break
done

log "exiting cleanly"
