"""Chat-specific value types.

The generic event/turn vocabulary now lives in
:mod:`agent_me.claude_agent.models` (shared with the Slack bridge and the brief)
and is re-exported here so existing ``from .models import EventKind`` /
``make_event`` / ``TurnResult`` imports keep working unchanged. Only
:class:`SessionMeta` (the chat's persisted ``meta.json`` shape) is chat-specific.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from agent_me.claude_agent.models import EventKind, TurnResult, make_event, now_ms

__all__ = ["EventKind", "SessionMeta", "TurnResult", "make_event", "now_ms"]


@dataclass(slots=True)
class SessionMeta:
    """Persistent metadata for one chat session (one ``meta.json``)."""

    id: str
    title: str
    cwd: str
    model: str | None = None
    claude_session_id: str | None = None  # the CLI's own id, used for --resume
    claude_started: bool = False
    created_at: int = field(default_factory=now_ms)
    last_activity: int = field(default_factory=now_ms)
    turns: int = 0

    def public_dict(self, *, busy: bool = False) -> dict:
        """JSON-safe view for API responses (adds the in-memory ``busy`` flag)."""
        return {**asdict(self), "busy": busy}

    @classmethod
    def from_dict(cls, d: dict) -> SessionMeta:
        known = {f: d[f] for f in cls.__slots__ if f in d}
        return cls(**known)
