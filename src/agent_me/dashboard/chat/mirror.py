"""Mirror an externally-driven agent turn into the web chat's on-disk store.

The Slack bridge runs the same ``claude`` engine in the same agent-me workspace
as the dashboard /chat does, but in a separate process. ``WebSessionMirror``
writes each Slack turn's events into the dashboard's :class:`DiskSessionStore`
under a deterministic, per-thread id — so the conversation shows up in the web
chat list and replays exactly like a session started in the browser. Because the
mirrored ``claude_session_id`` is the real CLI id, the user can even continue a
Slack thread from the web (it resumes the same session).

Single-writer model: only the bridge process writes a given Slack session, so a
plain in-memory ``seq`` counter (seeded from the transcript length) is enough to
keep the monotonic ``seq`` the web client dedups on.
"""
from __future__ import annotations

import re

from .models import EventKind, make_event
from .store import DiskSessionStore

# Raw engine events worth replaying in the web transcript (the USER prompt and
# TURN_END are emitted by the mirror itself; INIT/RESULT/TIMEOUT are internal).
_MIRRORED_KINDS = frozenset({
    EventKind.TEXT.value,
    EventKind.THINKING.value,
    EventKind.TOOL_USE.value,
    EventKind.TOOL_RESULT.value,
    EventKind.ERROR.value,
})


def web_session_id_for(key: str) -> str:
    """A stable, filesystem-safe web-session id for an external key (Slack ts)."""
    safe = re.sub(r"[^A-Za-z0-9]+", "-", str(key)).strip("-")
    return f"slack-{safe}" if safe else "slack-session"


class WebSessionMirror:
    def __init__(self, store: DiskSessionStore) -> None:
        self._store = store
        self._seq: dict[str, int] = {}

    def begin_turn(self, *, key: str, title: str | None, cwd: str, user_text: str,
                   model: str | None = None) -> str:
        """Ensure the mirrored session exists and record the user's message.

        Returns the web-session id. Reuses the same session across a thread's
        turns so context (and the chat history) accumulates like a real chat.
        """
        sid = web_session_id_for(key)
        self._store.ensure(
            sid, title=(title or "Slack chat")[:60] or "Slack chat", cwd=cwd,
            model=model, origin="slack", slack_thread_ts=str(key),
        )
        self._seq.setdefault(sid, self._store.last_seq(sid))
        self._emit(sid, make_event(EventKind.USER, text=user_text))
        return sid

    def tap(self, sid: str):
        """An ``on_event`` callback that mirrors raw engine events into ``sid``."""
        def on_event(evt: dict) -> None:
            if evt.get("kind") in _MIRRORED_KINDS:
                self._emit(sid, dict(evt))
        return on_event

    def end_turn(self, sid: str, *, claude_session_id: str | None = None,
                 error: str | None = None) -> None:
        """Close the turn: pin the resumable CLI id, then emit ``turn_end``."""
        meta = self._store.get_meta(sid)
        patch: dict[str, object] = {"claude_started": True}
        if meta is not None:
            patch["turns"] = meta.turns + 1
        if claude_session_id:
            seen = [s for s in [
                *(meta.claude_session_ids if meta else []),
                (meta.claude_session_id if meta else None),
                claude_session_id,
            ] if s]
            patch["claude_session_id"] = claude_session_id
            patch["claude_session_ids"] = list(dict.fromkeys(seen))
        self._store.update_meta(sid, **patch)
        if error:
            self._emit(sid, make_event(EventKind.ERROR, text=str(error)[:500]))
        self._emit(sid, make_event(EventKind.TURN_END))

    def _emit(self, sid: str, event: dict) -> dict:
        self._seq[sid] = self._seq.get(sid, 0) + 1
        event["seq"] = self._seq[sid]
        self._store.append_event(sid, event)
        return event
