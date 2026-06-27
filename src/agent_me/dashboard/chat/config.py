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

# Default model: the operator's standing decision is "always best/thinking-best".
DEFAULT_CHAT_MODEL = "claude-opus-4-8"


def _default_operating_context(repo: Path) -> str:
    """System-level instructions so a chat turn acts on the real host setup.

    Without this the spawned agent reached for ``gh``/GitHub (the repo's public
    mirror) and got confused about the branch; the working remote is GitLab.
    """
    return (
        "Operating context for this machine (authoritative):\n"
        f"- You run on host agent-me.nvidia.com (CPU) in the repo {repo}.\n"
        "- Always work on the `big-update` branch. Never switch to or commit on `main`.\n"
        "- The git remote `origin` is GitLab "
        "(gitlab-master.nvidia.com/vrdc-swqa/swqa-2e2-workflow/agent-me). "
        "The `github` remote is a public mirror only — do not push there.\n"
        "- `gh` is NOT installed; never use it. Use plain `git`.\n"
        "- After you finish a requested change, commit it on `big-update` with a clear "
        "message; a post-commit hook auto-pushes `big-update` to GitLab, so the user "
        "sees every commit on the remote.\n"
    )


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
        return cls(
            data_dir=data_dir,
            default_cwd=default_cwd,
            add_dirs=add_dirs,
            model=model,
            claude_bin=claude_bin,
            append_system_prompt=append_system_prompt,
            turn_timeout_s=turn_timeout_s,
        )
