"""Native chat feature for the agent-me dashboard.

Public surface is intentionally tiny: construct the wired service with
``build_chat_service(repo_dir)`` and call it from thin route handlers.
"""
from __future__ import annotations

import os

from .config import ChatConfig
from .runner import ClaudeCliRunner
from .service import ChatService
from .store import DiskSessionStore

__all__ = ["ChatConfig", "ChatService", "build_chat_service"]


def build_chat_service(repo_dir: str | os.PathLike[str]) -> ChatService:
    """Wire the default production graph: disk store + claude-CLI runner."""
    config = ChatConfig.from_env(repo_dir)
    return ChatService(
        store=DiskSessionStore(config.data_dir),
        runner=ClaudeCliRunner(config),
        config=config,
    )
