# MCP authentication for `agent-me`

## TL;DR

- MAAS-MCP tokens (NVIDIA-internal MCPs at `nvaihub.nvidia.com/maas/*` and
  `maas.prd.astra.nvidia.com/*`) expire **every ~24 hours**.
- When tokens expire, every server flips to `! Needs authentication` in
  `/mcp` output and the bot can no longer call those tools.
- **Re-auth has to happen on the host running the bridge** (today: your Mac;
  Phase 3+: Brev). It cannot happen from Slack — the OAuth flow needs an
  interactive browser session and writes to the host's `~/.claude.json`.
- Re-auth takes ~30 seconds and **does not require restarting the bridge** —
  it shells out to fresh `claude` per request and picks up the new token on
  the next call.

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

1. Open a fresh terminal (any directory is fine).
2. Run:
   ```
   claude
   ```
   The interactive REPL opens.
3. Ask it something that touches a maas-* server, e.g.:
   ```
   > use mcp__maas-jira__jira_search to find 1 issue assigned to me
   ```
4. If tokens are stale, Claude will print an auth URL like
   `https://nvaihub.nvidia.com/oauth/...` and pause.
5. **Cmd-click the URL** (or copy → paste in browser). Sign in with your
   NVIDIA SSO. The callback page will show "Authentication successful".
6. Return to the terminal. Claude will retry automatically and answer the
   question.
7. `/exit` to leave the REPL — tokens are persisted in `~/.claude.json`.

> The first MAAS-MCP you authenticate refreshes the **shared NVIDIA SSO
> session**, which auto-extends the token for every other `maas-*` server.
> You don't have to re-auth each one individually.

After step 7, run `/mcp` from Slack to confirm everything is `✓ Connected`.

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

## Future: bot-driven re-auth?

Not feasible. OAuth requires:
1. An HTTP redirect URI registered with NVIDIA's IDP.
2. A browser session that you control.

We could in theory:
- Have the bot DM you the auth URL when `/mcp` shows expired tokens,
- And let you paste the resulting code back into the bot.

That's a Phase-3+ enhancement (see `STATE.md` parking lot). For now, the
30-second terminal flow is the path.

## Quick checklist

- [ ] `/mcp` from Slack shows ≥1 `! Needs authentication`?
  → re-auth on host.
- [ ] Open terminal on the bridge host.
- [ ] `claude` (interactive) → run any MCP query.
- [ ] Click the SSO link, sign in, return to terminal.
- [ ] `/exit`.
- [ ] `/mcp` from Slack should now show everything `✓ Connected`.
- [ ] No bridge restart required.
