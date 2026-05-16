# MCP authentication for `agent-me`

## TL;DR

- MAAS-MCP tokens (NVIDIA-internal MCPs at `nvaihub.nvidia.com/maas/*` and
  `maas.prd.astra.nvidia.com/*`) expire **every ~24 hours**.
- This is a **Claude Code client limitation**, not an NVIDIA server-side
  revoke: Anthropic's GrowthBook flag `tengu_willow_refresh_ttl_hours = 0`
  disables proactive OAuth-token refresh in Claude Code. Cursor's MCP
  extension implements its own refresh loop, which is why Cursor sessions
  appear to last longer than `claude` sessions for the same MAAS endpoints.
- **Re-auth has to happen on a machine with an interactive browser**. For a
  remote Colossus host, the recommended path is re-auth on the Mac, then sync
  the refreshed credential store to the host.
- Re-auth takes ~30 seconds and **does not require restarting the bridge** —
  it shells out to fresh `claude` per request and picks up the new token on
  the next call.
- The bridge **auto-detects** stale tokens every 6 hours via `claude mcp
  list` and **DMs you** the re-auth one-liner so you don't have to remember
  to check.
- Separate from those MaaS OAuth servers, agent-me also serves the Auto SFA MCP
  endpoint at `/mcp/`. Auto SFA MCP uses DevTest HTTP Basic Auth supplied by
  the caller's MCP client, not MaaS OAuth tokens.

## Auto SFA MCP auth

External agent clients connect to the Auto SFA MCP endpoint shown in the
dashboard Auto SFA page's `MCP` dropdown, currently
`https://agent-me.nvidia.com/mcp/` unless
`AUTO_SFA_MCP_PUBLIC_BASE_URL` overrides the public base URL.

Credential model:

- User enters DevTest username/password once when adding the MCP server in the
  agent client.
- The client sends those credentials as HTTP Basic Auth on MCP requests.
- Agent-me does not store a server-side MCP session or password.
- Tool calls receive credentials from the MCP transport and pass them to
  `magic-auto` for that run.
- Credentials do not expire inside agent-me; they stop working when DevTest
  rejects them or the client changes/removes them.

Because this is Basic Auth, prefer HTTPS. For an intentionally HTTP-only
internal proxy, set `AUTO_SFA_MCP_PUBLIC_BASE_URL=http://agent-me.nvidia.com`
and add a matching `AUTO_SFA_MCP_ALLOWED_ORIGINS` value only if a browser MCP
client sends an `Origin` header.

## Command cheat sheet

Run these from the Mac checkout when the target host is `1xA100-40`:

```bash
# Reauth on the Mac, then push credentials and Codex env exports to the host.
./scripts/mac-reauth-and-sync.sh 1xA100-40

# Copy the current Mac Keychain credentials to the host only.
./scripts/sync-mcp-creds-to-host.sh 1xA100-40

# Reauth only on the current machine; do not sync to a host.
uv run agent-me-codex-reauth
```

`sync-mcp-creds-to-host.sh` now also prepares Codex bearer-token auth on the
host by writing `~/.config/agent-me/codex-mcp-env.sh` and installing a small
shell startup hook. Future shell-launched Codex sessions inherit refreshed
`AGENT_ME_MCP_TOKEN_*` values automatically. The running Slack bridge also reads
the host credential store on each Codex call. Run `/mcp refresh` in Slack to
force-refresh every MaaS OAuth token that has a usable refresh token, rewrite the
persistent env file, load it into the bridge process, and verify without
restarting the bridge. If an MCP auth server rejects its refresh token, the
command reports that server; that is the point where Mac sync/reauth is needed.

## Where the auth state lives

`claude mcp list` reads connection state from Claude's local credential store
on the host that ran `claude`. For the Codex path, agent-me reads MaaS OAuth
access tokens from `~/.claude/.credentials.json` and exports them as
`AGENT_ME_MCP_TOKEN_*` before starting Codex subprocesses or new shell-launched
Codex sessions.

**This means:**

- Bridge running on your **Mac** → auth from your **Mac terminal**.
- Bridge running on **Colossus/remote Linux** → auth from your **Mac terminal**
  with `./scripts/mac-reauth-and-sync.sh <ssh-host>` whenever possible.
  If the Mac auth is still valid and only the host copy/env is stale, run
  `./scripts/sync-mcp-creds-to-host.sh <ssh-host>` on the Mac, then `/mcp refresh`
  in Slack.
- Bridge running on **Cloud host** without Mac sync → auth from a **Cloud host SSH
  session** with the auth URL opened in your local browser; the
  loopback-token-paste flow is documented at the bottom of this doc.
- You **cannot** auth via Slack DM. There is no browser there.

## When to re-auth

Symptoms that re-auth is needed:

1. `/mcp` (or `claude mcp list`) shows `! Needs authentication` for one or
   more `maas-*` servers.
2. The bot replies with something like _"Permission denied — please
   approve the `mcp__maas-...` tool…"_ or _"the request was denied"_.
3. A normal Claude Code session (terminal `claude`) starts asking you to
   click a `https://nvaihub.nvidia.com/...` link before it can answer.

In practice: **once a day** is the rough cadence.

## How to re-auth (Mac dev — today)

### Fast path: helper script (auto-open)

```
~/agent-me/scripts/reauth-mcps.mjs
```

What it does:

1. Runs `claude mcp list` and parses out every server flagged
   `! Needs authentication`.
2. Spawns a persistent `claude` REPL via piped stdin (so the local
   OAuth callback listeners stay alive).
