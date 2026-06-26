"""Immutable value types for the chat feature.

Pure data — no I/O, no behaviour beyond serialization. These are the typed
hand-offs between the store, runner, job and service layers (SRP: the only
reason to change them is the wire/persistence schema).
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum


def now_ms() -> int:
    """Millisecond wall-clock, matching the dashboard's ``now_ms`` convention."""
    return int(time.time() * 1000)


class EventKind(StrEnum):
    """Every event the chat stream can carry (1:1 with the Claude CLI taxonomy)."""

    SNAPSHOT = "snapshot"      # first frame on (re)subscribe: current session status
    USER = "user"             # the user's message text
    INIT = "init"             # CLI system/init: carries the resumable session id
    TEXT = "text"             # assistant text block
    THINKING = "thinking"     # assistant extended-thinking block
    TOOL_USE = "tool_use"     # assistant requested a tool
    TOOL_RESULT = "tool_result"  # result of a tool call
    RESULT = "result"         # terminal CLI result for the turn
    TURN_END = "turn_end"     # turn finished (client closes EventSource here)
    ERROR = "error"           # spawn/parse failure
    TIMEOUT = "timeout"       # no events for the keepalive window; stream stays open


def make_event(kind: EventKind, **fields: object) -> dict:
    """Build a wire/persistence event record: ``{"kind", "t", **fields}``.

    Events travel as plain dicts to match the dashboard's existing SSE
    convention (``{"event": ..., "data": json.dumps(evt)}``), while
    :class:`EventKind` keeps the vocabulary typed and centralised.
    """
    return {"kind": kind.value, "t": now_ms(), **fields}


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Outcome of running one turn through the Claude CLI."""

    session_id: str | None
    result: str = ""
    is_error: bool = False
    exit_code: int = -1
    stderr: str = ""


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
