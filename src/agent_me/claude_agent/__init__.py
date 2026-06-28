"""Shared ``claude`` CLI agent engine reused by /chat, the Slack bridge, and the brief."""
from __future__ import annotations

from .models import EventKind, TurnResult, make_event, now_ms
from .operating_context import DEFAULT_BRANCH, operating_context
from .runner import ClaudeAgentRunner, ClaudeRunner, OnEvent, RunnerConfig

__all__ = [
    "DEFAULT_BRANCH",
    "ClaudeAgentRunner",
    "ClaudeRunner",
    "EventKind",
    "OnEvent",
    "RunnerConfig",
    "TurnResult",
    "make_event",
    "now_ms",
    "operating_context",
]
