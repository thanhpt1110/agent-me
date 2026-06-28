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

from .commands import discover_commands
from .config import ChatConfig
from .jobs import ChatTurnJob
from .local_sessions import purge_local_sessions
from .models import EventKind, SessionMeta, make_event, now_ms
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
        # In-process turn (this dashboard) OR an external turn (e.g. Slack)
        # still mid-flight in another process writing the same store.
        return sid in self._busy or self._external_busy(sid)

    def _external_busy(self, sid: str) -> bool:
        """A mirrored (Slack) session is "running" while its last event isn't a
        ``turn_end`` and it was touched recently — the window bounds a turn that
        crashed without a terminal event so it doesn't show busy forever."""
        if sid in self._busy:
            return False
        meta = self._store.get_meta(sid)
        # Only Slack-mirrored sessions are driven by another process; a half-
        # written web session (e.g. dashboard restart mid-turn) must not be
        # reported busy here — its in-process job is simply gone.
        if meta is None or meta.origin != "slack":
            return False
        last = self._store.last_event(sid)
        if not last or last.get("kind") == EventKind.TURN_END.value:
            return False
        # Recency from the freshest persisted event ('t'), not meta.last_activity:
        # a live turn only ever calls append_event, which never bumps last_activity.
        last_t = last.get("t", meta.last_activity)
        window_ms = int(self._config.external_busy_window_s * 1000)
        return (now_ms() - int(last_t)) < window_ms

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

    def available_commands(self) -> list[dict]:
        return [c.as_dict() for c in discover_commands(self._config.default_cwd)]

    def rename_session(self, sid: str, title: str) -> dict | None:
        clean = " ".join((title or "").split())[:120]
        if not clean:
            return None
        meta = self._store.update_meta(sid, title=clean)
        return meta.public_dict(busy=self.is_busy(sid)) if meta else None

    def delete_session(self, sid: str) -> bool:
        meta = self._store.get_meta(sid)
        if meta is None:
            return False
        task = self._tasks.pop(sid, None)
        if task is not None and not task.done():
            task.cancel()  # stops the claude subprocess (runner terminates the group)
        job = self._jobs.pop(sid, None)
        if job is not None:
            job.running = False
        self._busy.discard(sid)
        self._store.remove(sid)
        # Also delete the CLI's own transcripts so the chat doesn't linger in
        # `claude --resume` — but only for sessions this dashboard owns. A
        # mirrored Slack session shares its CLI transcript with the still-live
        # Slack thread; removing the web copy must not break Slack resume.
        if meta.origin != "slack":
            purge_local_sessions(
                self._config.claude_projects_dir, meta.cwd,
                [sid, meta.claude_session_id, *meta.claude_session_ids],
            )
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
        # Reject if a turn is already running here OR externally (a mirrored
        # Slack turn in the bridge process) — else two writers would interleave
        # events into one transcript and collide on seq.
        if self.is_busy(sid):
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
                append_system_prompt=self._config.append_system_prompt,
            )
            if result.is_error:
                last = next((ln for ln in reversed((result.stderr or "").splitlines()) if ln.strip()), "")
                self._emit(sid, job, make_event(
                    EventKind.ERROR,
                    text=f"claude exited with an error (code {result.exit_code}). {last[:300]}".rstrip()))
            # Pin/refresh the resumable session id even on error so the next turn resumes.
            new_sid = result.session_id or meta.claude_session_id
            seen = list(dict.fromkeys(
                s for s in [*meta.claude_session_ids, meta.claude_session_id, new_sid] if s
            ))
            self._store.update_meta(
                sid, claude_session_id=new_sid, claude_session_ids=seen,
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
        cap = self._config.replay_max
        job = self._jobs.get(sid)
        if job is not None:
            transcript = self._store.transcript(sid)     # full history (each has seq)
            for event in self._tail_cap(transcript, cap):
                yield event
            async for event in job.subscribe_events():   # live (buffer overlap deduped by seq)
                yield event
            return
        # No in-process job. Read the transcript once (events + byte offset) and
        # replay it; then either tail an external (Slack) in-flight turn so the
        # browser streams it live too, or tell the client to close.
        history, offset = self._store.transcript_since(sid, 0)
        for event in self._tail_cap(history, cap):
            yield event
        if self._external_busy(sid):
            async for event in self._tail_external(sid, offset):
                yield event
        else:
            yield make_event(EventKind.SNAPSHOT, status="idle", busy=False)

    @staticmethod
    def _tail_cap(events: list[dict], cap: int) -> list[dict]:
        """The last ``cap`` events (or all of them when ``cap`` is 0/unset).

        Bounds the transcript replayed to a (re)connecting client so opening a
        very long session can't ship — or make the browser render — thousands of
        rows at once. The full transcript stays on disk; ``seq`` is preserved so
        client dedup still works."""
        return events[-cap:] if cap and len(events) > cap else events

    async def _tail_external(self, sid: str, offset: int) -> AsyncIterator[dict]:
        """Poll the on-disk transcript for events appended by another process
        (a Slack turn), yielding only the newly-appended tail (by byte offset)
        until ``turn_end`` or the turn goes quiet past the keepalive window."""
        poll = max(0.05, self._config.external_tail_poll_s)
        idle = 0.0
        while True:
            await asyncio.sleep(poll)
            events, offset = self._store.transcript_since(sid, offset)
            if events:
                for event in events:
                    yield event
                    if event.get("kind") == EventKind.TURN_END.value:
                        return
                idle = 0.0
                continue
            idle += poll
            if idle >= self._config.keepalive_s:      # gone quiet → assume done/dead
                yield make_event(EventKind.SNAPSHOT, status="idle", busy=False)
                return
            yield make_event(EventKind.TIMEOUT, note="tail-keepalive")
