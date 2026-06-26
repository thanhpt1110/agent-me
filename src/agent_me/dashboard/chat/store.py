"""Session persistence behind a narrow Protocol.

``ChatService`` depends only on the :class:`SessionStore` interface (DIP +
ISP), so the disk implementation can be swapped for SQLite or an in-memory
fake (tests) without touching the orchestrator. Ported from the Node
prototype's ``sessions.js``.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import SessionMeta, now_ms


@runtime_checkable
class SessionStore(Protocol):
    def create(self, *, title: str | None, cwd: str, model: str | None) -> SessionMeta: ...
    def get_meta(self, session_id: str) -> SessionMeta | None: ...
    def update_meta(self, session_id: str, **patch: object) -> SessionMeta | None: ...
    def list(self) -> list[SessionMeta]: ...
    def exists(self, session_id: str) -> bool: ...
    def append_event(self, session_id: str, event: dict) -> dict: ...
    def transcript(self, session_id: str) -> list[dict]: ...
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
