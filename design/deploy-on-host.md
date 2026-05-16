# Deploy on host — single-shot playbook

This file is the **single source of truth** for taking a fresh
systemd-based Linux host from `git clone` to a 24/7 auto-updating
agent-me deployment.

Tested / recommended targets:
- **Colossus (NVIDIA internal)** — first-class, what we use. Internal
  network reaches all MaaS MCP endpoints without VPN gymnastics.
- **Any internal NVIDIA Linux box** with `systemd --user`, outbound
  to slack.com + github.com, and inbound from the human's browser
  (over SSH) for OAuth callbacks.
- **External cloud (Cloud host / EC2 / Lambda Cloud)** — works for the
  bridge process itself but the MaaS MCP endpoints (`*.nvidia.com`,
  `maas.prd.astra.nvidia.com`) generally aren't reachable from
  external networks, so Jira / GitLab / Confluence / NVBugs / Slack /
  Outlook MCP tools all 401 on the deployed bridge. Use only if you
  have a working VPN-on-the-host story.

It's written so a Claude Code session running on the host can follow
it end-to-end with minimal back-and-forth from the human. Each step
includes:
- exact commands
- a verification line you can run
- what to do if it fails

The deployer needs **two things from the human**:
1. The secrets file (`~/agent-me-secrets.md`, scp'd up before starting)
2. A browser, twice — once for `claude /login`, once for
   `agent-me-reauth` (NVIDIA SSO tabs, port-forwarded over SSH)

Everything else is automated.

## Outbound network the host must reach

Sanity-check before running the playbook on a new host. Run from the
target host:

```bash
for url in https://github.com https://slack.com \
           https://maas.prd.astra.nvidia.com \
           https://nvaihub.nvidia.com \
           https://enterprise-content-intelligence.nvidia.com; do
    if curl -fsSL --connect-timeout 5 -o /dev/null -w "%{http_code}\n" "$url" >/dev/null 2>&1; then
        echo "  ✓ $url"
    else
        echo "  ✗ $url   (will block deployment)"
    fi
done
```

NVIDIA-internal hosts (`*.nvidia.com`, `maas.prd.astra.nvidia.com`)
require internal network. Colossus has it; Cloud host/EC2 don't.

`api.anthropic.com` is needed if you authenticate Claude Code via
`claude /login`. NVIDIA-internal Inference Hub is an alternative if
Anthropic's API is blocked from your host (`NVIDIA_API_KEY` env var,
present in the secrets vault).

## Architecture (refresher)

```
                ┌──────────────┐  push from any machine
                │  GitHub      │  (Mac, laptop, codespace)
                │  agent-me    │
                └──────┬───────┘
                       │ poll origin/main every 60s
                       ▼
       ┌───────────────────────────────────────────┐
       │  Deploy host (Colossus / any systemd Linux│
       │                  on internal NVIDIA net)  │
       │  ┌───────────────────────┐  user systemd  │
       │  │ agent-me-watch.service│  enable-linger │
       │  │   git fetch / pull    │                │
       │  │   uv sync (if needed) │                │
       │  │   systemctl restart   │                │
       │  │     agent-me-bridge   │                │
       │  └──────────┬────────────┘                │
       │             │ on diff                     │
       │  ┌──────────▼────────────┐                │
       │  │ agent-me-bridge.svc   │  Slack         │
       │  │   uv run agent-me-    │  Socket Mode   │
       │  │   bridge              │  (outbound     │
       │  │                       │   WebSocket —  │
       │  │                       │   NO inbound   │
       │  │                       │   port needed) │
       │  └───────────────────────┘                │
       └───────────────────────────────────────────┘
```

**Slack does not need a public URL.** Bridge uses Socket Mode: it
opens a WebSocket *out* to Slack with the `xapp-…` token, and Slack
delivers events over that. No public endpoint, no firewall holes, no
port-expose needed for the bridge itself. (Watcher also doesn't need
inbound — it polls outbound to GitHub.) The host can sit fully behind
NVIDIA's internal network.

## Prerequisites the host should have

Run this as a checklist on the target host:

```bash
for cmd in git curl python3 jq node; do
    command -v "$cmd" >/dev/null && echo "  ✓ $cmd" || echo "  ✗ $cmd MISSING"
done
```

Missing tools to install (**ask the user before sudo**):

