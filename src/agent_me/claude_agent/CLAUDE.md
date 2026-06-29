# claude_agent — shared `claude` CLI engine

Generic engine that spawns the local `claude` CLI and parses its NDJSON stream.
Reused by `/chat`, the Slack bridge, and the brief — keep it **surface-agnostic**
(no Slack/dashboard/brief specifics here).

- `models.py` — `EventKind`, `make_event`, `TurnResult` (incl. `context_tokens`), `now_ms`.
- `runner.py` — `ClaudeAgentRunner` (impl) + `ClaudeRunner` (Protocol) + `RunnerConfig`.
  `run_turn(...)` is the only entry point; it streams events via a sync `on_event`
  callback and returns a `TurnResult`. New CLI flags go in `_build_args`.
- `cli.py` — `resolve_cli_bin(name, env_var=...)`: the **one** place that locates a
  CLI binary. Searches env var → PATH → common dirs **incl. nvm/n node bins** so a
  systemd `--user` service (minimal PATH, no nvm) still finds npm-installed
  `claude`/`codex`. The bridge, brief, chat config, and state_reader all delegate to
  it — don't reintroduce a local `shutil.which("claude")` (that's the "Failed to
  launch claude: [Errno 2]" bug).

Gotchas: `--allowedTools` is variadic → pass one comma-joined token. The subprocess
runs in its own group (`start_new_session=True`) so terminate reaches child tools.

See repo-root `CLAUDE.md` for conventions (clean code, research → implement → test).
