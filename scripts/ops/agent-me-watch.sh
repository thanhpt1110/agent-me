#!/usr/bin/env bash
# scripts/agent-me-watch.sh — poll origin/$BRANCH; on new commit, pull,
# uv-sync if pyproject changed, restart agent-me services (bridge +
# dashboard if installed).
#
# Designed to run under systemd --user as agent-me-watch.service.
# Stand-alone use: just `bash scripts/agent-me-watch.sh` from the repo root.
# Re-runs are safe; the loop is the primary control flow.
#
# Environment overrides (set in the systemd unit or shell):
#   AGENT_ME_WATCH_INTERVAL_S   poll interval, default 60
#   AGENT_ME_WATCH_BRANCH       branch to track, default main
#   AGENT_ME_RESTART_UNITS      space-separated list of units to restart;
#                               default: detect bridge + dashboard from
#                               ~/.config/systemd/user/, restart whatever
#                               is installed.
#   AGENT_ME_BRIDGE_UNIT        legacy alias — sets a single-unit restart
#                               list (backward compat with pre-Phase-4
#                               deployments). Ignored if AGENT_ME_RESTART_UNITS
#                               is set.
#
# Logs to stdout/stderr (which journald captures under the unit) using
# ISO-8601 timestamps so `journalctl --user -u agent-me-watch -f` reads cleanly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

INTERVAL="${AGENT_ME_WATCH_INTERVAL_S:-60}"
BRANCH="${AGENT_ME_WATCH_BRANCH:-main}"

# Resolve restart-unit list. Priority:
#   1. AGENT_ME_RESTART_UNITS (explicit list, wins)
#   2. AGENT_ME_BRIDGE_UNIT   (legacy single-unit)
#   3. auto-detect bridge + dashboard from installed unit files
USER_UNIT_DIR="$HOME/.config/systemd/user"
if [[ -n "${AGENT_ME_RESTART_UNITS:-}" ]]; then
    # shellcheck disable=SC2206
    RESTART_UNITS=( ${AGENT_ME_RESTART_UNITS} )
elif [[ -n "${AGENT_ME_BRIDGE_UNIT:-}" ]]; then
    RESTART_UNITS=( "${AGENT_ME_BRIDGE_UNIT}" )
else
    RESTART_UNITS=()
    for candidate in agent-me-bridge agent-me-dashboard; do
        if [[ -f "${USER_UNIT_DIR}/${candidate}.service" ]]; then
            RESTART_UNITS+=( "$candidate" )
        fi
    done
    # Failsafe: if neither is installed (e.g. running stand-alone before
    # install-systemd.sh has run), still try bridge so the script doesn't
    # silently no-op.
    if [[ "${#RESTART_UNITS[@]}" -eq 0 ]]; then
        RESTART_UNITS=( agent-me-bridge )
    fi
fi

log() { printf '%s [watch] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Graceful shutdown — SIGTERM from systemd should let the current iteration
# complete (or interrupt the sleep) and exit. set -e propagates a failed
# command but we want sleep interruption to be a clean exit.
running=1
trap 'running=0; log "received signal — exiting after current iteration"' TERM INT

log "starting; branch=$BRANCH interval=${INTERVAL}s units=${RESTART_UNITS[*]} cwd=$REPO_ROOT"

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

    # systemd --user: doesn't need sudo. Each unit's Restart=on-failure
    # picks up automatically; restart sends SIGTERM, the unit does its
    # graceful-shutdown, then comes back with new code.
    #
    # Bridge first (it's the user-visible Slack path), then dashboard.
    # We restart even if a unit is currently inactive — restart on a
    # stopped unit is equivalent to start, which is what we want after
    # an admin had stopped it manually for some reason.
    for unit in "${RESTART_UNITS[@]}"; do
        if systemctl --user restart "$unit"; then
            log "restarted $unit"
        else
            log "systemctl restart $unit failed — manual intervention needed"
        fi
    done

    sleep "$INTERVAL" || break
done

log "exiting cleanly"