```bash
# uv (python package manager) — official installer:
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env   # adds uv to PATH

# gh (GitHub CLI):
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | sudo dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install -y gh

# Claude Code CLI (Anthropic's `claude`):
sudo npm install -g @anthropic-ai/claude-code
# verify:  claude --version
```

If any of those need a non-trivial choice (e.g. apt key conflict,
Node version mismatch), surface it to the user before sudo'ing.

## Step 1 — clone repo

```bash
cd ~
git clone https://github.com/thanhpt1110/agent-me.git
cd agent-me
```

**Verify:**
```bash
test -f CLAUDE.md && test -f STATE.md && echo "  ✓ repo cloned"
```

## Step 2 — bootstrap (Python deps + register MCPs)

```bash
./scripts/bootstrap.sh
```

This is idempotent. It does:
1. Pre-flight check (claude, uv, gh, jq) — errors out if anything missing.
2. `uv sync` — populates `.venv` from `pyproject.toml` + `uv.lock`.
3. Copies `configs/.env.example` to `configs/.env` if absent (so we
   know what env vars to expect).
4. Runs `scripts/setup-mcps.sh` — registers all 17 MaaS MCP servers
   at user scope (idempotent: skips already-registered).

**Verify:**
```bash
claude mcp list 2>&1 | grep -c "^maas-"   # should print 17
```

## Step 3 — upload + apply secrets

The human will scp `~/agent-me-secrets.md` from their Mac before
running this step:

```bash
# Run on the human's Mac (NOT on the deploy host):
scp ~/agent-me-secrets.md <host>:~/agent-me-secrets.md
```

Once `~/agent-me-secrets.md` is on the deploy host, apply it to
`configs/.env` and any other places the file says it goes:

```bash
test -f ~/agent-me-secrets.md && chmod 600 ~/agent-me-secrets.md \
    && echo "  ✓ vault present" \
    || echo "  ✗ /home/$USER/agent-me-secrets.md missing — ask user to scp it"
```

The vault is structured as a markdown file with an inventory table
and per-key "Where each goes (commands)" sections. The Slack tokens
section emits the `configs/.env` snippet you need:

```bash
# Pull just the bridge .env block out of the vault. It looks like:
#   ## configs/.env (bridge)
#   ```ini
#   SLACK_BOT_TOKEN=xoxb-…
#   …
#   ```
# Sed range extracts between the ```ini and closing ``` markers in
# that section.
awk '/^## configs\/\.env/,/^```$/' ~/agent-me-secrets.md \
  | awk '/^```ini/{p=1; next} /^```$/{p=0} p' \
  > configs/.env
chmod 600 configs/.env
echo "  ✓ configs/.env populated"
```

If the vault layout doesn't have a `## configs/.env` section yet,
fall back to the older `~/agent-me-secrets.md` format and copy
`SLACK_*` lines manually — surface this to the user to confirm.

**Verify:**
```bash
grep -c "REPLACE-ME" configs/.env   # should print 0
grep -E "^SLACK_(BOT|APP|SIGNING)_" configs/.env | wc -l   # should print 3
```

## Step 4 — authenticate Claude Code CLI

Two paths — pick whichever the human has set up locally:

```bash
# Option A — Anthropic OAuth (browser flow):
claude /login
# Will print a URL; open it in a browser, authenticate, paste the
# callback code back. Token persists in ~/.claude/credentials.json.

# Option B — env var (headless, for ANTHROPIC_API_KEY-based access):
echo 'export ANTHROPIC_API_KEY=sk-ant-…' >> ~/.bashrc
source ~/.bashrc
```

NVIDIA users: the user's local Mac may have NVIDIA-internal Claude
auth (no `~/.claude/credentials.json`). On a fresh server, easiest is Option A
unless the human is using `ANTHROPIC_API_KEY`.

**Verify:**
```bash
claude --version   # version line, no auth error
echo 'say hi' | claude -p --output-format json --model claude-haiku-4-5 \
  | jq -r '.result // .api_error_status // "no result"'
# should print a brief greeting, NOT an auth error
```

## Step 5 — authenticate MCP servers (NVIDIA SSO)

```bash
uv run agent-me-reauth
```

This spawns `claude` under a pty and runs
`mcp__<server>__authenticate` for each MCP showing
"! Needs authentication" in `claude mcp list`. It auto-opens browser
tabs (via `xdg-open` on Linux) — but on a headless server there's
no browser, so the URLs will be printed and need manual handling.

