# Setup on a fresh host

Goal: a clean machine (Brev, new Mac, anything with Linux/macOS) goes
from `git clone` to a working agent-me deployment. The path is
deliberately scripted so you don't have to remember which 17 MCP
servers we use, or which env vars need filling.

## TL;DR

```bash
git clone https://github.com/thanhpt1110/agent-me.git
cd agent-me
./scripts/bootstrap.sh

# Then the three interactive steps the script prints:
claude /login              # one-time, links this machine to your Anthropic account
uv run agent-me-reauth     # opens browser tabs for each MaaS MCP NVIDIA SSO
$EDITOR configs/.env       # fill in Slack tokens (or scp ~/agent-me-secrets.md)
```

After that:

```bash
claude mcp list                       # all 17 should show ✓ Connected
uv run agent-me-brief --period day    # posts a brief to your Slack DM
uv run agent-me-bridge                # starts the always-on bridge
```

## What `bootstrap.sh` does

1. **Pre-flight check** — errors out if `claude`, `uv`, `gh`, or `jq`
   isn't on PATH, with install hints per platform. We do not auto-install
   these because they pin to system package managers (brew/apt/dnf) and
   silent installs are riskier than a clear "install this" message.

2. **`uv sync`** — reads `pyproject.toml` + `uv.lock` and creates
   `.venv` with pinned dependency versions. Reproducible across hosts.

3. **`configs/.env`** — copies from `.env.example` if absent, warns if
   `REPLACE-ME` placeholders are still there. The bridge refuses to
   start without real Slack tokens, so this is a soft gate.

4. **`scripts/setup-mcps.sh`** — registers all 17 MaaS MCP servers via
   `claude mcp add`. Idempotent: any server already in
   `claude mcp list` is reported and skipped. The list is hard-coded
   in the script — when NVIDIA adds a new server we want, append a row.
   Catalog of all available servers: [maas-mcp-catalog.md](maas-mcp-catalog.md).

5. **Print interactive next steps** — the things bootstrap.sh
   intentionally won't do because they need a browser:
     - `claude /login` (machine ↔ Anthropic account binding)
     - `uv run agent-me-reauth` (NVIDIA SSO per MCP)
     - filling in `configs/.env` from your secrets vault

## Prerequisites the host must have

| Tool | Why | Install |
|---|---|---|
| `claude` | Runs `claude -p` for brief subagents and `claude mcp` for OAuth | `npm install -g @anthropic-ai/claude-code` |
| `uv` | Python package manager + venv | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `gh` | GitHub source for the brief (issues, PRs, reviews) | `brew install gh` / `apt install gh` / `dnf install gh` |
| `jq` | Used by helper scripts (`tail-log.sh`) and the setup script | `brew install jq` / `apt install jq` / `dnf install jq` |
| `python ≥ 3.12` | Bridge + brief runtime | usually present; `uv` will fetch its own if needed |
| `node` | Required for `claude` CLI itself and for the playwright MCP (`npx`) | `brew install node` etc. |
| `git` | Pulling configs / pushing state | usually present |

On Brev specifically, `claude`, `gh`, and `jq` need explicit install:

```bash
# Brev L4-CPU (Ubuntu 22.04) one-shot:
sudo apt update && sudo apt install -y jq nodejs npm git python3.12 python3.12-venv
sudo npm install -g @anthropic-ai/claude-code
type -p curl >/dev/null && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
  && sudo apt update && sudo apt install -y gh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## What `setup-mcps.sh` registers

17 servers, hard-coded. Source of truth = the script's `SERVERS=()`
array. Pulling in a new one is a one-line edit:

```bash
"<server-name>|http|https://maas.prd.astra.nvidia.com/maas/<path>/mcp"
"<stdio-server>|stdio|<command and args>"
```

Run `./scripts/setup-mcps.sh` to apply. Idempotent.

### Always uses `--scope user`

The script always passes `--scope user` to `claude mcp add`, which
stores the server at `~/.claude.json#mcpServers` (global per-user)
rather than the default `local` scope (per-project at
`~/.claude.json#projects.<cwd>.mcpServers`). Reason: when an MCP is
project-local, the `mcp__<server>__authenticate` tool's response is
emitted differently — claude wraps the auth URL in conversational
prose ("Ask the user to open this URL…") rather than printing it as a
direct OAuth helper line, and `agent-me-reauth`'s URL-extractor sees
this just fine on the regex level but the trust/permission flow can
hang in subtle ways. Keeping every MCP at user scope makes them all
behave identically. We learned this the hard way migrating Slack and
Outlook on 2026-05-10.

