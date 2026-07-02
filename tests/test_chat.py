"""Tests for the native dashboard chat feature.

Service-layer tests inject a ``FakeClaudeRunner`` (no ``claude`` binary needed),
exercising the SOLID seam (DIP). HTTP tests drive the real Starlette app.
"""
from __future__ import annotations

import asyncio
import subprocess
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
            context_tokens=self._result_kwargs.get("context_tokens", 0),
        )


def _wire(store, runner, cfg, native=None) -> ChatService:
    """Build a ChatService whose turn-worker runs IN-PROCESS (no detached subprocess),
    so tests drive the real TurnExecutor + queue/resume logic with a FakeRunner."""
    from agent_me.dashboard.chat.turn_runner import TurnExecutor
    tasks: dict[str, asyncio.Task] = {}

    def launch(sid: str) -> None:
        tasks[sid] = asyncio.ensure_future(TurnExecutor(store, runner, cfg).run(sid))

    svc = ChatService(store, runner, cfg, native=native, launch=launch)
    svc._test_tasks = tasks  # type: ignore[attr-defined]
    return svc


def _service(tmp_path: Path, events=None) -> tuple[ChatService, DiskSessionStore, FakeClaudeRunner]:
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    runner = FakeClaudeRunner(events or [make_event(EventKind.TEXT, text="hello")])
    return _wire(store, runner, cfg), store, runner


async def _drain(svc: ChatService, sid: str) -> None:
    task = getattr(svc, "_test_tasks", {}).get(sid)
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


async def test_context_tokens_tracked_and_exposed(tmp_path: Path) -> None:
    """A turn's context-window usage is persisted on the meta and surfaced in the
    API payload so the web can render the % gauge; a 0 (killed turn) never clobbers
    a prior reading."""
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    runner = FakeClaudeRunner([make_event(EventKind.TEXT, text="hi")], context_tokens=250_000)
    svc = _wire(store, runner, cfg)
    meta = svc.create_session(title=None, cwd=None, model=None)
    await svc.start_turn(meta.id, "hello")
    await _drain(svc, meta.id)

    assert store.get_meta(meta.id).context_tokens == 250_000
    payload = next(d for d in svc.session_dicts() if d["id"] == meta.id)
    assert payload["context_tokens"] == 250_000

    runner._result_kwargs["context_tokens"] = 0   # a killed/empty turn reports 0
    await svc.start_turn(meta.id, "again")
    await _drain(svc, meta.id)
    assert store.get_meta(meta.id).context_tokens == 250_000  # prior reading kept


async def test_update_cwd_locked_after_start(tmp_path: Path) -> None:
    """The folder picker can move a brand-new chat, but the cwd is frozen once the
    chat has started (its claude --resume cwd is pinned)."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(a),
                     add_dirs=(str(tmp_path),), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    svc = _wire(store, FakeClaudeRunner([make_event(EventKind.TEXT, text="hi")]), cfg)
    meta = svc.create_session(title=None, cwd=str(a), model=None)

    assert svc.folder_locked(store.get_meta(meta.id)) is False
    out = svc.update_cwd(meta.id, str(b))                 # unstarted → allowed
    assert out is not None and out["cwd"] == str(b)
    assert svc.update_cwd(meta.id, str(tmp_path / "missing")) is None  # invalid dir refused

    await svc.start_turn(meta.id, "go")
    await _drain(svc, meta.id)
    assert svc.folder_locked(store.get_meta(meta.id)) is True
    assert svc.update_cwd(meta.id, str(a)) is None        # started → locked
    assert store.get_meta(meta.id).cwd == str(b)          # unchanged


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    def run(*a: str) -> None:
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@e.x")
    run("config", "user.name", "t")
    (repo / "f").write_text("x")
    run("add", "-A")
    run("commit", "-qm", "init")
    run("branch", "feature")


def _head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


async def test_branch_options_and_checkout(tmp_path: Path) -> None:
    """The branch picker lists a folder's local branches and checks one out on an
    unstarted, dedicated chat; it's refused for a non-git folder, an unknown
    branch, and a started chat (whose cwd/branch are pinned to --resume)."""
    repo, plain = tmp_path / "repo", tmp_path / "plain"
    _git_init(repo)
    plain.mkdir()
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    svc = _wire(store, FakeClaudeRunner([make_event(EventKind.TEXT, text="hi")]), cfg)

    assert svc.branch_options(str(plain)) == {"is_git": False, "branches": [], "current": ""}
    info = svc.branch_options(str(repo))
    assert info["is_git"] is True and info["current"] == "main"
    assert set(info["branches"]) == {"main", "feature"}

    meta = svc.create_session(title=None, cwd=str(repo), model=None)
    assert svc.checkout_branch(meta.id, "nope") is None          # unknown branch refused
    out = svc.checkout_branch(meta.id, "feature")                # unstarted, dedicated → checked out
    assert out is not None and out["current"] == "feature"
    assert _head(repo) == "feature"

    await svc.start_turn(meta.id, "go")
    await _drain(svc, meta.id)
    assert svc.checkout_branch(meta.id, "main") is None          # started → locked
    assert _head(repo) == "feature"


def test_branch_options_handles_unborn_branch(tmp_path: Path) -> None:
    """A freshly-init'd repo with no commits reports its real (unborn) branch name,
    not a misleading 'HEAD'."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True,
                   capture_output=True, text=True)
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    svc = _wire(DiskSessionStore(cfg.data_dir), FakeClaudeRunner([]), cfg)
    info = svc.branch_options(str(repo))
    assert info["is_git"] is True and info["current"] == "main"