**Three ways to handle this on a remote host (preferred first):**

1. **macOS Keychain transfer (recommended if you authenticate on a
   Mac).** Claude Code stores MCP OAuth tokens in the Mac Keychain
   item `Claude Code-credentials` as plain JSON; the host stores them
   in `~/.claude/.credentials.json`. The keys are disjoint top-level
   (`claudeAiOauth` for Anthropic, `mcpOAuth` for MCPs) so they merge
   cleanly. Use the bundled helper:

   ```bash
   # On the Mac, after all 17 MCPs show ✓ Connected in `claude mcp list`:
   ./scripts/sync-mcp-creds-to-host.sh <ssh-alias>
   ```

   The helper also writes `~/.config/agent-me/codex-mcp-env.sh` on the
   host, installs a shell startup hook, and verifies how many
   `AGENT_ME_MCP_TOKEN_*` exports future shell-launched Codex sessions
   will see. Existing Codex sessions still need a restart because a
   running process cannot inherit newly-written environment variables.

   For the daily "reauth locally, then push to host" path, use the wrapper:

   ```bash
   # On the Mac:
   ./scripts/mac-reauth-and-sync.sh <ssh-alias>
   ```

   Verify with `claude mcp list` or `codex mcp list` on the host. Empirically
   16/17 turn ✓ Connected immediately this way; nvbugs is occasionally stale
   on the Mac too, in which case path 2 covers it.

   **Caveats**:
   - Each token's `redirect_uri` records the Mac's localhost:NNNN
     used at code-exchange time. Refresh requests don't re-validate
     `redirect_uri` for ECI in practice, so token refresh from the
     host works.
   - Re-run after any new MCP auth on the Mac, or daily-ish to keep
     refresh tokens fresh.
   - macOS may pop a permission dialog the first time
     `security find-generic-password` reads `Claude Code-credentials`
     — click "Always Allow".

2. **SSH port-forward + interactive reauth on the host.** If you
   don't have macOS, or path 1 missed a server, do it the manual
   way: SSH with port-forwards back to a workstation browser, then
   run `agent-me-reauth` on the host:

   ```bash
   # On the workstation:
   ssh -L 51080:localhost:51080 -L 51081:localhost:51081 \
       -L 51082:localhost:51082 -L 51083:localhost:51083 \
       -L 51084:localhost:51084 -L 51085:localhost:51085 \
       -L 51086:localhost:51086 <host>
   ```

   (Forward ~7 ports — one per stale MCP. Random ports in 51000-65535
   range.) URLs printed by the helper reference `localhost:5108X` —
   open them in the workstation browser; OAuth redirects come back
   through the SSH tunnel.

3. **Copy whole `~/.claude.json`** (registrations only, NOT tokens).
   Useful for mirroring MCP server *registrations* between machines
   without rerunning `setup-mcps.sh`. Tokens still need path 1 or 2.

**Verify:**
```bash
claude mcp list 2>&1 | grep -c "✓ Connected"
# should print 17 (all MCPs healthy) or close to it; nvbugs is
# historically flaky, that's OK
```

## Step 6 — install systemd services

```bash
./scripts/install-systemd.sh
```

This installs both unit files (`agent-me-bridge.service`,
`agent-me-watch.service`), enables linger so they survive logout,
and starts them.

**Verify:**
```bash
systemctl --user is-active agent-me-bridge.service   # should print "active"
systemctl --user is-active agent-me-watch.service    # should print "active"
journalctl --user -u agent-me-bridge -n 20 --no-pager
# should show "bridge_running" log line within ~5s of start
```

## Step 7 — smoke test from Slack

DM the bot from the human's Slack workspace:

```
mcp
```

Expected reply: a `claude mcp list` output dump showing all 17
servers. If you see this, **you're done**.

If the bot doesn't respond:
- `journalctl --user -u agent-me-bridge -f` → look for the WebSocket
  connection establishing (`A new session (s_…) has been established`)
- Slack app config: confirm Socket Mode = ON and the App Token is
  `xapp-…` not `xoxa-` (different tier)
- `configs/.env` has `SLACK_ALLOWED_USER_ID` set to the human's
  Slack user id — without it the bridge ignores all messages

## Step 8 — verify auto-deploy

This proves the watcher works:

