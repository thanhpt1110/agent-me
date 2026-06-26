"""Chat orchestration — the only object the route handlers touch.

Coordinates the injected :class:`SessionStore`, :class:`ClaudeRunner` and
:class:`ChatConfig` (constructor injection → fully unit-testable). Holds no
HTTP or I/O detail itself (SRP); a turn runs server-side in an ``asyncio.Task``
so it survives client disconnect, while every event is both persisted (catch-up
source) and fanned out live via a :class:`ChatTurnJob`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from .config import ChatConfig
from .jobs import ChatTurnJob
from .models import EventKind, SessionMeta, make_event
from .runner import ClaudeRunner
from .store import SessionStore

log = logging.getLogger("agent_me.dashboard.chat")


class ChatService:
    def __init__(self, store: SessionStore, runner: ClaudeRunner, config: ChatConfig) -> None:
        self._store = store
        self._runner = runner
        self._config = config
        self._jobs: dict[str, ChatTurnJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._busy: set[str] = set()

    @property
    def config(self) -> ChatConfig:
        return self._config

    # -- queries --------------------------------------------------------------
    def list_sessions(self) -> list[SessionMeta]:
        return self._store.list()

    def is_busy(self, sid: str) -> bool:
        return sid in self._busy

    def session_dicts(self) -> list[dict]:
        return [m.public_dict(busy=self.is_busy(m.id)) for m in self._store.list()]

    def create_session(self, *, title: str | None, cwd: str | None, model: str | None) -> SessionMeta:
        return self._store.create(
            title=title, cwd=(cwd or self._config.default_cwd), model=(model or self._config.model),
        )

    def get_session(self, sid: str) -> dict | None:
        meta = self._store.get_meta(sid)
        if meta is None:
            return None
        return {"meta": meta.public_dict(busy=self.is_busy(sid)), "transcript": self._store.transcript(sid)}

    def delete_session(self, sid: str) -> bool:
        if not self._store.exists(sid):
            return False
        task = self._tasks.pop(sid, None)
        if task is not None and not task.done():
            task.cancel()  # stops the claude subprocess (runner terminates the group)
        job = self._jobs.pop(sid, None)
        if job is not None:
            job.running = False
        self._busy.discard(sid)
        self._store.remove(sid)
        return True

    # -- commands -------------------------------------------------------------
    async def start_turn(self, sid: str, text: str) -> tuple[bool, str | None]:
        """Launch a server-owned turn. Returns ``(ok, error_code)``;
        error_code is ``not_found`` | ``empty`` | ``busy``."""
        meta = self._store.get_meta(sid)
        if meta is None:
            return False, "not_found"
        if not (text or "").strip():
            return False, "empty"
        if sid in self._busy:
            return False, "busy"

        self._busy.add(sid)
        job = ChatTurnJob(
            sid, start_seq=len(self._store.transcript(sid)),  # session-global seq
            buffer=self._config.event_buffer,
            queue_max=self._config.subscriber_queue_max, keepalive_s=self._config.keepalive_s,
        )
        self._jobs[sid] = job

        self._emit(sid, job, make_event(EventKind.USER, text=text))
        if (not meta.title or meta.title == "New chat") and meta.turns == 0:
            self._store.update_meta(sid, title=(" ".join(text.split())[:60] or "Chat"))

        self._tasks[sid] = asyncio.ensure_future(self._run_turn(sid, text, job))
        return True, None

    async def cancel_turn(self, sid: str) -> bool:
        """Request cancellation of the in-flight turn (Stop generating)."""
        task = self._tasks.get(sid)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def _emit(self, sid: str, job: ChatTurnJob, event: dict) -> dict:
        """Stamp a monotonic seq, persist, then fan out — one source of truth.
        The seq lets the client dedup the transcript replay against the live
        buffer and survive SSE auto-reconnects."""
        event["seq"] = job.next_seq()
        self._store.append_event(sid, event)
        job.emit(event)
        return event

    async def _run_turn(self, sid: str, text: str, job: ChatTurnJob) -> None:
        meta = self._store.get_meta(sid)
        try:
            if meta is None:
                self._emit(sid, job, make_event(EventKind.ERROR, text="Session metadata is missing; aborting turn."))
                return

            def on_event(evt: dict) -> None:
                self._emit(sid, job, evt)

            result = await self._runner.run_turn(
                prompt=text, cwd=meta.cwd, session_id=meta.claude_session_id,
                resume=meta.claude_started, model=meta.model or self._config.model,
                add_dirs=self._config.add_dirs, on_event=on_event,
            )
            if result.is_error:
                last = next((ln for ln in reversed((result.stderr or "").splitlines()) if ln.strip()), "")
                self._emit(sid, job, make_event(
                    EventKind.ERROR,
                    text=f"claude exited with an error (code {result.exit_code}). {last[:300]}".rstrip()))
            # Pin/refresh the resumable session id even on error so the next turn resumes.
            self._store.update_meta(
                sid, claude_session_id=(result.session_id or meta.claude_session_id),
                claude_started=True, turns=meta.turns + 1,
            )
        except asyncio.CancelledError:
            self._emit(sid, job, make_event(EventKind.ERROR, text="Turn cancelled by user."))
            raise
        except Exception as exc:
            log.exception("chat turn failed for %s", sid)
            self._emit(sid, job, make_event(EventKind.ERROR, text=f"Turn failed: {exc}"))
        finally:
            self._busy.discard(sid)
            self._tasks.pop(sid, None)
            self._emit(sid, job, make_event(EventKind.TURN_END))
            job.running = False
            self._jobs.pop(sid, None)

    # -- streaming ------------------------------------------------------------
    async def stream(self, sid: str) -> AsyncIterator[dict]:
        """Single source of truth for a (re)connecting client: full persisted
        history first, then live events. Both carry ``seq`` so the client
        dedups the overlap and survives auto-reconnect."""
        if not self._store.exists(sid):
            yield make_event(EventKind.ERROR, text="session not found")
            return
        job = self._jobs.get(sid)
        for event in self._store.transcript(sid):   # full history (each has seq)
            yield event
        if job is None:                              # idle: tell client to close
            yield make_event(EventKind.SNAPSHOT, status="idle", busy=False)
            return
        async for event in job.subscribe_events():   # live (buffer overlap deduped by seq)
            yield event