async def test_branch_checkout_refused_for_shared_or_own_tree(tmp_path: Path) -> None:
    """A checkout mutates the whole working tree, so it's refused for the agent's
    own repo (default_cwd) and for any folder another session is pinned to."""
    own = tmp_path / "own"
    _git_init(own)
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(own),
                     add_dirs=(str(tmp_path),), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    svc = _wire(store, FakeClaudeRunner([make_event(EventKind.TEXT, text="hi")]), cfg)

    s_own = svc.create_session(title=None, cwd=str(own), model=None)
    assert svc.branch_options(str(own))["is_git"] is True        # still listed (read-only)
    assert svc.checkout_branch(s_own.id, "feature") is None       # own/default tree → refused
    assert _head(own) == "main"

    dedi = tmp_path / "dedi"
    _git_init(dedi)
    a = svc.create_session(title=None, cwd=str(dedi), model=None)
    assert svc.checkout_branch(a.id, "feature") is not None       # sole user → allowed
    subprocess.run(["git", "-C", str(dedi), "checkout", "-q", "main"], check=True)
    svc.create_session(title=None, cwd=str(dedi), model=None)     # a second chat shares the folder
    assert svc.checkout_branch(a.id, "feature") is None           # now shared → refused
    assert _head(dedi) == "main"


async def test_branch_checkout_refused_for_alias_of_own_tree(tmp_path: Path) -> None:
    """Path identity for the checkout guard is canonical, not string equality: a
    symlink-alias spelling of default_cwd is still the live dashboard tree. New
    sessions store a canonical cwd; legacy metas with an alias spelling hit the
    same guard via the canonical compare."""
    own = tmp_path / "own"
    _git_init(own)
    alias = tmp_path / "own-alias"
    alias.symlink_to(own, target_is_directory=True)
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(own),
                     add_dirs=(str(tmp_path),), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    svc = _wire(store, FakeClaudeRunner([]), cfg)
    s = svc.create_session(title=None, cwd=str(alias), model=None)
    assert s.cwd == str(own.resolve())                          # cwd stored canonical
    assert svc.checkout_branch(s.id, "feature") is None         # own tree via alias → refused
    assert _head(own) == "main"
    legacy = store.create(title=None, cwd=str(alias), model=None)  # pre-canon meta on disk
    assert svc._branch_switch_allowed(legacy) is False


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
    svc = _wire(store, runner, cfg)
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
    svc = _wire(store, runner, cfg)
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

    home = str(Path.home())                          # an in-scope cwd ($HOME subtree)
    r = client.post("/api/chat/sessions", json={"title": "t", "cwd": home})
    assert r.status_code == 201
    sid = r.json()["id"]

    assert any(s["id"] == sid for s in client.get("/api/chat/sessions").json()["sessions"])
    got = client.get(f"/api/chat/sessions/{sid}")
    assert got.status_code == 200 and got.json()["meta"]["cwd"] == home

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