```bash
# On the human's Mac:
cd ~/agent-me
echo "# deploy test $(date -u +%FT%TZ)" >> README.md
git add README.md && git commit -m "deploy test (revert in next commit)"
git push origin main
```

Within 60s, on the deploy host:
```bash
journalctl --user -u agent-me-watch -f
# should show "behind by 1 commit" → "pulled <old> → <new>" → "restarted agent-me-bridge"
```

Then revert the test commit:
```bash
git revert --no-edit HEAD && git push origin main
```

## Step 9 — install the Phase 4 dashboard (optional but recommended)

The dashboard is a read-only web view over the bridge's state. Reads
the same SQLite the bridge writes, tails `bridge.log`, fans out
on-demand brief refreshes per source, and shows three live log
streams (watcher / Slack interactions / Claude session traces).
It also serves the Auto SFA MCP endpoint at `/mcp/` from the same
dashboard service. Users who want MCP visit `/mcp/setup` once to verify
DevTest credentials and create a long-lived Agent Me bearer token. MCP
does not use the dashboard bearer/cookie auth middleware.

The default install assumes you're putting it behind a reverse proxy
on the NVIDIA-internal network — `https://agent-me.nvidia.com`.
Setup for the proxy host itself lives in
`design/deploy-proxy-on-host.md`. **You can skip this step entirely**
if the operator only wants the Slack interface; everything above is
sufficient for that.

The Auto SFA page derives the MCP endpoint from the request origin by
default. If users open `http://agent-me.nvidia.com/auto-sfa`, the code
block shows `http://agent-me.nvidia.com/mcp/`; if the proxy later serves
HTTPS, the same page shows the HTTPS MCP URL. Set
`AUTO_SFA_MCP_PUBLIC_BASE_URL` only when the MCP public endpoint must
differ from the dashboard page origin. Add matching
`AUTO_SFA_MCP_ALLOWED_ORIGINS` only when an MCP browser client sends an
`Origin` header that differs from the default allow-list.

The setup flow stores DevTest passwords and bearer tokens encrypted in
`${AGENT_ME_STATE_DIR}/auto-sfa-mcp.db`. The setup page remembers only a
signed token digest cookie so the same browser can reopen `/mcp/setup` and
copy the token again. If `AUTO_SFA_MCP_CREDENTIAL_KEY` is not set, the
dashboard creates a private Fernet key file at
`${AGENT_ME_STATE_DIR}/auto-sfa-mcp.fernet`; back up that key with the state
directory if MCP tokens should survive host rebuilds.

```bash
cd ~/agent-me
./scripts/install-dashboard.sh                 # default: reverse-proxy mode
# or
./scripts/install-dashboard.sh --tailscale     # also enable Tailscale Funnel as a backup public URL
# or
./scripts/install-dashboard.sh --token         # also generate DASHBOARD_TOKEN (defense-in-depth)
```

The default mode:
1. `uv sync` (idempotent).
2. Appends `DASHBOARD_TRUST_NETWORK=1` to `configs/.env` (the env flag
   that tells the dashboard a non-loopback bind is OK because upstream
   gates access via VPN + reverse proxy).
3. Installs and starts `agent-me-dashboard.service` (binds
   `0.0.0.0:8765`, accepts X-Forwarded-* from any upstream).
4. Updates the watcher unit so future `git push origin main` also
   restarts the dashboard along with the bridge.

**Verify:**
```bash
systemctl --user is-active agent-me-dashboard.service   # active
curl -sSL http://127.0.0.1:8765/healthz                 # {"ok":true,...}
curl -sI http://127.0.0.1:8765/ | grep -i x-dashboard-auth
# Expected: x-dashboard-auth: trust-network
curl -sI http://127.0.0.1:8765/mcp/ | head
# Expected: reachable MCP endpoint; unauthenticated calls get an Agent Me bearer challenge
```

The dashboard is now reachable from any host on the NVIDIA private
network via `http://<this-host>:8765`. To get `https://agent-me.nvidia.com`
working, follow `design/deploy-proxy-on-host.md` on the proxy host.

## Day-2 ops cheat sheet

