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


@dataclass(frozen=True, slots=True)
class ChatConfig:
    data_dir: Path
    default_cwd: str
    add_dirs: tuple[str, ...] = ()
    model: str | None = None
    claude_bin: str = "claude"
    tool_result_max: int = 8000      # truncate huge tool outputs before persist/stream
    turn_timeout_s: float = 0.0      # 0 = no overall turn timeout
    keepalive_s: float = 120.0       # SSE idle keepalive (mirrors the dashboard's 120s)
    event_buffer: int = 300          # replay buffer size for late subscribers
    subscriber_queue_max: int = 512  # per-subscriber backpressure bound

    @classmethod
    def from_env(cls, repo_dir: str | os.PathLike[str]) -> ChatConfig:
        repo = Path(repo_dir)
        data_dir = Path(os.environ.get("CHAT_DATA_DIR") or (repo / "data" / "chat-sessions"))
        default_cwd = (os.environ.get("CHAT_DEFAULT_CWD") or str(repo)).strip()
        add_dirs = tuple(
            p.strip() for p in (os.environ.get("CHAT_ADD_DIRS") or str(Path.home())).split(",") if p.strip()
        )
        model = (os.environ.get("CHAT_MODEL") or "").strip() or None
        claude_bin = (os.environ.get("CHAT_CLAUDE_BIN") or "claude").strip()
        try:
            turn_timeout_s = float(os.environ.get("CHAT_TURN_TIMEOUT_S") or 0)
        except ValueError:
            turn_timeout_s = 0.0
        return cls(
            data_dir=data_dir,
            default_cwd=default_cwd,
            add_dirs=add_dirs,
            model=model,
            claude_bin=claude_bin,
            turn_timeout_s=turn_timeout_s,
        )
