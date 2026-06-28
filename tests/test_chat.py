"""Tests for the native dashboard chat feature.

Service-layer tests inject a ``FakeClaudeRunner`` (no ``claude`` binary needed),
exercising the SOLID seam (DIP). HTTP tests drive the real Starlette app.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from agent_me.dashboard.chat.config import ChatConfig
from agent_me.dashboard.chat.models import EventKind, TurnResult, make_event
from agent_me.dashboard.chat.service import ChatService
from agent_me.dashboard.chat.store import DiskSessionStore


class FakeClaudeRunner:
    """Yields scripted events instead of spawning the CLI."""

    def __init__(self, events: list[dict], session_id: str = "claude-xyz", **result_kwargs) -> None:
        self._events = events
        self._session_id = session_id
        self._result_kwargs = result_kwargs
        self.calls: list[dict] = []

    async def run_turn(self, *, prompt, cwd, session_id, resume, model, add_dirs, on_event,
                       append_system_prompt=None, **_kw) -> TurnResult:
        self.calls.append({"prompt": prompt, "resume": resume, "session_id": session_id, "cwd": cwd,
                           "append_system_prompt": append_system_prompt})
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


async def test_followups_queue_while_busy(tmp_path: Path) -> None:
    """Terminal-like: a 2nd message sent while a turn runs is queued (not rejected)
    and runs after the first, under a single turn_end."""
    svc, store, runner = _service(tmp_path)
    meta = svc.create_session(title=None, cwd=None, model=None)
    ok1, _ = await svc.start_turn(meta.id, "first")
    ok2, err2 = await svc.start_turn(meta.id, "second")  # while the first turn runs
    assert ok1 is True
    assert ok2 is True and err2 is None
    await _drain(svc, meta.id)

    transcript = store.transcript(meta.id)
    assert [e["text"] for e in transcript if e["kind"] == "user"] == ["first", "second"]
    assert sum(1 for e in transcript if e["kind"] == "turn_end") == 1
    assert store.get_meta(meta.id).turns == 2
    assert len(runner.calls) == 2 and runner.calls[1]["resume"] is True
    # the queued follow-up is flagged so the UI can mark it pending
    queued = [e for e in transcript if e["kind"] == "user" and e.get("queued")]
    assert [e["text"] for e in queued] == ["second"]


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

async def test_operating_context_passed_to_runner(tmp_path: Path) -> None:
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path),
                     keepalive_s=0.2, append_system_prompt="OP-CTX")
    store = DiskSessionStore(cfg.data_dir)
    runner = FakeClaudeRunner([make_event(EventKind.TEXT, text="hi")])
    svc = ChatService(store, runner, cfg)
    meta = svc.create_session(title=None, cwd=None, model=None)
    await svc.start_turn(meta.id, "do work")
    await _drain(svc, meta.id)
    assert runner.calls[0]["append_system_prompt"] == "OP-CTX"


def test_from_env_operating_context_default(monkeypatch) -> None:
    for k in ("CHAT_MODEL", "CHAT_APPEND_SYSTEM_PROMPT", "CHAT_DEFAULT_CWD"):
        monkeypatch.delenv(k, raising=False)
    cfg = ChatConfig.from_env("/home/thaphan/agent-me")
    assert cfg.model == "claude-opus-4-8"
    ctx = cfg.append_system_prompt or ""
    assert "big-update" in ctx and "GitLab" in ctx and "gh" in ctx
    # explicit empty value disables it
    monkeypatch.setenv("CHAT_APPEND_SYSTEM_PROMPT", "")
    assert ChatConfig.from_env("/x").append_system_prompt is None


def test_http_routes_and_chat_page() -> None:
    from agent_me.dashboard import app as dash
    client = TestClient(dash.app)

    r = client.get("/chat")
    assert r.status_code == 200
    assert "x-data='chat(" in r.text and "/api/sse/chat/" in r.text

    r = client.post("/api/chat/sessions", json={"title": "t", "cwd": "/tmp"})
    assert r.status_code == 201
    sid = r.json()["id"]

    assert any(s["id"] == sid for s in client.get("/api/chat/sessions").json()["sessions"])
    got = client.get(f"/api/chat/sessions/{sid}")
    assert got.status_code == 200 and got.json()["meta"]["cwd"] == "/tmp"

    assert client.post(f"/api/chat/sessions/{sid}/message", json={"text": ""}).status_code == 400
    assert client.get("/api/chat/sessions/does-not-exist").status_code == 404
    assert client.request("DELETE", f"/api/chat/sessions/{sid}").status_code == 200


# ── transcript replay cap (long sessions must not ship/render thousands of rows) ──

async def test_stream_caps_replayed_history(tmp_path: Path) -> None:
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path),
                     keepalive_s=0.2, replay_max=5)
    store = DiskSessionStore(cfg.data_dir)
    svc = ChatService(store, FakeClaudeRunner([]), cfg)
    meta = store.create(title="x", cwd=str(tmp_path), model=None)
    for i in range(20):
        store.append_event(meta.id, make_event(EventKind.TEXT, text=f"m{i}", seq=i + 1))

    got = [ev async for ev in svc.stream(meta.id)]
    texts = [e for e in got if e.get("kind") == EventKind.TEXT.value]
    assert [t["text"] for t in texts] == ["m15", "m16", "m17", "m18", "m19"]  # last 5 only
    assert got[-1]["kind"] == EventKind.SNAPSHOT.value  # idle, then close
    assert got[0]["kind"] == EventKind.SNAPSHOT.value and got[0].get("cursor", 0) > 0  # paging cursor


async def test_tail_and_scroll_up_paging(tmp_path: Path) -> None:
    store = DiskSessionStore(tmp_path / "cs")
    meta = store.create(title="x", cwd=str(tmp_path), model=None)
    for i in range(50):
        store.append_event(meta.id, make_event(EventKind.TEXT, text=f"m{i}", seq=i + 1))

    assert store.last_seq(meta.id) == 50
    tail, cursor = store.tail(meta.id, 10)
    assert [e["text"] for e in tail] == [f"m{i}" for i in range(40, 50)] and cursor > 0
    older, cursor2 = store.events_before(meta.id, cursor, 10)
    assert [e["text"] for e in older] == [f"m{i}" for i in range(30, 40)] and 0 < cursor2 < cursor

    # paging from the tail back to the start reconstructs the full transcript, in
    # order, with no gaps or duplicates.
    rebuilt, cur = list(tail), cursor
    while cur > 0:
        chunk, cur = store.events_before(meta.id, cur, 7)
        rebuilt = chunk + rebuilt
    assert [e["text"] for e in rebuilt] == [f"m{i}" for i in range(50)]


class GatedRunner:
    """A runner whose turn blocks on a gate so a turn can be observed mid-flight."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls: list[str] = []

    async def run_turn(self, *, prompt, cwd, session_id, resume, model, add_dirs, on_event,
                       append_system_prompt=None, **_kw) -> TurnResult:
        self.calls.append(prompt)
        on_event(make_event(EventKind.TEXT, text=f"live:{prompt}"))
        await self.gate.wait()
        return TurnResult(session_id=session_id, result="ok", is_error=False, exit_code=0)


