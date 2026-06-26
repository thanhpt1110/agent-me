"""Tests for the native dashboard chat feature.

Service-layer tests inject a ``FakeClaudeRunner`` (no ``claude`` binary needed),
exercising the SOLID seam (DIP). HTTP tests drive the real Starlette app.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from agent_me.dashboard.chat.config import ChatConfig
from agent_me.dashboard.chat.models import TurnResult, make_event, EventKind
from agent_me.dashboard.chat.service import ChatService
from agent_me.dashboard.chat.store import DiskSessionStore


class FakeClaudeRunner:
    """Yields scripted events instead of spawning the CLI."""

    def __init__(self, events: list[dict], session_id: str = "claude-xyz", **result_kwargs) -> None:
        self._events = events
        self._session_id = session_id
        self._result_kwargs = result_kwargs
        self.calls: list[dict] = []

    async def run_turn(self, *, prompt, cwd, session_id, resume, model, add_dirs, on_event) -> TurnResult:
        self.calls.append({"prompt": prompt, "resume": resume, "session_id": session_id, "cwd": cwd})
        for ev in self._events:
            on_event(dict(ev))
        return TurnResult(
            session_id=self._session_id,
            result=self._result_kwargs.get("result", "ok"),
            is_error=self._result_kwargs.get("is_error", False),
            exit_code=self._result_kwargs.get("exit_code", 0),
            stderr=self._result_kwargs.get("stderr", ""),
        )


def _service(tmp_path: Path, events=None) -> tuple[ChatService, DiskSessionStore, FakeClaudeRunner]:
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    runner = FakeClaudeRunner(events or [make_event(EventKind.TEXT, text="hello")])
    return ChatService(store, runner, cfg), store, runner


async def _drain(svc: ChatService, sid: str) -> None:
    task = svc._tasks.get(sid)
    if task is not None:
        await task


# ── service ────────────────────────────────────────────────────────────────

async def test_turn_persists_streams_and_resumes(tmp_path: Path) -> None:
    svc, store, runner = _service(tmp_path, events=[
        make_event(EventKind.INIT, session_id="claude-xyz"),
        make_event(EventKind.TEXT, text="the answer"),
    ])
    meta = svc.create_session(title=None, cwd=None, model=None)
    ok, err = await svc.start_turn(meta.id, "hi there")
    assert ok and err is None
    await _drain(svc, meta.id)

    kinds = [e["kind"] for e in store.transcript(meta.id)]
    assert kinds[0] == "user" and "text" in kinds and kinds[-1] == "turn_end"
    # every persisted event carries a monotonic seq (client dedup)
    seqs = [e["seq"] for e in store.transcript(meta.id)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # auto-title from first message; resume id captured; busy cleared
    assert store.get_meta(meta.id).title == "hi there"
    assert store.get_meta(meta.id).claude_session_id == "claude-xyz"
    assert not svc.is_busy(meta.id)

    # a 2nd turn resumes the captured session id
    ok, _ = await svc.start_turn(meta.id, "again")
    await _drain(svc, meta.id)
    assert runner.calls[-1]["resume"] is True
    assert runner.calls[-1]["session_id"] == "claude-xyz"
    assert store.get_meta(meta.id).turns == 2


async def test_busy_lock_rejects_concurrent_turn(tmp_path: Path) -> None:
    svc, _, _ = _service(tmp_path)
    meta = svc.create_session(title=None, cwd=None, model=None)
    ok1, _ = await svc.start_turn(meta.id, "first")
    ok2, err2 = await svc.start_turn(meta.id, "second")  # before the task drains
    assert ok1 is True
    assert ok2 is False and err2 == "busy"
    await _drain(svc, meta.id)


async def test_validation_errors(tmp_path: Path) -> None:
    svc, _, _ = _service(tmp_path)
    meta = svc.create_session(title=None, cwd=None, model=None)
    assert await svc.start_turn("nope", "x") == (False, "not_found")
    assert await svc.start_turn(meta.id, "   ") == (False, "empty")


async def test_stream_replays_history_then_idle(tmp_path: Path) -> None:
    svc, _, _ = _service(tmp_path)
    meta = svc.create_session(title=None, cwd=None, model=None)
    await svc.start_turn(meta.id, "hi")
    await _drain(svc, meta.id)
    seen = [ev async for ev in svc.stream(meta.id)]
    kinds = [e["kind"] for e in seen]
    assert "user" in kinds and "text" in kinds and "turn_end" in kinds
    assert kinds[-1] == "snapshot" and seen[-1]["status"] == "idle"  # closes cleanly


async def test_delete_session(tmp_path: Path) -> None:
    svc, store, _ = _service(tmp_path)
    meta = svc.create_session(title=None, cwd=None, model=None)
    assert svc.delete_session(meta.id) is True
    assert store.exists(meta.id) is False
    assert svc.delete_session(meta.id) is False


async def test_first_turn_pins_session_id(tmp_path: Path) -> None:
    # claude_session_id is pinned at creation (== our id) so an interrupted
    # first turn is still resumable; the first run passes it (not --resume).
    svc, store, runner = _service(tmp_path)
    meta = svc.create_session(title=None, cwd=None, model=None)
    assert store.get_meta(meta.id).claude_session_id == meta.id
    await svc.start_turn(meta.id, "hi")
    await _drain(svc, meta.id)
    assert runner.calls[0]["resume"] is False
    assert runner.calls[0]["session_id"] == meta.id


async def test_cli_error_is_surfaced(tmp_path: Path) -> None:
    # A non-zero claude exit with no result line must produce a visible error,
    # not a silently-empty turn.
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    runner = FakeClaudeRunner([], is_error=True, exit_code=1, stderr="boom\nauth expired")
    svc = ChatService(store, runner, cfg)
    meta = svc.create_session(title=None, cwd=None, model=None)
    await svc.start_turn(meta.id, "hi")
    await _drain(svc, meta.id)
    errors = [e for e in store.transcript(meta.id) if e["kind"] == "error"]
    assert errors and "code 1" in errors[0]["text"] and "auth expired" in errors[0]["text"]


def test_http_malformed_json_returns_400() -> None:
    from agent_me.dashboard import app as dash
    client = TestClient(dash.app)
    r = client.post("/api/chat/sessions", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


# ── HTTP (real app, auth disabled when no DASHBOARD_TOKEN) ────────────────────

def test_http_routes_and_chat_page() -> None:
    from agent_me.dashboard import app as dash
    client = TestClient(dash.app)

    r = client.get("/chat")
    assert r.status_code == 200
    assert 'x-data="chat(' in r.text and "/api/sse/chat/" in r.text

    r = client.post("/api/chat/sessions", json={"title": "t", "cwd": "/tmp"})
    assert r.status_code == 201
    sid = r.json()["id"]

    assert any(s["id"] == sid for s in client.get("/api/chat/sessions").json()["sessions"])
    got = client.get(f"/api/chat/sessions/{sid}")
    assert got.status_code == 200 and got.json()["meta"]["cwd"] == "/tmp"

    assert client.post(f"/api/chat/sessions/{sid}/message", json={"text": ""}).status_code == 400
    assert client.get("/api/chat/sessions/does-not-exist").status_code == 404
    assert client.request("DELETE", f"/api/chat/sessions/{sid}").status_code == 200
