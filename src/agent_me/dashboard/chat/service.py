"""Chat orchestration — the only object the route handlers touch.

Coordinates the injected :class:`SessionStore`, :class:`ClaudeRunner` and
:class:`ChatConfig` (constructor injection → fully unit-testable). Holds no
HTTP or I/O detail itself (SRP); a turn runs server-side in an ``asyncio.Task``
so it survives client disconnect, while every event is both persisted (catch-up
source) and fanned out live via a :class:`ChatTurnJob`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from .commands import discover_commands
from .config import ChatConfig
from .local_sessions import purge_local_sessions
from .models import EventKind, SessionMeta, make_event, now_ms
from .native_sessions import NativeSessionReader
from .runner import ClaudeRunner
from .store import SessionStore, pid_alive

log = logging.getLogger("agent_me.dashboard.chat")

_TURN_WORKER_MODULE = "agent_me.dashboard.chat.turn_runner"
_TAIL_POLL_S = 0.25   # how often a connected client polls the transcript for new events


class ChatService:
    def __init__(self, store: SessionStore, runner: ClaudeRunner, config: ChatConfig,
                 native: NativeSessionReader | None = None,
                 launch: Callable[[str], None] | None = None) -> None:
        self._store = store
        self._runner = runner
        self._config = config
        self._native = native
        # Turns run in a DETACHED worker process (own process group), so a dashboard
        # restart doesn't kill them; the dashboard only appends to the queue + tails
        # the transcript. ``launch`` is injectable so tests run the worker in-process.
        self._launch = launch or self._spawn_worker

    @property
    def config(self) -> ChatConfig:
        return self._config

    # -- queries --------------------------------------------------------------
    def list_sessions(self) -> list[SessionMeta]:
        return self._store.list()

    def _running(self, sid: str) -> bool:
        """A detached worker is actively draining this session (pid alive)."""
        meta = self._store.get_meta(sid)
        return bool(meta and pid_alive(meta.running_pid))

    def is_busy(self, sid: str) -> bool:
        return self._running(sid) or self._external_busy(sid)

    def _live_elsewhere(self, meta: SessionMeta | None) -> bool:
        """True if this session's CLI id is currently held by a live ``claude``
        process we don't own (e.g. open in a terminal). Driving it from the web
        would ``--resume`` a session another process owns → conflict/hang/fork."""
        if self._native is None or meta is None:
            return False
        live = self._native.live_ids()
        return any(s in live for s in (meta.id, meta.claude_session_id, *meta.claude_session_ids) if s)

    def _external_busy(self, sid: str) -> bool:
        """A mirrored (Slack) session is "running" while its last event isn't a
        ``turn_end`` and it was touched recently — the window bounds a turn that
        crashed without a terminal event so it doesn't show busy forever. (Web
        turns use ``running_pid`` instead; see :meth:`_running`.)"""
        meta = self._store.get_meta(sid)
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
        chosen = (cwd or "").strip() or self._config.default_cwd
        if not Path(chosen).expanduser().is_dir():  # never spawn into a missing dir
            chosen = self._config.default_cwd
        return self._store.create(
            title=title, cwd=str(Path(chosen).expanduser()), model=(model or self._config.model),
        )

    def folder_options(self) -> dict:
        """Candidate working directories for a new chat: the repo, configured
        add-dirs, $HOME, and sibling git repos under $HOME. The agent always
        spawns (the runner passes ``--dangerously-skip-permissions``, which skips
        the CLI's workspace-trust dialog), so any existing dir is safe to offer."""
        out: list[str] = []

        def add(p: str | None) -> None:
            if not p:
                return
            try:
                rp = str(Path(p).expanduser())
            except (OSError, RuntimeError):
                return
            if rp not in out and Path(rp).is_dir():
                out.append(rp)

        add(self._config.default_cwd)
        for d in self._config.add_dirs:
            add(d)
        home = Path.home()
        add(str(home))
        try:
            for child in sorted(home.iterdir()):
                if child.is_dir() and not child.name.startswith(".") and (child / ".git").is_dir():
                    add(str(child))
        except OSError:
            pass
        default = str(Path(self._config.default_cwd).expanduser()) if self._config.default_cwd else str(home)
        return {"folders": out[:50], "default": default}

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

    def folder_locked(self, meta: SessionMeta) -> bool:
        """A session's cwd is fixed once it has started — the CLI ``--resume`` is
        pinned to that dir, and a running/terminal-held session can't be moved."""
        return bool(
            meta.claude_started or meta.turns > 0
            or self._running(meta.id) or self._live_elsewhere(meta)
        )

    def update_cwd(self, sid: str, cwd: str) -> dict | None:
        """Change an UNSTARTED session's working dir (the folder picker). Refused
        once the session has started so an in-flight chat can't change folder."""
        meta = self._store.get_meta(sid)
        if meta is None or self.folder_locked(meta):
            return None
        chosen = (cwd or "").strip()
        if not chosen or not Path(chosen).expanduser().is_dir():
            return None
        updated = self._store.update_meta(sid, cwd=str(Path(chosen).expanduser()))
        return updated.public_dict(busy=self.is_busy(sid)) if updated else None

    def delete_session(self, sid: str) -> bool:
        meta = self._store.get_meta(sid)
        if meta is None:
            return self._delete_native(sid)
        self._terminate_worker(meta.running_pid)  # stop the detached worker (+ its claude child)
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

        The message is appended to the durable queue and a DETACHED worker process
        drains it (or, if one is already running, picks it up) — so the user is never
        blocked AND a dashboard restart can't kill the turn. Only an external (Slack)
        in-flight turn, or a session live in a terminal, is refused."""
        meta = self._store.get_meta(sid)
        if meta is None:
            meta, err = self._adopt_native(sid)
            if meta is None:
                return False, err
        text = (text or "").strip()
        if not text:
            return False, "empty"
        if self._external_busy(sid):              # a Slack turn owns the stream elsewhere
            return False, "busy"
        if not self._running(sid) and self._live_elsewhere(meta):  # open in a terminal → read-only
            return False, "live"

        self._store.append_pending(sid, text)     # durable: survives a restart
        if self._store.claim_run(sid):            # nobody draining → launch a worker
            self._launch(sid)
        return True, None

    def _spawn_worker(self, sid: str) -> None:
        """Launch the detached turn-runner in its OWN process group (so a dashboard
        restart — with ``KillMode=process`` — leaves it running). It inherits this
        process's env (claude OAuth, PATH, the loaded .env)."""
        repo = self._config.default_cwd or str(Path.cwd())
        out: object = subprocess.DEVNULL
        with contextlib.suppress(OSError):
            logp = Path(self._config.data_dir) / sid / "worker.log"
            logp.parent.mkdir(parents=True, exist_ok=True)
            out = logp.open("ab")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", _TURN_WORKER_MODULE, sid],
                cwd=repo, env={**os.environ}, stdin=subprocess.DEVNULL,
                stdout=out, stderr=out, start_new_session=True,
            )
            # Record the REAL worker pid (claim_run only set a placeholder), so
            # ``_running`` reflects the worker — and clears when it dies.
            self._store.update_meta(sid, running_pid=proc.pid)
        except (OSError, ValueError):
            log.exception("failed to spawn chat turn worker for %s", sid)
            self._store.update_meta(sid, running_pid=0)   # release the claim so a retry can launch
        finally:
            if out is not subprocess.DEVNULL:
                with contextlib.suppress(Exception):
                    out.close()

    @staticmethod
    def _terminate_worker(pid: int) -> None:
        """SIGTERM the worker's whole process group (worker + its claude child)."""
        if not pid_alive(pid):
            return
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)

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
        """Stop the in-flight turn: SIGTERM the worker's process group (so its claude
        child dies too) and clear the queue so the cancelled message doesn't resume.
        The worker emits ``turn_end`` on its way out so tailing clients close."""
        meta = self._store.get_meta(sid)
        pid = meta.running_pid if meta else 0
        running = pid_alive(pid)
        if running:
            self._terminate_worker(pid)
        self._store.update_meta(sid, pending=[], pending_attempts=0)
        return running

    def resume_pending(self) -> int:
        """At startup, relaunch a worker for any session that still has queued work
        but no live worker (its worker died with the previous process). A worker that
        SURVIVED the restart is left alone — the dashboard just tails it. Returns how
        many workers were launched."""
        resumed = 0
        for meta in self._store.list():
            if not meta.pending or meta.origin == "slack":
                continue
            if self._running(meta.id) or self._live_elsewhere(meta):
                continue
            if self._store.claim_run(meta.id):
                self._launch(meta.id)
                resumed += 1
        if resumed:
            log.info("relaunched %d session worker(s) with pending messages", resumed)
        return resumed

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
        live = self._running(sid) or self._external_busy(sid)
        events, cursor = self._store.tail(sid, cap)
        yield make_event(EventKind.SNAPSHOT, status=("running" if live else "open"), cursor=cursor)
        for event in events:
            yield {**event, "replay": True}    # history: a past turn_end must NOT close the live stream
        # A turn runs in a detached worker writing this transcript; tail it (the
        # dashboard can restart and a reconnecting client just re-tails). When no
        # worker is live, go idle.
        if live:
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
        """Poll the on-disk transcript for events appended by the detached worker
        (or a Slack turn), yielding only the newly-appended tail (by byte offset)
        until ``turn_end`` or the worker is gone and the turn has fallen quiet."""
        idle = 0.0
        while True:
            await asyncio.sleep(_TAIL_POLL_S)
            events, offset = self._store.transcript_since(sid, offset)
            if events:
                for event in events:
                    yield event
                    if event.get("kind") == EventKind.TURN_END.value:
                        return
                idle = 0.0
                continue
            if not self._running(sid) and not self._external_busy(sid):
                yield make_event(EventKind.SNAPSHOT, status="idle", busy=False)
                return
            idle += _TAIL_POLL_S
            if idle >= self._config.keepalive_s:      # periodic keepalive while still running
                yield make_event(EventKind.TIMEOUT, note="tail-keepalive")
                idle = 0.0