3. Sends a single prompt instructing Claude to call
   `mcp__<server>__authenticate` for each stale server.
4. As Claude streams its output, the helper extracts each
   `https://...nvidia.com/...` auth URL and `open`s it in your default
   browser.

You sign in to NVIDIA SSO in each tab. Each browser redirects back to
the still-alive REPL on `localhost:<random>/callback`; tokens are stored
in `~/.claude.json`. When you're done, press Ctrl-C in the helper — it
sends `/exit` to claude and shuts down cleanly.

You don't paste anything. You don't type any prompts. Just sign in.

### Manual path

If the script can't run for any reason:

1. `claude` (interactive) in any directory.
2. Type or paste:
   ```
   > use mcp__maas-jira__jira_search to find 1 issue assigned to me
   ```
3. Cmd-click the printed `https://nvaihub.nvidia.com/oauth/...` URL.
4. Complete NVIDIA SSO. Return to terminal.
5. The first MAAS-MCP you re-auth refreshes the **shared NVIDIA SSO
   browser session**, so subsequent OAuth flows for other maas-* servers
   are typically a single redirect each (no password re-prompt). To force
   each server to actually run its flow, ask Claude to try one tool from
   each — that's exactly what the helper script does for you.
6. `/exit`.

After either path, run `/mcp` from Slack — every server should be
`✓ Connected`.

## How to re-auth (Cloud host — Phase 3+)

When the bridge moves to Cloud host, you'll have no local browser on the instance.
Two patterns work:

### Pattern A — SSH port forwarding (recommended)

```
ssh -L 8080:localhost:8080 cloud-instance
# ...inside the SSH session...
claude
> use mcp__maas-jira__jira_search to find 1 issue assigned to me
```

The OAuth callback runs on the cloud instance's `localhost:8080`, but you
forwarded that port to your Mac so the URL Claude prints opens in **your
local** browser and the callback hits your Mac → SSH tunnel → Cloud host.

### Pattern B — Manual token paste

If Claude Code prints `Open this URL: https://...` and a separate
`Once authenticated, paste your code:` prompt, the SSO completes in your
local browser, the page shows a one-time code, and you paste it back into
the SSH terminal. No tunneling needed but extra clicks.

## Why the bridge's `/mcp` showed everything "Needs authentication"

If you authenticated `claude` interactively earlier in the day and the
bridge's `/mcp` output later showed all servers as needing auth, the most
likely cause is **token expiry between the two events**. The bridge does
not maintain its own auth — it inherits from `~/.claude.json` — so a stale
config affects both equally. Re-auth from terminal fixes both.

## How the bridge helps

- **Periodic health probe.** On startup and every `MCP_CHECK_INTERVAL_MS`
  (default 6h) the bridge runs `claude mcp list`, parses out servers
  flagged `! Needs authentication`, and DMs the operator (auto-discovered
  from the first DM, or pinned via `SLACK_ALLOWED_USER_ID` in `.env`).
- **Throttled.** Same set of stale servers won't re-notify within
  `MIN_NOTIFY_GAP_MS` (default 4h), so you don't get spam if you read the
  first ping but haven't re-auth'd yet.
- **`/mcp`** is always there for ad-hoc checks.
- **`/mcp refresh`** force-refreshes every MaaS OAuth token that has refresh
  metadata, rewrites `~/.config/agent-me/codex-mcp-env.sh` from the host's
  `~/.claude/.credentials.json`, loads those tokens into the running bridge
  process, then runs the same MCP status probe. If a refresh token is rejected,
  the command names the affected server so you know when Mac sync/reauth is
  actually required.
- **`/whoami`** echoes your Slack user id so you can pin
  `SLACK_ALLOWED_USER_ID` in `.env` for both notification routing and
  single-user lockdown.

## Experiments that probably won't help

- **Override the GrowthBook flag.** Editing
  `~/.claude.json:cachedGrowthBookFeatures.tengu_willow_refresh_ttl_hours`
  to a non-zero value (e.g. 23) is tempting but Claude Code resyncs the
  cache periodically and will revert. Even if it stuck, the refresh code
  path may be guarded by additional flags. We don't recommend this hack.
- **Daemon-keepalive.** Leaving an interactive `claude` REPL alive in tmux
  doesn't extend MCP-token life — the bridge spawns fresh `claude -p`
  children that read the same on-disk auth state regardless.
- **Static API key headers.** Cursor's `~/.cursor/mcp.json` shows
  `headers: {}` for the same maas-* servers — i.e. NVIDIA does not
  publish long-lived bearer tokens for these endpoints. Both clients use
  the same OAuth flow; the difference is purely in client-side refresh.

## Future: bot-driven re-auth?

Not feasible end-to-end. OAuth requires:
1. An HTTP redirect URI registered with NVIDIA's IDP.
2. A browser session that you control.

A Phase-3+ enhancement could:
- Detect stale tokens (already done), then
- Capture the SSO link `claude` would print and DM it to you,
- And let you paste the resulting code back into the bot.

For now, the bridge's notification + 30-second terminal flow is the path.

## Quick checklist

- [ ] `/mcp` from Slack shows ≥1 `! Needs authentication`?
  → re-auth on host.
- [ ] Open terminal on the bridge host.
- [ ] `claude` (interactive) → run any MCP query.
- [ ] Click the SSO link, sign in, return to terminal.
- [ ] `/exit`.
- [ ] `/mcp` from Slack should now show everything `✓ Connected`.
- [ ] No bridge restart required.