async def test_signal_exit_keeps_pending_for_resume(tmp_path: Path) -> None:
    """A SIGTERM exit (143 — e.g. the dashboard restarted mid-turn) is NOT a claude
    error: it surfaces as a soft "interrupted, resumes automatically" note (no scary
    code), and the un-finished + queued messages stay in ``meta.pending`` so they can
    auto-resume — the user never has to re-type them."""
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    runner = FakeClaudeRunner([], is_error=True, exit_code=143)
    svc = _wire(store, runner, cfg)
    meta = svc.create_session(title=None, cwd=None, model=None)
    await svc.start_turn(meta.id, "first")
    await svc.start_turn(meta.id, "second")   # queued while the first runs
    await _drain(svc, meta.id)

    m = store.get_meta(meta.id)
    errs = [e for e in store.transcript(meta.id) if e["kind"] == "error"]
    assert errs and "interrupt" in errs[0]["text"].lower() and "143" not in errs[0]["text"]
    assert m.claude_started is True                 # still resumable
    assert m.pending == ["first", "second"]         # both kept for auto-resume
    assert m.pending_attempts == 1


async def test_pending_auto_resumes_after_restart(tmp_path: Path) -> None:
    """A message left pending by a restart auto-resumes: a fresh service (same store)
    re-runs it on startup via ``resume_pending`` and clears it once it completes."""
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    # round 1: the turn is signal-interrupted, leaving the message pending
    svc1 = _wire(store, FakeClaudeRunner([], is_error=True, exit_code=143), cfg)
    meta = svc1.create_session(title=None, cwd=None, model=None)
    await svc1.start_turn(meta.id, "do the thing")
    await _drain(svc1, meta.id)
    assert store.get_meta(meta.id).pending == ["do the thing"]

    # round 2: "restart" — a new service on the same store resumes + finishes it
    runner2 = FakeClaudeRunner([make_event(EventKind.TEXT, text="done")])
    svc2 = _wire(store, runner2, cfg)
    assert svc2.resume_pending() == 1
    await _drain(svc2, meta.id)
    assert store.get_meta(meta.id).pending == []        # cleared after completion
    assert runner2.calls and runner2.calls[0]["resume"] is True
    assert any(e["kind"] == "text" and e["text"] == "done" for e in store.transcript(meta.id))


def test_folder_options_lists_default_and_dirs(tmp_path: Path) -> None:
    sub = tmp_path / "proj"
    sub.mkdir()
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), add_dirs=(str(sub),))
    svc = ChatService(DiskSessionStore(cfg.data_dir), FakeClaudeRunner([]), cfg)
    opts = svc.folder_options()
    assert opts["default"] == str(tmp_path)
    assert str(tmp_path) in opts["folders"] and str(sub) in opts["folders"]


def test_create_session_validates_cwd(tmp_path: Path) -> None:
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path))
    svc = ChatService(DiskSessionStore(cfg.data_dir), FakeClaudeRunner([]), cfg)
    # a missing folder falls back to the default (never spawn into a dead cwd);
    # the stored cwd is canonical (symlink-resolved)
    assert (svc.create_session(title=None, cwd="/no/such/dir/zzz", model=None).cwd
            == str(tmp_path.resolve()))
    sub = tmp_path / "ok"
    sub.mkdir()
    assert svc.create_session(title=None, cwd=str(sub), model=None).cwd == str(sub.resolve())
    # an out-of-scope dir (outside $HOME / the configured roots) also falls back
    assert (svc.create_session(title=None, cwd="/etc", model=None).cwd
            == str(tmp_path.resolve()))


