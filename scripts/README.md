# scripts/ — categorized helpers

| Dir | What lives here |
|---|---|
| `setup/` | one-time install/bootstrap: `bootstrap.sh` (uv sync + MCP registration), `setup-mcps.sh`, `setup-codex-mcps.sh`, `install-systemd.sh`, `install-dashboard.sh`, `install-codex-mcp-env-on-host.sh` |
| `auth/` | credential/reauth flows (mostly Mac ↔ host): `mac-reauth-and-sync.sh`, `sync-mcp-creds-to-host.sh`, `open-auth-urls.py`, `extract-reauth-urls.py` |
| `ops/` | day-to-day host operations: `claude-persistent.sh` (claude in tmux), `brief-6am-loop.sh` (daily 06:00 VN brief loop for tmux), `agent-me-watch.sh`, `tail-log.sh`, `kill-bridge.sh` |
| `dev/` | development utilities: `verify_chat_ui.py` (headless browser UI check), `render-agent-me-avatar.py` |
| `tailwind/` | compiled-CSS build (`build.sh` — run after changing template class names) |

Python entry points (`agent-me-bridge`, `agent-me-brief`, …) are NOT here — they
live in `src/agent_me/` and run via `uv run <name>` (see `pyproject.toml`).