## What needs to happen interactively (and why bootstrap.sh stops)

### `claude /login`

Once per machine. Opens a browser to `console.anthropic.com` and
captures an OAuth refresh token into `~/.claude/credentials.json`. After
this every `claude -p ...` call is authenticated automatically.

For headless Brev: `export ANTHROPIC_API_KEY=...` in your shell rc
instead of `claude /login`. The CLI prefers the env var if set.

### `uv run agent-me-reauth`

Opens one browser tab per stale MCP, each pointing at NVIDIA SSO
(`enterprise-content-intelligence.nvidia.com/v2/auth/<server>/authorize`).
You sign in once with your `@nvidia.com` account; the OAuth callback
hits a localhost listener owned by claude and the token persists in
`~/.claude.json`. Tokens last ~24h before another reauth is needed.

If a tab doesn't auto-open, the helper still prints the URL — paste
into a browser manually.

### Filling `configs/.env`

The brief and bridge both need:

- `SLACK_BOT_TOKEN` (`xoxb-...`)
- `SLACK_APP_TOKEN` (`xapp-...`)
- `SLACK_SIGNING_SECRET`
- `SLACK_ALLOWED_USER_ID` (your Slack user id; restricts the bridge to you)

These belong to your Slack workspace, not the host. Two sources of truth:
1. The Slack app config page (`api.slack.com/apps/<your-app>`)
2. Your `~/agent-me-secrets.md` vault (recommended after you've set it
   up once — `scp` the file across hosts)

Fill them in once, then never edit `configs/.env` from inside the bridge
process — restart it to pick up new values.

## Brev-specific notes

- **SSH port-forward for OAuth tabs.** When `agent-me-reauth` runs on
  Brev, the OAuth callback hits `localhost:<random>` on Brev, not your
  Mac. Two ways to handle this:
    1. Run `agent-me-reauth` *via SSH with port-forward*: `ssh -L 51080:localhost:51080 brev-host` and the callback comes back to Brev's listener which you reach through the tunnel.
    2. Or run `claude /login` and `agent-me-reauth` on your Mac (where browser auto-open works), then `scp ~/.claude.json brev-host:~/.claude.json` once. Tokens live in that file.
  The second option is simpler; refresh once a day.
- **Persistent runtime.** Use `systemd` (Linux) to keep
  `agent-me-bridge` alive across reboots. Example unit:
    ```ini
    [Unit]
    Description=agent-me Slack bridge
    After=network.target

    [Service]
    Type=simple
    User=agent
    WorkingDirectory=/home/agent/agent-me
    ExecStart=/home/agent/.local/bin/uv run agent-me-bridge
    Restart=on-failure
    EnvironmentFile=/home/agent/agent-me/configs/.env

    [Install]
    WantedBy=default.target
    ```
- **Daily MCP reauth via cron.** Add `0 6 * * * cd /home/agent/agent-me && uv run agent-me-reauth` to the agent user's crontab — but only after option (2) above is set up, so the cron run picks up your refreshed `~/.claude.json` from the Mac sync.

## Re-running

`bootstrap.sh` is idempotent. If you change pyproject.toml, edit
`.env`, or add a new MCP, just re-run it. Existing state (uv venv,
configured MCPs, claude login) is detected and preserved.
