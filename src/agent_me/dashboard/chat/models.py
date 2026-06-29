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
    claude_session_ids: list[str] = field(default_factory=list)  # every CLI id seen (resume can fork)
    claude_started: bool = False
    created_at: int = field(default_factory=now_ms)
    last_activity: int = field(default_factory=now_ms)
    turns: int = 0
    origin: str = "web"               # "web" | "slack" — where the session was opened
    slack_thread_ts: str | None = None  # Slack thread this session mirrors (origin == "slack")
    # Durable queue: messages accepted but not yet fully answered. Persisted so a
    # dashboard restart mid-turn doesn't lose them — a detached worker drains them.
    pending: list[str] = field(default_factory=list)
    pending_attempts: int = 0         # consecutive signal-interrupts (poison-message guard)
    # PID of the detached turn-runner process draining this session (0 = none). It
    # runs in its own cgroup, so a dashboard restart doesn't kill it; the dashboard
    # tails the transcript and reconnects to it.
    running_pid: int = 0

    def public_dict(self, *, busy: bool = False) -> dict:
        """JSON-safe view for API responses (adds the in-memory ``busy`` flag).

        ``pending``/``pending_attempts`` are internal queue state — kept out of the
        API payload (the queued bubbles already stream in the transcript)."""
        d = asdict(self)
        for internal in ("pending", "pending_attempts", "running_pid"):
            d.pop(internal, None)
        return {**d, "busy": busy}

    @classmethod
    def from_dict(cls, d: dict) -> SessionMeta:
        known = {f: d[f] for f in cls.__slots__ if f in d}
        return cls(**known)
