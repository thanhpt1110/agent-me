#!/usr/bin/env bash
# scripts/setup-mcps.sh — register every MaaS MCP server with the user's
#                         `claude` CLI. Idempotent: already-registered
#                         servers are reported and skipped.
#
# Run anywhere, any time. Re-running on an already-set-up host is safe.
# Authentication happens separately via `uv run agent-me-reauth` — this
# script only ensures the URLs are wired up.
#
# Catalog source (re-fetch periodically — NVIDIA adds servers often):
#   https://ipp-safety-tools.gitlab-master-pages.nvidia.com/giza-llm-tools/giza_ai/docs/preprod/tutorial/maas-available-mcps/

set -euo pipefail

# ── Servers we use ─────────────────────────────────────────────────────
# Format: "<name>|<transport>|<url-or-command>"
#   transport=http   → register via `claude mcp add --transport http <name> <url>`
#   transport=stdio  → register via `claude mcp add <name> -- <command>`
#
# When adding new servers: pull the canonical URL from the MaaS catalog
# (link above) and append a row. Production base = maas.prd.astra.nvidia.com.

SERVERS=(
    "maas-confluence|http|https://maas.prd.astra.nvidia.com/maas/confluence/mcp"
    "maas-gdrive|http|https://maas.prd.astra.nvidia.com/maas/gdrive/mcp"
    "maas-gitlab|http|https://maas.prd.astra.nvidia.com/maas/gitlab/mcp"
    "maas-glean|http|https://maas.prd.astra.nvidia.com/maas/glean/mcp"
    "maas-ippsec|http|https://maas.prd.astra.nvidia.com/maas/ippsec_metrics/mcp"
    "maas-jama|http|https://maas.prd.astra.nvidia.com/maas/jama_cache/mcp"
    "maas-jira|http|https://maas.prd.astra.nvidia.com/maas/jira/mcp"
    "maas-mysql|http|https://maas.prd.astra.nvidia.com/maas/colossus_mysql/mcp"
    "maas-nsight-cuda|http|https://maas.prd.astra.nvidia.com/maas/nsight_cuda/mcp"
    "maas-nvbugs|http|https://maas.prd.astra.nvidia.com/maas/nvbugs/mcp"
    "maas-nvks-prometheus|http|https://maas.prd.astra.nvidia.com/maas/nvks_prometheus/mcp"
    "maas-onedrive|http|https://maas.prd.astra.nvidia.com/maas/onedrive/mcp"
    "maas-outlook|http|https://maas.prd.astra.nvidia.com/maas/outlook/mcp"
    "maas-pagerduty|http|https://maas.prd.astra.nvidia.com/maas/pagerduty/mcp"
    "maas-sharepoint|http|https://maas.prd.astra.nvidia.com/maas/sharepoint/mcp"
    "maas-slack|http|https://maas.prd.astra.nvidia.com/maas/slack/mcp"
    "maas-playwright|stdio|npx -y @playwright/mcp@latest"
)

# ── Pre-flight ─────────────────────────────────────────────────────────
if ! command -v claude >/dev/null 2>&1; then
    echo "❌ \`claude\` CLI not found in PATH. Install it first:" >&2
    echo "   npm install -g @anthropic-ai/claude-code" >&2
    echo "   (or follow https://docs.claude.com/en/docs/claude-code)" >&2
    exit 2
fi

echo "── claude version: $(claude --version 2>&1 | head -1)"
echo "── Resolving current MCP registrations…"

# Capture once so we don't re-run `claude mcp list` per server.
existing="$(claude mcp list 2>&1 || true)"

added=0
skipped=0
for entry in "${SERVERS[@]}"; do
    IFS='|' read -r name transport target <<<"$entry"
    if printf '%s\n' "$existing" | grep -q "^${name}:"; then
        printf "✓ %-26s already registered\n" "$name"
        skipped=$((skipped + 1))
        continue
    fi

    # Scope = user (global). Default is "local" (project-scoped, stored
    # under .projects.<cwd>.mcpServers in ~/.claude.json), which causes
    # the OAuth flow to behave differently — the auto-open URL helper
    # (`agent-me-reauth`) doesn't reliably extract URLs for project-local
    # servers. Forcing --scope user keeps every MCP at the same global
    # ~/.claude.json#mcpServers level, identical to the older Azure-auth
    # MCPs that already work.
    case "$transport" in
        http)
            if claude mcp add --scope user --transport http "$name" "$target" >/dev/null 2>&1; then
                printf "+ %-26s registered (HTTP, user scope)\n" "$name"
                added=$((added + 1))
            else
                printf "✗ %-26s FAILED to register (HTTP %s)\n" "$name" "$target" >&2
                exit 3
            fi
            ;;
        stdio)
            # shellcheck disable=SC2086
            if claude mcp add --scope user "$name" -- $target >/dev/null 2>&1; then
                printf "+ %-26s registered (stdio, user scope: %s)\n" "$name" "$target"
                added=$((added + 1))
            else
                printf "✗ %-26s FAILED to register (stdio %s)\n" "$name" "$target" >&2
                exit 3
            fi
            ;;
        *)
            echo "❌ unknown transport '${transport}' for '${name}'" >&2
            exit 4
            ;;
    esac
done

echo
echo "Summary: ${added} added, ${skipped} already present (${#SERVERS[@]} total)."

if [[ "$added" -gt 0 ]]; then
    cat <<'NEXT'

Next step — authenticate the new MCPs (interactive, opens browser tabs):
    uv run agent-me-reauth

NVIDIA SSO covers most servers. Slack/Outlook use ECI OAuth; sign in with
your NVIDIA account in the popped-up tab. Tokens persist ~24h.

Verify with:
    claude mcp list
NEXT
else
    echo
    echo "Nothing to do. Check auth health with: claude mcp list"
fi