def test_folder_options_lists_all_nonhidden_home_dirs(tmp_path: Path, monkeypatch) -> None:
    """The folder picker offers EVERY non-hidden folder under $HOME (the dynamic
    fix), not just git repos, and excludes dotfile dirs and non-directories."""
    home = tmp_path / "home"
    (home / "proj-git" / ".git").mkdir(parents=True)
    (home / "plain-dir").mkdir(parents=True)       # non-git → must still be offered
    (home / ".hidden").mkdir()                      # dotfile dir → excluded
    (home / "note.txt").write_text("x")             # not a dir → excluded
    monkeypatch.setenv("HOME", str(home))           # drives Path.home()
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(home / "proj-git"))
    svc = ChatService(DiskSessionStore(cfg.data_dir), FakeClaudeRunner([]), cfg)
    folders = svc.folder_options()["folders"]
    names = {Path(f).name for f in folders}
    assert {"proj-git", "plain-dir"} <= names       # both git and non-git folders
    assert str(home) in folders                      # $HOME itself
    assert ".hidden" not in names and "note.txt" not in names


def test_folder_options_dedup_symlink_alias_and_add_dir_children(tmp_path: Path, monkeypatch) -> None:
    """A $HOME symlink into the real workspace dedups against the real path
    (offered once, first spelling wins), and non-hidden children of every
    add-dir are offered too — so sibling repos on another disk show up."""
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data" / "workspace"
    (data / "agent-me").mkdir(parents=True)
    (data / "devtest-wrapper").mkdir()
    (data / ".hidden").mkdir()
    (home / "agent-me").symlink_to(data / "agent-me", target_is_directory=True)
    outside = tmp_path / "outside"                  # resolves under NO scope root
    outside.mkdir()
    (home / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))           # drives Path.home()
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(data / "agent-me"),
                     add_dirs=(str(data),))
    svc = ChatService(DiskSessionStore(cfg.data_dir), FakeClaudeRunner([]), cfg)
    folders = svc.folder_options()["folders"]
    assert folders.count(str(data / "agent-me")) == 1     # real path offered once...
    assert str(home / "agent-me") not in folders          # ...the symlink alias dedup'd
    assert str(data) in folders                           # the add-dir itself
    assert str(data / "devtest-wrapper") in folders       # add-dir children offered
    assert not any(Path(f).name == ".hidden" for f in folders)
    # the picker only offers what create_session will accept: a $HOME child
    # symlinking outside every scope root is not offered (it isn't in scope)
    assert str(home / "escape") not in folders
    assert not svc._allowed_cwd(str(home / "escape"))


def test_folder_options_survives_unresolvable_home(tmp_path: Path, monkeypatch) -> None:
    """No $HOME + no passwd entry makes Path.home() raise; the picker must degrade to
    the configured roots, not 500 (the scope helpers already guard this)."""
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path))
    svc = ChatService(DiskSessionStore(cfg.data_dir), FakeClaudeRunner([]), cfg)

    def boom() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(boom))
    out = svc.folder_options()                        # must not raise
    assert out["default"] == str(tmp_path) and str(tmp_path) in out["folders"]


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


async def test_old_session_new_turn_streams_via_tail(tmp_path: Path) -> None:
    """Opening an old session and prompting again must stream the new turn by tailing
    the (detached-worker-written) transcript: the replayed historical turn_end is
    tagged ``replay`` so it doesn't close the stream, and the live turn_end ends it."""
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    store = DiskSessionStore(cfg.data_dir)
    runner = GatedRunner()
    svc = _wire(store, runner, cfg)
    meta = svc.create_session(title=None, cwd=None, model=None)

    runner.gate.set()
    await svc.start_turn(meta.id, "first")
    await _drain(svc, meta.id)                          # history now has a turn_end
    runner.gate.clear()
    await svc.start_turn(meta.id, "second")            # a NEW turn now runs (gated) on the old session

    out: list[dict] = []

    async def reader() -> None:
        async for ev in svc.stream(meta.id):           # mirrors the browser's SSE handling
            out.append(ev)
            if ev["kind"] == "turn_end" and not ev.get("replay"):
                break

    task = asyncio.ensure_future(reader())
    await asyncio.sleep(0.05)
    runner.gate.set()
    await asyncio.wait_for(task, timeout=5)
    await _drain(svc, meta.id)

    assert any(e["kind"] == "turn_end" and e.get("replay") for e in out)        # old turn_end tagged
    assert any("live:second" in (e.get("text") or "") for e in out)            # new turn's progress shown
    assert out[-1]["kind"] == "turn_end" and not out[-1].get("replay")          # ends on the live turn
    assert store.get_meta(meta.id).pending == []                                # completed, queue drained
