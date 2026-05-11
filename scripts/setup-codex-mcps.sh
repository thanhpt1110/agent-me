#!/usr/bin/env bash
# Register the MaaS MCP servers agent-me expects with Codex CLI.
#
# This mirrors the MaaS server set used by the Claude MCP setup, but writes to
# Codex's MCP config (`~/.codex/config.toml`) via `codex mcp add`.
#
# Codex CLI does not currently expose OAuth login support for these HTTP MaaS
# servers, so agent-me registers each HTTP server with a bearer-token env var.
# The runtime populates those env vars from the existing MaaS credential store.
#
#   uv run agent-me-codex-reauth

set -euo pipefail

SERVERS=(
    "maas-confluence|http|https://nvaihub.nvidia.com/maas/confluence/mcp/"
    "maas-gitlab|http|https://nvaihub.nvidia.com/maas/gitlab/mcp/"
    "maas-gdrive|http|https://nvaihub.nvidia.com/maas/gdrive/mcp/"
    "maas-glean|http|https://maas.prd.astra.nvidia.com/maas/glean/mcp"
    "maas-ippsec|http|https://nvaihub.nvidia.com/maas/ippsec_metrics/mcp/"
    "maas-jama|http|https://nvaihub.nvidia.com/maas/jama_cache/mcp/"
    "maas-jira|http|https://nvaihub.nvidia.com/maas/jira/mcp/"
    "maas-mysql|http|https://nvaihub.nvidia.com/maas/colossus_mysql/mcp/"
    "maas-nsight-cuda|http|https://nvaihub.nvidia.com/maas/nsight_cuda/mcp/"
    "maas-nvbugs|http|https://nvaihub.nvidia.com/maas/nvbugs/mcp/"
    "maas-nvks-prometheus|http|https://nvaihub.nvidia.com/maas/nvks_prometheus/mcp/"
    "maas-onedrive|http|https://nvaihub.nvidia.com/maas/onedrive/mcp/"
    "maas-outlook|http|https://maas.prd.astra.nvidia.com/maas/outlook/mcp"
    "maas-pagerduty|http|https://nvaihub.nvidia.com/maas/pagerduty/mcp/"
    "maas-sharepoint|http|https://nvaihub.nvidia.com/maas/sharepoint/mcp/"
    "maas-slack|http|https://maas.prd.astra.nvidia.com/maas/slack/mcp"
    "maas-playwright|stdio|npx -y @playwright/mcp@latest"
)

if ! command -v codex >/dev/null 2>&1; then
    echo "codex CLI not found in PATH" >&2
    exit 2
fi

token_env_var() {
    local name="$1"
    printf 'AGENT_ME_MCP_TOKEN_%s' "$(printf '%s' "$name" | tr '[:lower:]-' '[:upper:]_')"
}

echo "── codex version: $(codex --version 2>&1 | head -1)"
echo "── Resolving current Codex MCP registrations..."
existing="$(codex mcp list 2>&1 || true)"

added=0
updated=0
skipped=0
for entry in "${SERVERS[@]}"; do
    IFS='|' read -r name transport target <<<"$entry"
    if printf '%s\n' "$existing" | grep -qE "^${name}([[:space:]:]|$)"; then
        if [[ "$transport" == "http" ]]; then
            env_var="$(token_env_var "$name")"
            config_json="$(codex mcp get "$name" --json 2>/dev/null || true)"
            if printf '%s\n' "$config_json" | grep -q "\"bearer_token_env_var\": \"${env_var}\""; then
                printf "✓ %-26s already registered (%s)\n" "$name" "$env_var"
                skipped=$((skipped + 1))
                continue
            fi
            if codex mcp remove "$name" >/dev/null; then
                printf "↻ %-26s updating bearer token env var\n" "$name"
                updated=$((updated + 1))
            else
                printf "✗ %-26s FAILED to remove old registration\n" "$name" >&2
                exit 3
            fi
        else
            printf "✓ %-26s already registered\n" "$name"
            skipped=$((skipped + 1))
            continue
        fi
    fi

    case "$transport" in
        http)
            env_var="$(token_env_var "$name")"
            if codex mcp add "$name" --url "$target" --bearer-token-env-var "$env_var" >/dev/null; then
                printf "+ %-26s registered (HTTP, %s)\n" "$name" "$env_var"
                added=$((added + 1))
            else
                printf "✗ %-26s FAILED to register (HTTP %s)\n" "$name" "$target" >&2
                exit 3
            fi
            ;;
        stdio)
            # shellcheck disable=SC2086
            if codex mcp add "$name" -- $target >/dev/null; then
                printf "+ %-26s registered (stdio: %s)\n" "$name" "$target"
                added=$((added + 1))
            else
                printf "✗ %-26s FAILED to register (stdio %s)\n" "$name" "$target" >&2
                exit 3
            fi
            ;;
        *)
            echo "unknown transport '${transport}' for '${name}'" >&2
            exit 4
            ;;
    esac
done

echo
echo "Summary: ${added} added, ${updated} updated, ${skipped} already present (${#SERVERS[@]} total)."
echo
echo "Next:"
echo "  uv run agent-me-codex-reauth"
echo "  codex mcp list"
