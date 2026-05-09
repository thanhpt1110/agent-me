# MCP authentication for `agent-me`

## TL;DR

- MAAS-MCP tokens (NVIDIA-internal MCPs at `nvaihub.nvidia.com/maas/*` and
  `maas.prd.astra.nvidia.com/*`) expire **every ~24 hours**.
- This is a **Claude Code client limitation**, not an NVIDIA server-side
  revoke: Anthropic's GrowthBook flag `tengu_willow_refresh_ttl_hours = 0`
  disables proactive OAuth-token refresh in Claude Code. Cursor's MCP
  extension implements its own refresh loop, which is why Cursor sessions
  appear to last longer than `claude` sessions for the same MAAS endpoints.
- **Re-auth has to happen on the host running the bridge** (today: your Mac;
  Phase 3+: Brev). It cannot happen from Slack — the OAuth flow needs an
  interactive browser session and writes to the host's `~/.claude.json`.
- Re-auth takes ~30 seconds and **does not require restarting the bridge** —
  it shells out to fresh `claude` per request and picks up the new token on
  the next call.
- The bridge **auto-detects** stale tokens every 6 hours via `claude mcp
  list` and **DMs you** the re-auth one-liner so you don't have to remember
  to check.

## Where the auth state lives

`claude mcp list` reads connection state from `~/.claude.json` on the host
that ran `claude`. The token is written there during the OAuth callback step
of the auth flow. Anything that spawns `claude` as a child process (the
bridge does this with `cwd = ~/agent-me/`) inherits that state automatically.

**This means:**

- Bridge running on your **Mac** → auth from your **Mac terminal**.
- Bridge running on **Brev** → auth from a **Brev SSH session** (with the
  auth URL opened in your local browser; the loopback-token-paste flow is
  documented at the bottom of this doc).
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

### Fast path: helper script

```
~/agent-me/scripts/reauth-mcps.sh
```

What it does:

1. Runs `claude mcp list` and parses out every server flagged
   `! Needs authentication`.
2. Builds a Claude prompt that calls a read-only tool from each stale
   server (forces each one's OAuth flow).
3. Copies the prompt to your macOS clipboard (`pbcopy`).
4. After a 3-second countdown, `exec`s an interactive `claude` REPL.

In the REPL: **Cmd-V → Enter**. Claude prints one auth URL per stale
server. **Cmd-click each URL** to complete NVIDIA SSO in the browser.
The OAuth callback handler running inside the REPL captures each redirect
and stores the new tokens in `~/.claude.json`. Type `/exit` when all
servers show ✓ in the next `/mcp` check.

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

## How to re-auth (Brev — Phase 3+)

When the bridge moves to Brev, you'll have no local browser on the instance.
Two patterns work:

### Pattern A — SSH port forwarding (recommended)

```
ssh -L 8080:localhost:8080 brev-instance
# ...inside the SSH session...
claude
> use mcp__maas-jira__jira_search to find 1 issue assigned to me
```

The OAuth callback runs on the Brev instance's `localhost:8080`, but you
forwarded that port to your Mac so the URL Claude prints opens in **your
local** browser and the callback hits your Mac → SSH tunnel → Brev.

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
