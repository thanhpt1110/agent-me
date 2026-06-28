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
    terminal_enabled: bool = True    # Ctrl+~ host terminal (gated by the operator passcode)
    terminal_shell: str = ""         # "" → resolve $SHELL / bash at connect time
    terminal_cwd: str = ""           # "" → default_cwd
    # Cross-process mirroring (Slack turns written to this same store by another
    # process): tail the on-disk transcript so an in-flight external turn streams
    # live, and treat a session as busy while its turn is unfinished and recent.
    external_tail_poll_s: float = 1.0
    external_busy_window_s: float = 900.0  # 15 min: bound "busy" for a crashed external turn

    @classmethod
    def from_env(cls, repo_dir: str | os.PathLike[str]) -> ChatConfig:
        repo = Path(repo_dir)
        data_dir = Path(os.environ.get("CHAT_DATA_DIR") or (repo / "data" / "chat-sessions"))
        default_cwd = (os.environ.get("CHAT_DEFAULT_CWD") or str(repo)).strip()
        add_dirs = tuple(
            p.strip() for p in (os.environ.get("CHAT_ADD_DIRS") or str(Path.home())).split(",") if p.strip()
        )
        model = (os.environ.get("CHAT_MODEL") or DEFAULT_CHAT_MODEL).strip() or None
        claude_bin = (os.environ.get("CHAT_CLAUDE_BIN") or "claude").strip()
        append_system_prompt = (
            os.environ.get("CHAT_APPEND_SYSTEM_PROMPT")
            if "CHAT_APPEND_SYSTEM_PROMPT" in os.environ
            else _default_operating_context(Path(default_cwd or repo))
        ) or None
        try:
            turn_timeout_s = float(os.environ.get("CHAT_TURN_TIMEOUT_S") or 0)
        except ValueError:
            turn_timeout_s = 0.0
        claude_home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        return cls(
            data_dir=data_dir,
            default_cwd=default_cwd,
            add_dirs=add_dirs,
            model=model,
            claude_bin=claude_bin,
            append_system_prompt=append_system_prompt,
            turn_timeout_s=turn_timeout_s,
            claude_projects_dir=Path(claude_home) / "projects",
            terminal_enabled=_env_flag("CHAT_TERMINAL_ENABLED", True),
            terminal_shell=(os.environ.get("CHAT_TERMINAL_SHELL") or "").strip(),
            terminal_cwd=(os.environ.get("CHAT_TERMINAL_CWD") or "").strip(),
        )
