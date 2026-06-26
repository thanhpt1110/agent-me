"""Live event fan-out for one in-flight turn.

A direct mirror of the dashboard's ``AutoSFAJob`` (auto_sfa_runner.py): a turn
runs server-side and emits events to every subscribed SSE client through a
bounded queue, with a replay buffer so late/reconnecting clients catch up.
Single responsibility: live fan-out + replay (no subprocess, no persistence).
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator

from .models import EventKind, make_event


class ChatTurnJob:
    def __init__(self, session_id: str, *, start_seq: int = 0, buffer: int = 300,
                 queue_max: int = 512, keepalive_s: float = 120.0) -> None:
        self.session_id = session_id
        self.running = True
        self._subscribers: list[asyncio.Queue] = []
        self._buffer: deque[dict] = deque(maxlen=buffer)
        self._queue_max = queue_max
        self._keepalive_s = keepalive_s
        # Seeded from the existing transcript length so seq is monotonic across
        # the whole SESSION (not per-turn) — the client dedups the transcript
        # replay vs the live buffer globally and survives SSE reconnects.
        self._seq = start_seq

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- producer side --------------------------------------------------------
    def emit(self, event: dict) -> None:
        # Bounded per-subscriber queues: on overflow we drop the live copy (the
        # transcript on disk stays authoritative; a lagging client recovers by
        # reloading history). Buffer keeps the last N for catch-up.
        self._buffer.append(event)
        for q in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    def finish(self) -> None:
        """Mark terminal and wake any idle subscribers so they can close."""
        self.running = False
        for q in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(make_event(EventKind.TURN_END))

    # -- consumer side --------------------------------------------------------
    def _subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_max)
        self._subscribers.append(q)
        return q

    def _unsubscribe(self, q: asyncio.Queue) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(q)

    async def subscribe_events(self) -> AsyncIterator[dict]:
        """Yield buffered-then-live events. Emits a ``timeout`` keepalive when
        idle so the SSE connection stays open; ends after ``turn_end``."""
        q = self._subscribe()
        try:
            # replay everything seen so far (catch-up for late subscribers)
            for event in list(self._buffer):
                yield event
                if event.get("kind") == EventKind.TURN_END.value:
                    return
            if not self.running:
                return
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=self._keepalive_s)
                except TimeoutError:
                    yield make_event(EventKind.TIMEOUT, note="keepalive")
                    if not self.running:
                        return
                    continue
                yield event
                if event.get("kind") == EventKind.TURN_END.value:
                    return
        finally:
            self._unsubscribe(q)
