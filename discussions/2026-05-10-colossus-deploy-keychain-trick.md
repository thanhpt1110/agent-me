# Phase 3 deploy on Colossus + Mac-Keychain MCP token transfer

_2026-05-10 evening · the deploy that replaced the Brev plan._

## What landed

1. **Pivoted Phase 3 target from Brev to Colossus.** Brev is external
   network — MaaS MCP endpoints (`*.nvidia.com`) all 401 from there,
   making the bridge useless on a Brev host. Colossus is internal,
   reaches everything. Renamed `design/deploy-on-brev.md` →
   `design/deploy-on-host.md` and made it host-agnostic.
2. **Steps 1–5 of `design/deploy-on-host.md` done on Colossus
   `1xA100-40`** (Ubuntu 24.04, 16 CPU / 125 GB / 731 GB free,
   internal network). Tools, bootstrap, secrets vault scp + apply,
   `gh auth`, `claude /login` — all complete.
3. **`scripts/sync-mcp-creds-to-host.sh`** — Mac→host MCP token sync.
   Got 16/17 maas-* ✓ Connected on Colossus in **one command instead
   of 16 browser OAuth flows**.
4. **Reauth helper polish** — graceful fallback when no browser is
   available (Linux headless), filter to `maas-*` by default so the
   4 claude.ai built-in MCPs (Linear / Figma / Amplitude / Template)
   don't show up in the helper output, per-call gap bumped 12 → 20s
   for slower ECI flows.

## The Keychain trick — how it was found

Started by trying the obvious thing: scp `~/.claude.json` from Mac
to the host. Result: 17 MCPs registered, 0 authenticated. The file
only carries `mcpServers.<name>.{type,url}` — registration metadata,
no tokens.

Where do tokens live then? On Linux the host already had
`~/.claude/.credentials.json` (14.5 KB) after `claude /login` —
`jq keys` showed `["claudeAiOauth"]`. So that's where Anthropic OAuth
goes. But still no `mcpOAuth` section.

On the Mac we had no `~/.claude/credentials.json` at all. The Mac
keeps OAuth in `Library/Keychains/login.keychain-db`. `security
dump-keychain | grep -i claude` surfaced two relevant items:

- `Claude Code-credentials` (66 KB)
- `Claude Safe Storage` (small, looks like an encryption-key
  wrapper)

`security find-generic-password -s 'Claude Code-credentials' -w` got
the 66 KB blob. It's plain JSON:

```json
{"mcpOAuth": {
  "maas-jira|a461df3f1d2caec8": {
    "serverName": "maas-jira",
    "serverUrl": "https://nvaihub.nvidia.com/maas/jira/mcp/",
    "accessToken": "Z0FB…",
    "refreshToken": "…",
    "expiresAt": ...,
    "redirectUri": "http://localhost:NNNN/callback",
    ...
  },
  "maas-confluence|...": {...},
  ...
}}
```

The Mac and the host store the *same JSON shape* — just under
different storage layers (Keychain item vs file) and with disjoint
top-level keys (`claudeAiOauth` only on host, `mcpOAuth` only on
Mac post-MCP-auth). So the merge is `jq -s '.[0] * .[1]'`. That's
the entire mechanism.

The script wraps it with backups, an `mcpOAuth`-presence sanity
check, and a verify step that runs `claude mcp list` on the host
and counts ✓ vs Needs auth.

## What actually happened on the host after merge

```
maas-confluence: ✓ Connected
maas-gitlab:     ✓ Connected
maas-gdrive:     ✓ Connected
maas-ippsec:     ✓ Connected
maas-jama:       ✓ Connected
maas-jira:       ✓ Connected
maas-mysql:      ✓ Connected
maas-nvbugs:     ! Needs authentication   ← stale on Mac too
maas-nvks-prom.: ✓ Connected
maas-nsight-cuda:✓ Connected
maas-onedrive:   ✓ Connected
maas-pagerduty:  ✓ Connected
maas-sharepoint: ✓ Connected
maas-glean:      ✓ Connected
maas-playwright: ✓ Connected (stdio, no auth needed)
maas-slack:      ✓ Connected
maas-outlook:    ✓ Connected
```

16/17. The one failure (nvbugs) was already stale on the Mac — its
token wasn't transferred because there wasn't a fresh one to
transfer. Reauth nvbugs on Mac, re-run the sync script, all 17
green.

## Caveat (and why it doesn't bite us)

Each token's `redirect_uri` records the localhost callback port the
Mac used at OAuth code-exchange time (e.g. `localhost:55060`). When
the access token expires (~1h) and Claude Code calls the auth server
to refresh, in theory the auth server could check that the request's
redirect_uri matches the one used at issuance. The OAuth 2.0 spec
doesn't require that for refresh requests, and ECI's auth server
doesn't enforce it in practice — confirmed empirically over multiple
hours of refreshes from the host with tokens born on the Mac.

If a future ECI version *does* tighten this, the fallback is the
SSH-port-forward + agent-me-reauth-on-the-host path — still
documented in `design/deploy-on-host.md` step 5 as the option-2
fallback.

## Daily ritual now

When the bridge DMs you "MCPs need re-auth":

```bash
cd ~/agent-me
uv run agent-me-reauth                         # opens stale URLs in Mac browser
./scripts/sync-mcp-creds-to-host.sh 1xA100-40  # mirror to Colossus
```

Two commands. The first is unchanged from before. The second is
new. Saves the round-trip of "ssh into Colossus, set up port
forwards, reauth there, click 16 tabs through SSH tunnel."

## What's still on the user's plate (steps 6–8 of the playbook)

```bash
# On Colossus:
cd ~/agent-me
./scripts/install-systemd.sh    # installs --user units, enable-linger
                                # bridge + watcher start

# Smoke test from Slack: DM the bot the word "mcp" → should reply
# with the `claude mcp list` output.

# Verify auto-deploy: from Mac, push any trivial commit. Within ~60s
# `journalctl --user -u agent-me-watch -f` on Colossus shows
# "behind by 1 commit" → "pulled <old> → <new>" → "restarted
# agent-me-bridge".
```

User is driving these from a claude session running on Colossus.

## Files touched today

- `design/deploy-on-host.md` (renamed from deploy-on-brev.md;
  step-5 rewritten to put Keychain transfer first)
- `scripts/sync-mcp-creds-to-host.sh` (new)
- `src/agent_me/scripts/reauth_mcps.py` (Linux fallback, maas-*
  filter, 20s gap)
- `STATE.md` (Phase 3 progress, decisions, roadmap)
- `~/agent-me-secrets.md` (LOCAL — restructured so
  `design/deploy-on-host.md`'s awk extractor parses it; never pushed)

Commits: `4917b41` (rename + Brev→host pivot), `d0c41da` (reauth
graceful fallback), `02667d8` (reauth maas-* filter), `07f7396`
(sync-mcp-creds-to-host.sh + design rewrite).

## Lesson learned (non-obvious)

When you see `~/.claude/credentials.json` missing on macOS, that's
not "Claude Code is unauthenticated" — that's "Claude Code stores
credentials in the Keychain on macOS, like 1Password or any
password manager would." Linux has no equivalent, so it falls back
to a chmod-600 file. Both are the same JSON underneath. This is
the kind of platform-specific storage detail that doesn't show up
in any docs but matters a lot for cross-machine deploys.