async def test_send_on_old_session_streams_live_after_history(tmp_path: Path) -> None:
    """The reported bug: opening an old session and prompting again must stream the
    new turn. The replayed historical turn_end is tagged ``replay`` so the client
    doesn't mistake it for the live turn's end and close the stream."""
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    runner = GatedRunner()
    svc = ChatService(store, runner, cfg)
    meta = svc.create_session(title=None, cwd=None, model=None)

    runner.gate.set()
    await svc.start_turn(meta.id, "first")
    await _drain(svc, meta.id)
    runner.gate.clear()
    await svc.start_turn(meta.id, "second")            # a NEW turn now runs on the old session

    out: list[dict] = []

    async def reader() -> None:
        async for ev in svc.stream(meta.id):           # mirrors the browser's SSE handling
            out.append(ev)
            if ev["kind"] == "turn_end" and not ev.get("replay"):
                break

    task = asyncio.ensure_future(reader())
    await asyncio.sleep(0.02)
    runner.gate.set()
    await asyncio.wait_for(task, timeout=3)

    assert any(e["kind"] == "turn_end" and e.get("replay") for e in out)   # old turn_end tagged
    live = [e["text"] for e in out if e["kind"] == "text" and not e.get("replay")]
    assert any("second" in x for x in live)            # new prompt's live progress streamed
    assert out[-1]["kind"] == "turn_end" and not out[-1].get("replay")     # ends on the live turn
