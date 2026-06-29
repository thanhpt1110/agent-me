"""Value types emitted by the claude engine and consumed by every surface."""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum


def now_ms() -> int:
    return int(time.time() * 1000)


class EventKind(StrEnum):
    SNAPSHOT = "snapshot"
    USER = "user"
    INIT = "init"
    TEXT = "text"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    TURN_END = "turn_end"
    ERROR = "error"
    TIMEOUT = "timeout"


def make_event(kind: EventKind, **fields: object) -> dict:
    return {"kind": kind.value, "t": now_ms(), **fields}


@dataclass(frozen=True, slots=True)
class TurnResult:
    session_id: str | None
    result: str = ""
    is_error: bool = False
    exit_code: int = -1
    stderr: str = ""
    context_tokens: int = 0  # tokens occupying the model context after this turn
