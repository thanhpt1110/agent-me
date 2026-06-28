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
from .native_sessions import NativeSessionReader
from .runner import ClaudeRunner
from .store import SessionStore

log = logging.getLogger("agent_me.dashboard.chat")


class ChatService:
    def __init__(self, store: SessionStore, runner: ClaudeRunner, config: ChatConfig,
                 native: NativeSessionReader | None = None) -> None:
        self._store = store
        self._runner = runner
        self._config = config
        self._native = native
        self._jobs: dict[str, ChatTurnJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._busy: set[str] = set()
        self._queues: dict[str, list[str]] = {}  # follow-ups typed while a turn runs

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
        """Web-store sessions merged with the native CLI sessions (the ones
        ``claude --resume`` lists), so terminal- and headless-born chats show up
        too. Dedup by every CLI id a web session already covers."""
        web = self._store.list()
        dicts = [m.public_dict(busy=self.is_busy(m.id)) for m in web]
        if self._native is None:
            return dicts
        represented = {
            s for m in web for s in (m.id, m.claude_session_id, *m.claude_session_ids) if s
        }
        extra = [
            n.public_dict() for n in self._native.list_sessions(self._config.default_cwd)
            if n.id not in represented
        ]
        return sorted(dicts + extra, key=lambda d: d.get("last_activity", 0), reverse=True)

    def create_session(self, *, title: str | None, cwd: str | None, model: str | None) -> SessionMeta:
        return self._store.create(
            title=title, cwd=(cwd or self._config.default_cwd), model=(model or self._config.model),
        )

    def get_session(self, sid: str) -> dict | None:
        meta = self._store.get_meta(sid)
        if meta is not None:
            return {"meta": meta.public_dict(busy=self.is_busy(sid)),
                    "transcript": self._store.transcript(sid)}
        if self._native is not None:
            n = self._native.find(self._config.default_cwd, sid)
            if n is not None:
                events = self._native.read_events_tail(self._config.default_cwd, sid, self._config.replay_max or 0) or []
                return {"meta": n.public_dict(), "transcript": events}
        return None

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
            return self._delete_native(sid)
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

    def _delete_native(self, sid: str) -> bool:
        """Delete a native (terminal/CLI) session that was never adopted into the
        web store: remove its CLI transcript so it's gone from ``claude --resume``
        too. A session open elsewhere is left alone (don't yank a live file)."""
        if self._native is None:
            return False
        n = self._native.find(self._config.default_cwd, sid)
        if n is None or n.live:
            return False
        purge_local_sessions(self._config.claude_projects_dir, self._config.default_cwd, [sid])
        self._native.forget(sid)
        return True

    # -- commands -------------------------------------------------------------
    async def start_turn(self, sid: str, text: str) -> tuple[bool, str | None]:
        """Accept a user message. Returns ``(ok, error_code)``; error_code is
        ``not_found`` | ``empty`` | ``busy`` | ``live``.

        Terminal-like: if a turn is already running **in this process**, the
        message is queued (shown immediately) and runs after the current one —
        the user isn't blocked. Only an *external* (Slack) in-flight turn rejects,
        since this process can't safely interleave into another writer's stream."""
        meta = self._store.get_meta(sid)
        if meta is None:
            meta, err = self._adopt_native(sid)
            if meta is None:
                return False, err
        text = (text or "").strip()
        if not text:
            return False, "empty"

        job = self._jobs.get(sid)
        if job is not None:                       # an in-process turn is running → queue
            self._queues.setdefault(sid, []).append(text)
            self._emit(sid, job, make_event(EventKind.USER, text=text, queued=True))
            return True, None
        if self._external_busy(sid):              # a Slack turn owns the stream elsewhere
            return False, "busy"

        self._busy.add(sid)
        job = ChatTurnJob(
            sid, start_seq=self._store.last_seq(sid),  # session-global seq (cheap tail read)
            buffer=self._config.event_buffer,
            queue_max=self._config.subscriber_queue_max, keepalive_s=self._config.keepalive_s,
        )
        self._jobs[sid] = job
        self._queues[sid] = [text]
        self._emit(sid, job, make_event(EventKind.USER, text=text))
        if (not meta.title or meta.title == "New chat") and meta.turns == 0:
            self._store.update_meta(sid, title=(" ".join(text.split())[:60] or "Chat"))

        self._tasks[sid] = asyncio.ensure_future(self._drain(sid, job))
        return True, None

    def _adopt_native(self, sid: str) -> tuple[SessionMeta | None, str | None]:
        """Bring a native (terminal/CLI) session into the web store so it can be
        continued from the browser. Imports the native transcript verbatim — same
        seq numbering the read-only replay used — so the first web turn ``--resume``s
        the real CLI session and the history dedups across the adopt boundary. A
        session open elsewhere stays read-only (resuming a live session forks it)."""
        if self._native is None:
            return None, "not_found"
        n = self._native.find(self._config.default_cwd, sid)
        if n is None:
            return None, "not_found"
        if n.live:
            return None, "live"
        events = self._native.read_events(self._config.default_cwd, sid, cap=0) or []
        self._store.ensure(sid, title=n.title, cwd=n.cwd, model=self._config.model,
                           claude_session_id=sid, origin="terminal")
        for event in events:
            self._store.append_event(sid, event)
        meta = self._store.update_meta(sid, claude_started=True, turns=n.turns)
        return meta, None

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

    async def _drain(self, sid: str, job: ChatTurnJob) -> None:
        """Run the queued messages one after another on a single job, emitting
        exactly one ``turn_end`` when the queue empties. The client keeps one open
        stream across follow-ups; cancelling stops the run and drops the queue."""
        try:
            while True:
                queue = self._queues.get(sid)
                if not queue:
                    break
                await self._run_agent(sid, queue.pop(0), job)
        except asyncio.CancelledError:
            self._emit(sid, job, make_event(EventKind.ERROR, text="Turn cancelled by user."))
            raise
        finally:
            self._busy.discard(sid)
            self._tasks.pop(sid, None)
            self._queues.pop(sid, None)
            self._emit(sid, job, make_event(EventKind.TURN_END))
            job.running = False
            self._jobs.pop(sid, None)

    async def _run_agent(self, sid: str, text: str, job: ChatTurnJob) -> None:
        """One agent invocation on ``job`` (the drain owns busy/turn_end)."""
        meta = self._store.get_meta(sid)
        if meta is None:
            self._emit(sid, job, make_event(EventKind.ERROR, text="Session metadata is missing; aborting turn."))
            return

        def on_event(evt: dict) -> None:
            self._emit(sid, job, evt)

        try:
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
            raise
        except Exception as exc:
            log.exception("chat turn failed for %s", sid)
            self._emit(sid, job, make_event(EventKind.ERROR, text=f"Turn failed: {exc}"))

    # -- streaming ------------------------------------------------------------
    async def stream(self, sid: str) -> AsyncIterator[dict]:
        """Replay only the recent tail (O(limit), not O(file)) then live events.
        A leading ``snapshot`` carries the paging ``cursor`` (scroll-up loads
        older); every event carries ``seq`` so the client dedups + survives
        reconnect."""
        if not self._store.exists(sid):
            async for event in self._stream_native(sid):
                yield event
            return
        cap = self._config.replay_max or 0
        job = self._jobs.get(sid)
        events, cursor = self._store.tail(sid, cap)
        yield make_event(EventKind.SNAPSHOT, status=("running" if job else "open"), cursor=cursor)
        for event in events:
            yield {**event, "replay": True}    # history: a past turn_end must NOT close the live stream
        if job is not None:
            async for event in job.subscribe_events():   # live (overlap deduped by seq)
                yield event
            return
        if self._external_busy(sid):                     # an in-flight Slack turn
            async for event in self._tail_external(sid, self._store.size(sid)):
                yield event
        else:
            yield make_event(EventKind.SNAPSHOT, status="idle", busy=False)

    def history_before(self, sid: str, cursor: int, limit: int) -> dict:
        """Older events for scroll-up paging. Web sessions page by byte offset;
        native (terminal) previews only show their tail, so they have no older."""
        if self._store.exists(sid):
            events, next_cursor = self._store.events_before(sid, cursor, limit)
            return {"events": events, "cursor": next_cursor}
        return {"events": [], "cursor": 0}

    async def _stream_native(self, sid: str) -> AsyncIterator[dict]:
        """Replay a native (terminal/CLI) session read-only: the recent tail of the
        conversation as ``claude --resume`` has it on disk, then an idle snapshot.
        A live external turn isn't captured here (no stdout) — the session-list
        poll keeps the sidebar dot fresh; continuing it from the web adopts it."""
        events = (
            self._native.read_events_tail(self._config.default_cwd, sid, self._config.replay_max or 0)
            if self._native is not None else None
        )
        if events is None:
            yield make_event(EventKind.ERROR, text="session not found")
            return
        yield make_event(EventKind.SNAPSHOT, status="open", cursor=0)
        for event in events:
            yield {**event, "replay": True}
        yield make_event(EventKind.SNAPSHOT, status="idle", busy=False)

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
