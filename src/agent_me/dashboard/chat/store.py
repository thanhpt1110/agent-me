"""Session persistence behind a narrow Protocol.

``ChatService`` depends only on the :class:`SessionStore` interface (DIP +
ISP), so the disk implementation can be swapped for SQLite or an in-memory
fake (tests) without touching the orchestrator. Ported from the Node
prototype's ``sessions.js``.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import SessionMeta, now_ms


@runtime_checkable
class SessionStore(Protocol):
    def create(self, *, title: str | None, cwd: str, model: str | None) -> SessionMeta: ...
    def ensure(self, session_id: str, *, title: str | None, cwd: str, model: str | None = ...,
               claude_session_id: str | None = ..., origin: str = ...,
               slack_thread_ts: str | None = ...) -> SessionMeta: ...
    def get_meta(self, session_id: str) -> SessionMeta | None: ...
    def update_meta(self, session_id: str, **patch: object) -> SessionMeta | None: ...
    def list(self) -> list[SessionMeta]: ...
    def exists(self, session_id: str) -> bool: ...
    def append_event(self, session_id: str, event: dict) -> dict: ...
    def transcript(self, session_id: str) -> list[dict]: ...
    def transcript_since(self, session_id: str, offset: int) -> tuple[list[dict], int]: ...
    def tail(self, session_id: str, limit: int) -> tuple[list[dict], int]: ...
    def events_before(self, session_id: str, cursor: int, limit: int) -> tuple[list[dict], int]: ...
    def last_event(self, session_id: str) -> dict | None: ...
    def last_seq(self, session_id: str) -> int: ...
    def size(self, session_id: str) -> int: ...
    def remove(self, session_id: str) -> None: ...


class DiskSessionStore:
    """File-backed store: ``<data_dir>/<id>/{meta.json, transcript.jsonl}``.

    Append-only JSONL transcript is the catch-up source after a client
    reconnect and survives process restart.
    """

    def __init__(self, data_dir: Path) -> None:
        # Lazy: don't touch the filesystem at construction (import-time) — a bad
        # data dir must not take down the whole dashboard. Created on first write.
        self._dir = Path(data_dir)

    # -- paths ----------------------------------------------------------------
    def _meta_path(self, sid: str) -> Path:
        return self._dir / sid / "meta.json"

    def _transcript_path(self, sid: str) -> Path:
        return self._dir / sid / "transcript.jsonl"

    # -- CRUD -----------------------------------------------------------------
    def create(self, *, title: str | None, cwd: str, model: str | None) -> SessionMeta:
        sid = str(uuid.uuid4())
        (self._dir / sid).mkdir(parents=True, exist_ok=True)
        # Pin the claude session id up-front (== our id) so the first turn can
        # `--session-id` it and an interrupted first turn is still resumable.
        meta = SessionMeta(id=sid, title=(title or "New chat"), cwd=cwd, model=model,
                           claude_session_id=sid)
        self._write_meta(meta)
        self._transcript_path(sid).touch()
        return meta

    def ensure(self, sid: str, *, title: str | None, cwd: str, model: str | None = None,
               claude_session_id: str | None = None, origin: str = "web",
               slack_thread_ts: str | None = None) -> SessionMeta:
        """Get the session ``sid`` or create it with that exact id (idempotent).

        Used to mirror an externally-driven session (e.g. a Slack thread) into
        this store under a deterministic id so the dashboard lists/replays it.
        Unlike :meth:`create`, ``claude_session_id`` is stored verbatim (``None``
        until the external turn reports the real CLI id).
        """
        existing = self.get_meta(sid)
        if existing is not None:
            return existing
        (self._dir / sid).mkdir(parents=True, exist_ok=True)
        meta = SessionMeta(id=sid, title=(title or "New chat"), cwd=cwd, model=model,
                           claude_session_id=claude_session_id, origin=origin,
                           slack_thread_ts=slack_thread_ts)
        self._write_meta(meta)
        self._transcript_path(sid).touch()
        return meta

    def get_meta(self, sid: str) -> SessionMeta | None:
        try:
            return SessionMeta.from_dict(json.loads(self._meta_path(sid).read_text("utf-8")))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def update_meta(self, sid: str, **patch: object) -> SessionMeta | None:
        meta = self.get_meta(sid)
        if meta is None:
            return None
        for key, value in patch.items():
            setattr(meta, key, value)
        meta.last_activity = now_ms()
        self._write_meta(meta)
        return meta

    def list(self) -> list[SessionMeta]:
        metas = [m for sid in self._ids() if (m := self.get_meta(sid)) is not None]
        return sorted(metas, key=lambda m: m.last_activity, reverse=True)

    def exists(self, sid: str) -> bool:
        return self._meta_path(sid).exists()

    def append_event(self, sid: str, event: dict) -> dict:
        # Tolerate a session deleted mid-turn: don't recreate the dir, just skip.
        try:
            with self._transcript_path(sid).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except FileNotFoundError:
            pass
        return event

    def transcript(self, sid: str) -> list[dict]:
        try:
            lines = self._transcript_path(sid).read_text("utf-8").splitlines()
        except FileNotFoundError:
            return []
        out: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def transcript_since(self, sid: str, offset: int) -> tuple[list[dict], int]:
        """Events appended after byte ``offset`` + the new offset.

        Reads only the appended tail (not the whole file), so a live tail of a
        long transcript stays O(new bytes) per poll. A trailing partial line
        (mid-write by another process) is left unconsumed for the next read.
        """
        path = self._transcript_path(sid)
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except (FileNotFoundError, OSError):
            return [], offset
        nl = data.rfind(b"\n")
        if nl < 0:
            return [], offset                       # no complete line yet
        chunk = data[: nl + 1]                       # whole, newline-terminated lines
        new_offset = offset + len(chunk)             # exact byte advance
        out: list[dict] = []
        for raw in chunk.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw.decode("utf-8", "replace")))
            except json.JSONDecodeError:
                continue
        return out, new_offset

    def size(self, sid: str) -> int:
        try:
            return self._transcript_path(sid).stat().st_size
        except OSError:
            return 0

    def last_seq(self, sid: str) -> int:
        """The highest persisted seq (== event count) — read from the file tail,
        not the whole file, so seeding a new turn's seq is O(1) on a long session."""
        ev = self.last_event(sid)
        try:
            return int(ev.get("seq", 0)) if ev else 0
        except (TypeError, ValueError):
            return 0

    def tail(self, sid: str, limit: int) -> tuple[list[dict], int]:
        """The last ``limit`` events + a cursor (byte offset where the *older*
        history ends; 0 = none). Reads only the file tail, so opening a long
        session is O(limit), not O(file). ``limit<=0`` → all events, cursor 0."""
        size = self.size(sid)
        if size == 0:
            return [], 0
        if limit <= 0:
            return self.transcript(sid), 0
        lines, start = self._read_back(self._transcript_path(sid), size, limit)
        return self._parse_lines(lines), start

    def events_before(self, sid: str, cursor: int, limit: int) -> tuple[list[dict], int]:
        """``limit`` events ending just before byte ``cursor`` + the next cursor
        (0 at the start of history) — powers scroll-up paging."""
        if cursor <= 0 or limit <= 0:
            return [], 0
        lines, start = self._read_back(self._transcript_path(sid), cursor, limit)
        return self._parse_lines(lines), start

    @staticmethod
    def _parse_lines(lines: list[bytes]) -> list[dict]:
        out: list[dict] = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw.decode("utf-8", "replace")))
            except json.JSONDecodeError:
                continue
        return out

    @staticmethod
    def _read_back(path: Path, end: int, limit: int) -> tuple[list[bytes], int]:
        """Read backwards from byte ``end`` and return the last ``limit`` complete
        lines (forward order) + the byte offset where the first of them starts.

        ``end`` is always a line boundary for paging (a prior cursor), or the file
        size for the first tail; an unterminated trailing line just fails to parse
        and is dropped by ``_parse_lines``."""
        block = 65536
        pos = end
        data = b""
        try:
            with path.open("rb") as fh:
                while pos > 0 and data.count(b"\n") <= limit:
                    step = min(block, pos)
                    pos -= step
                    fh.seek(pos)
                    data = fh.read(step) + data
        except OSError:
            return [], 0
        segments = data.split(b"\n")
        drop_leading_partial = pos > 0  # we cut mid-line: the first segment is incomplete
        kept: list[tuple[bytes, int]] = []
        cur = pos
        last = len(segments) - 1
        for i, seg in enumerate(segments):
            seg_start = cur
            cur += len(seg) + (0 if i == last else 1)  # +1 for the consumed '\n'
            if i == 0 and drop_leading_partial:
                continue
            if seg.strip():
                kept.append((seg, seg_start))
        window = kept[-limit:] if limit and len(kept) > limit else kept
        if not window:
            return [], 0
        return [seg for seg, _ in window], window[0][1]

    def last_event(self, sid: str) -> dict | None:
        """The last persisted event, read from the file tail (no full parse).

        Cheap enough to call per-session on every list refresh: only the trailing
        few KB are read so a long transcript doesn't cost O(n) just to learn
        whether the most recent turn finished.
        """
        path = self._transcript_path(sid)
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 8192))
                data = fh.read().decode("utf-8", "replace")
        except (FileNotFoundError, OSError):
            return None
        for line in reversed(data.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    def remove(self, sid: str) -> None:
        import shutil

        shutil.rmtree(self._dir / sid, ignore_errors=True)

    # -- internals ------------------------------------------------------------
    def _ids(self) -> list[str]:
        if not self._dir.exists():
            return []
        return [p.name for p in self._dir.iterdir() if (p / "meta.json").exists()]

    def _write_meta(self, meta: SessionMeta) -> None:
        from dataclasses import asdict

        self._meta_path(meta.id).write_text(
            json.dumps(asdict(meta), ensure_ascii=False, indent=2), encoding="utf-8"
        )
