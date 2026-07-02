"""Chat configuration as an injected, immutable value object.

Centralises every ``CHAT_*`` env read in one place (``from_env``) so no other
module reaches into ``os.environ`` at use-time (DIP). Everything downstream
takes a :class:`ChatConfig` by constructor injection, which makes the whole
feature testable without the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agent_me.claude_agent import operating_context as _default_operating_context
from agent_me.claude_agent import resolve_cli_bin

# Default model: the operator's standing decision is "always best/thinking-best".
DEFAULT_CHAT_MODEL = "claude-opus-4-8"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, slots=True)
class ChatConfig:
    data_dir: Path
    default_cwd: str
    # Default folder for NEW chats (blank → default_cwd). Distinct from
    # default_cwd on purpose: default_cwd stays the agent's own repo (branch
    # guard target, operating-context source) even when new chats should open
    # somewhere broader, e.g. the workspace umbrella.
    spawn_cwd: str = ""
    add_dirs: tuple[str, ...] = ()
    model: str | None = None
    claude_bin: str = "claude"
    append_system_prompt: str | None = None  # operating-context injected into every turn
    tool_result_max: int = 8000      # truncate huge tool outputs before persist/stream
    turn_timeout_s: float = 0.0      # 0 = no overall turn timeout
    keepalive_s: float = 120.0       # SSE idle keepalive (mirrors the dashboard's 120s)
    event_buffer: int = 300          # replay buffer size for late subscribers
    subscriber_queue_max: int = 512  # per-subscriber backpressure bound
    replay_max: int = 400            # cap transcript replay sent on (re)connect (0 = all)
    claude_projects_dir: Path = Path.home() / ".claude" / "projects"  # CLI session transcripts
    show_native_sessions: bool = True  # list terminal/CLI sessions (what `claude --resume` shows)
    terminal_enabled: bool = True    # Ctrl+~ host terminal (gated by the operator passcode)
    terminal_shell: str = ""         # "" → resolve $SHELL / bash at connect time
    terminal_cwd: str = ""           # "" → default_cwd
    # Persistent web terminal: back it with a named tmux session so a closed
    # browser/laptop never kills what's running in it (falls back to a plain
    # shell when tmux isn't installed). Same session is reachable over SSH.
    terminal_persist: bool = True
    terminal_tmux_session: str = "agme-web"
    # Cross-process mirroring (Slack turns written to this same store by another
    # process): tail the on-disk transcript so an in-flight external turn streams
    # live, and treat a session as busy while its turn is unfinished and recent.
    external_tail_poll_s: float = 1.0
    external_busy_window_s: float = 900.0  # 15 min: bound "busy" for a crashed external turn
    context_window: int = 1_000_000  # model context size; denominator for the /chat % gauge

    @classmethod
    def from_env(cls, repo_dir: str | os.PathLike[str]) -> ChatConfig:
        repo = Path(repo_dir)
        data_dir = Path(os.environ.get("CHAT_DATA_DIR") or (repo / "data" / "chat-sessions"))
        default_cwd = (os.environ.get("CHAT_DEFAULT_CWD") or str(repo)).strip()
        add_dirs = tuple(
            p.strip() for p in (os.environ.get("CHAT_ADD_DIRS") or str(Path.home())).split(",") if p.strip()
        )
        model = (os.environ.get("CHAT_MODEL") or DEFAULT_CHAT_MODEL).strip() or None
        # Resolve to an absolute path so a detached worker / prod dashboard whose
        # systemd PATH omits the nvm node bin can still launch claude.
        claude_bin = resolve_cli_bin("claude", env_var="CHAT_CLAUDE_BIN")
        append_system_prompt = (
            os.environ.get("CHAT_APPEND_SYSTEM_PROMPT")
            if "CHAT_APPEND_SYSTEM_PROMPT" in os.environ
            else _default_operating_context(Path(default_cwd or repo))
        ) or None
        try:
            turn_timeout_s = float(os.environ.get("CHAT_TURN_TIMEOUT_S") or 0)
        except ValueError:
            turn_timeout_s = 0.0
        try:
            context_window = int(os.environ.get("CHAT_CONTEXT_WINDOW") or 1_000_000)
        except ValueError:
            context_window = 1_000_000
        claude_home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        return cls(
            data_dir=data_dir,
            default_cwd=default_cwd,
            spawn_cwd=(os.environ.get("CHAT_SPAWN_DEFAULT_CWD") or "").strip(),
            add_dirs=add_dirs,
            model=model,
            claude_bin=claude_bin,
            append_system_prompt=append_system_prompt,
            turn_timeout_s=turn_timeout_s,
            context_window=context_window,
            claude_projects_dir=Path(claude_home) / "projects",
            show_native_sessions=_env_flag("CHAT_SHOW_NATIVE_SESSIONS", True),
            terminal_enabled=_env_flag("CHAT_TERMINAL_ENABLED", True),
            terminal_shell=(os.environ.get("CHAT_TERMINAL_SHELL") or "").strip(),
            terminal_cwd=(os.environ.get("CHAT_TERMINAL_CWD") or "").strip(),
            terminal_persist=_env_flag("CHAT_TERMINAL_PERSIST", True),
            # Per-instance default: DEV and PROD run as the same uid (same tmux
            # socket) — one shared name would fuse their terminals into a single
            # live screen across both gates.
            terminal_tmux_session=(
                (os.environ.get("CHAT_TERMINAL_TMUX_SESSION") or "").strip()
                or f"agme-web-{(os.environ.get('DEPLOY_ENV') or '').strip().lower() or 'prod'}"
            ),
        )