```bash
# Live tail bridge:
journalctl --user -u agent-me-bridge -f

# Live tail watcher (see git pulls happen):
journalctl --user -u agent-me-watch -f

# Force immediate redeploy (skip the 60s wait):
cd ~/agent-me && git pull && uv sync && systemctl --user restart agent-me-bridge

# Pause auto-deploy (e.g. before a risky commit on main):
systemctl --user stop agent-me-watch

# Resume:
systemctl --user start agent-me-watch

# Take bridge down for maintenance:
systemctl --user stop agent-me-bridge agent-me-watch

# Reauth MCPs (when they go stale, ~daily):
uv run agent-me-reauth

# Inspect SQLite state:
sqlite3 ~/.local/state/agent-me/state.db '.tables'
sqlite3 ~/.local/state/agent-me/state.db 'SELECT * FROM claude_sessions;'

# Tail file logs (alternative to journald):
tail -F ~/.local/state/agent-me/bridge.log

# Run brief on demand:
uv run agent-me-brief --period day
```

## Things that can go wrong

| Symptom | Cause | Fix |
|---|---|---|
| `bootstrap.sh` says `claude not found` | CLI not installed | `npm install -g @anthropic-ai/claude-code` |
| `bootstrap.sh` says `uv not found` | uv not on PATH | `source ~/.local/bin/env` |
| `setup-mcps.sh` registers but reauth fails | `claude /login` not done | Step 4 |
| Bridge restarts every 5s | `configs/.env` missing or has REPLACE-ME | Step 3 |
| Bridge restarts every 5s, env looks ok | Wrong `SLACK_APP_TOKEN` (xoxa- not xapp-) | regenerate App Token in Slack app config |
| Watcher pulls but bridge doesn't restart | `loginctl enable-linger` not run | `sudo loginctl enable-linger $USER && systemctl --user daemon-reload` |
| `journalctl --user` empty after logout | linger not enabled, services died | same as above |
| Legacy MaaS MCPs go to 401 after a day | normal (MaaS OAuth tokens expire ~24h) | On the Mac: `./scripts/mac-reauth-and-sync.sh <ssh-alias>` |
| `git pull` fails: "would clobber" | local change on the host (someone edited there) | `git stash` (only if intentional) or hand-resolve |
| `git pull` fails: "diverged" | local commit on the host that isn't on origin | `git log origin/main..HEAD` to inspect; either push or reset |

## Why systemd --user (not system-level)

System-level units would need either a dedicated Linux user (over-
engineered for personal use) or `sudo` rules to let the watcher
restart the bridge (bigger blast radius if anything goes wrong with
the watcher). User-level units run under your normal account, write
state to `$HOME`, and `systemctl --user restart` works without sudo.

The cost: `loginctl enable-linger` is required, otherwise services
die on logout. That command needs sudo *once* during setup.

## Why polling (not webhooks)

We poll origin/main every 60s. Alternatives:

- **GitHub webhook → provider port-expose**: instant, but needs a
  publicly reachable HTTPS endpoint, a webhook secret to manage,
  and Cloud host's port-expose URL is fragile (rotates per instance
  restart). If the bridge needs to handle webhooks too, that's a
  second listener to keep alive.
- **GitHub Actions → SSH to cloud host**: faster than polling, but needs
  an SSH deploy key in GitHub Secrets, plus a separate runner
  setup. Workable if we want sub-30s deploys; overkill for now.

Polling at 60s is simple, no public surface area, and 60s
latency-to-deploy is fine for personal-scale work. Easy to switch
to webhooks later if we want — the watcher script is small.

## What lives where (quick reference)

```
~/agent-me/                            ← repo (git pulls update this)
├── configs/.env                       ← Slack tokens (NOT in git)
├── deploy/*.service                   ← systemd unit files (in git)
├── scripts/agent-me-watch.sh          ← polled by watcher (in git)
└── scripts/install-systemd.sh         ← idempotent installer (in git)

~/.config/systemd/user/                ← installed unit files (copied from deploy/)
├── agent-me-bridge.service
└── agent-me-watch.service

~/.local/state/agent-me/               ← runtime state
├── state.db                           ← SQLite: threads, sessions, audit
├── bridge.log                         ← rotating JSON log
├── brief.log                          ← brief subprocess output
└── chat-cwd/                          ← cwd for `claude -p` chat invocations

~/.claude/                             ← Claude Code state (per-user, per-machine)
├── credentials.json                   ← OAuth tokens (claude /login)
├── projects/<sanitized-cwd>/sessions/ ← per-cwd session jsonl files
└── ...

~/agent-me-secrets.md                  ← LOCAL-ONLY secrets vault (never push)
```
