"""Native chat feature for the agent-me dashboard.

Public surface is intentionally tiny: construct the wired service with
``build_chat_service(repo_dir)`` and call it from thin route handlers.
"""
from __future__ import annotations

import os

from .config import ChatConfig
from .mirror import WebSessionMirror, web_session_id_for
from .native_sessions import NativeSessionReader
from .runner import ClaudeCliRunner
from .service import ChatService
from .store import DiskSessionStore
from .terminal import (
    TerminalSession,
    list_tmux_sessions,
    resolve_shell,
    resolve_tmux,
    stream_output,
    valid_tmux_name,
)

__all__ = [
    "ChatConfig",
    "ChatService",
    "NativeSessionReader",
    "TerminalSession",
    "WebSessionMirror",
    "build_chat_service",
    "build_session_mirror",
    "list_tmux_sessions",
    "resolve_shell",
    "resolve_tmux",
    "stream_output",
    "valid_tmux_name",
    "web_session_id_for",
]


def build_chat_service(repo_dir: str | os.PathLike[str]) -> ChatService:
    """Wire the default production graph: disk store + claude-CLI runner, plus a
    read-only reader of the CLI's own session store so the web lists/replays
    terminal + Slack sessions (everything ``claude --resume`` shows)."""
    config = ChatConfig.from_env(repo_dir)
    native = (
        NativeSessionReader(config.claude_projects_dir, tool_result_max=config.tool_result_max)
        if config.show_native_sessions else None
    )
    return ChatService(
        store=DiskSessionStore(config.data_dir),
        runner=ClaudeCliRunner(config),
        config=config,
        native=native,
    )


def build_session_mirror(repo_dir: str | os.PathLike[str]) -> WebSessionMirror:
    """A mirror pointed at the SAME on-disk store the dashboard /chat reads, so
    another process (the Slack bridge) can surface its turns in the web chat."""
    config = ChatConfig.from_env(repo_dir)
    return WebSessionMirror(DiskSessionStore(config.data_dir))
