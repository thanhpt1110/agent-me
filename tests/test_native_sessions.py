"""Tests for native CLI session sync (web ↔ ``claude --resume``).

The reader parses the ``claude`` CLI's own ``~/.claude/projects/<slug>/<id>.jsonl``
store; the service merges those native sessions into the web list, replays them
read-only, and adopts one (importing its transcript) when continued from the web.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from agent_me.dashboard.chat.config import ChatConfig
from agent_me.dashboard.chat.local_sessions import project_dir_name
from agent_me.dashboard.chat.models import EventKind, TurnResult, make_event
from agent_me.dashboard.chat.native_sessions import NativeSessionReader
from agent_me.dashboard.chat.service import ChatService
from agent_me.dashboard.chat.store import DiskSessionStore
from agent_me.dashboard.chat.turn_runner import TurnExecutor

CWD = "/home/thaphan/agent-me"


class FakeClaudeRunner:
    def __init__(self, events: list[dict], session_id: str = "claude-xyz") -> None:
        self._events = events
        self._session_id = session_id
        self.calls: list[dict] = []

    async def run_turn(self, *, prompt, cwd, session_id, resume, model, add_dirs, on_event,
                       append_system_prompt=None, **_kw) -> TurnResult:
        self.calls.append({"prompt": prompt, "resume": resume, "session_id": session_id, "cwd": cwd})
        for ev in self._events:
            on_event(dict(ev))
        return TurnResult(session_id=self._session_id, result="ok", is_error=False, exit_code=0)


def _u(text, ts="2026-06-27T12:00:00.000Z", entry="cli", sidechain=False, cwd=CWD):
    return {"type": "user", "timestamp": ts, "entrypoint": entry, "isSidechain": sidechain,
            "cwd": cwd, "gitBranch": "big-update", "message": {"role": "user", "content": text}}


def _a(blocks, ts="2026-06-27T12:00:05.000Z", entry="cli", sidechain=False, cwd=CWD):
    return {"type": "assistant", "timestamp": ts, "entrypoint": entry, "isSidechain": sidechain,
            "cwd": cwd, "gitBranch": "big-update", "message": {"role": "assistant", "content": blocks}}


def _write_native(projects: Path, sid: str, lines: list[dict], cwd: str = CWD) -> Path:
    d = projects / project_dir_name(cwd)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")
    return path


def _mark_live(projects: Path, sid: str, *, pid: int | None = None) -> None:
    sessions = projects.parent / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{sid}.json").write_text(
        json.dumps({"sessionId": sid, "pid": pid or os.getpid(), "status": "busy", "kind": "interactive"}),
        encoding="utf-8",
    )


def _reader(tmp_path: Path) -> NativeSessionReader:
    return NativeSessionReader(tmp_path / "projects", tool_result_max=50)


# ── reader ───────────────────────────────────────────────────────────────────

def test_reader_lists_with_ai_title_and_origin(tmp_path: Path) -> None:
    _write_native(tmp_path / "projects", "s1", [
        _u("hello there"),
        _a([{"type": "text", "text": "hi"}]),
        {"type": "ai-title", "sessionId": "s1", "aiTitle": "A friendly chat"},
    ])
    sessions = _reader(tmp_path).list_sessions(CWD)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == "s1" and s.title == "A friendly chat"
    assert s.turns == 1 and s.origin == "terminal" and s.entrypoint == "cli"
    assert s.git_branch == "big-update" and s.live is False


def test_reader_title_fallbacks(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_native(projects, "sum", [_u("x"), {"type": "summary", "summary": "Summary title"}])
    _write_native(projects, "msg", [_u("first message wins as title")])
    by_id = {s.id: s for s in _reader(tmp_path).list_sessions(CWD)}
    assert by_id["sum"].title == "Summary title"
    assert by_id["msg"].title == "first message wins as title"


def test_reader_read_events_maps_blocks_with_contiguous_seq(tmp_path: Path) -> None:
    _write_native(tmp_path / "projects", "s1", [
        _u("a question"),
        _a([{"type": "thinking", "thinking": "hmm"}, {"type": "tool_use", "id": "t1", "name": "Bash",
                                                       "input": {"command": "ls"}}]),
        _u([{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 200, "is_error": False}]),
        _a([{"type": "text", "text": "the answer"}]),
    ])
    events = _reader(tmp_path).read_events(CWD, "s1", cap=0)
    kinds = [e["kind"] for e in events]
    assert kinds == ["user", "thinking", "tool_use", "tool_result", "text"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]
    tr = next(e for e in events if e["kind"] == "tool_result")
    assert "truncated" in tr["content"] and len(tr["content"]) < 200  # tool_result_max=50
    tu = next(e for e in events if e["kind"] == "tool_use")
    assert tu["name"] == "Bash" and tu["input"] == {"command": "ls"}


def test_reader_cap_keeps_true_tail_seq(tmp_path: Path) -> None:
    _write_native(tmp_path / "projects", "s1", [
        _u("q1"), _a([{"type": "text", "text": "a1"}]),
        _u("q2"), _a([{"type": "text", "text": "a2"}]),
    ])
    r = _reader(tmp_path)
    full = r.read_events(CWD, "s1", cap=0)
    capped = r.read_events(CWD, "s1", cap=2)
    assert [e["seq"] for e in capped] == [e["seq"] for e in full[-2:]] == [3, 4]


def test_reader_skips_sidechain_and_meta_only(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    # sidechain-only + pure-meta file → not a listable session
    _write_native(projects, "side", [
        {"type": "mode", "mode": "normal"},
        _u("subagent prompt", sidechain=True),
        _a([{"type": "text", "text": "subagent"}], sidechain=True),
    ])
    assert _reader(tmp_path).list_sessions(CWD) == []
    assert _reader(tmp_path).read_events(CWD, "side", cap=0) == []


def test_reader_marks_live_from_sessions_dir(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_native(projects, "live1", [_u("hi")])
    _mark_live(projects, "live1")
    s = _reader(tmp_path).find(CWD, "live1")
    assert s is not None and s.live is True and s.busy is True
    # a dead pid is not live
    _mark_live(projects, "live1", pid=2_000_000_000)
    assert _reader(tmp_path).find(CWD, "live1").live is False


# ── service merge / replay / adopt ───────────────────────────────────────────

def _service(tmp_path: Path, runner_events=None):
    projects = tmp_path / "projects"
    # claude_projects_dir must match the reader's dir (as build_chat_service wires
    # it in production) so the service's native purge targets the same files.
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=CWD, keepalive_s=0.2,
                     claude_projects_dir=projects)
    store = DiskSessionStore(cfg.data_dir)
    runner = FakeClaudeRunner(runner_events or [make_event(EventKind.TEXT, text="hi")])
    tasks: dict[str, asyncio.Task] = {}

    def launch(sid: str) -> None:   # run the turn-worker in-process (no detached subprocess)
        tasks[sid] = asyncio.ensure_future(TurnExecutor(store, runner, cfg).run(sid))

    svc = ChatService(store, runner, cfg,
                      native=NativeSessionReader(projects, tool_result_max=50), launch=launch)
    svc._test_tasks = tasks  # type: ignore[attr-defined]
    return svc, store, runner


async def _drain(svc: ChatService, sid: str) -> None:
    task = getattr(svc, "_test_tasks", {}).get(sid)
    if task is not None:
        await task


def test_session_dicts_merges_and_dedups(tmp_path: Path) -> None:
    svc, store, _ = _service(tmp_path)
    projects = tmp_path / "projects"
    # a web session whose CLI id == a native file → must dedup (shown once, as web)
    web = store.create(title="Web one", cwd=CWD, model=None)
    _write_native(projects, web.claude_session_id, [_u("from web"), _a([{"type": "text", "text": "ok"}])])
    # a native-only (terminal) session → appears as native
    _write_native(projects, "term-only", [_u("from terminal"),
                                           {"type": "ai-title", "sessionId": "term-only", "aiTitle": "Terminal chat"}])
    dicts = svc.session_dicts()
    by_id = {d["id"]: d for d in dicts}
    assert web.id in by_id and by_id[web.id]["origin"] == "web"
    assert "term-only" in by_id
    assert by_id["term-only"]["origin"] == "terminal" and by_id["term-only"]["native"] is True
    # the web session's native twin is not double-listed
    assert sum(1 for d in dicts if d["id"] == web.claude_session_id) <= 1


async def test_stream_native_is_read_only_replay(tmp_path: Path) -> None:
    svc, _, _ = _service(tmp_path)
    _write_native(tmp_path / "projects", "term1", [_u("hello"), _a([{"type": "text", "text": "world"}])])
    out = [ev async for ev in svc.stream("term1")]
    kinds = [e["kind"] for e in out]
    assert kinds[0] == "snapshot"                      # leading paging snapshot
    assert [k for k in kinds if k in ("user", "text")] == ["user", "text"]
    assert kinds[-1] == "snapshot" and out[-1]["status"] == "idle"

    got = svc.get_session("term1")
    assert got is not None and got["meta"]["native"] is True
    assert [e["kind"] for e in got["transcript"]] == ["user", "text"]


async def test_start_turn_adopts_native_session(tmp_path: Path) -> None:
    svc, store, runner = _service(tmp_path, runner_events=[
        make_event(EventKind.INIT, session_id="term1"),
        make_event(EventKind.TEXT, text="continued"),
    ])
    _write_native(tmp_path / "projects", "term1", [
        _u("original question"), _a([{"type": "text", "text": "original answer"}]),
        {"type": "ai-title", "sessionId": "term1", "aiTitle": "Adopt me"},
    ])
    ok, err = await svc.start_turn("term1", "keep going")
    assert ok and err is None
    await _drain(svc, "term1")

    meta = store.get_meta("term1")
    assert meta is not None and meta.origin == "terminal" and meta.claude_started is True
    # the first web turn RESUMES the real CLI session (not a fresh --session-id)
    assert runner.calls[-1]["resume"] is True and runner.calls[-1]["session_id"] == "term1"
    # imported history precedes the new turn; seq stays monotonic + unique
    kinds = [e["kind"] for e in store.transcript("term1")]
    assert kinds[:2] == ["user", "text"]            # imported native history
    assert "turn_end" in kinds
    seqs = [e["seq"] for e in store.transcript("term1")]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[0] == 1


async def test_start_turn_blocked_when_session_is_live(tmp_path: Path) -> None:
    svc, store, _ = _service(tmp_path)
    projects = tmp_path / "projects"
    _write_native(projects, "live1", [_u("open in terminal")])
    _mark_live(projects, "live1")
    assert await svc.start_turn("live1", "drive it") == (False, "live")
    assert store.get_meta("live1") is None  # not adopted


def test_delete_removes_native_transcript(tmp_path: Path) -> None:
    svc, _, _ = _service(tmp_path)
    projects = tmp_path / "projects"
    jsonl = _write_native(projects, "term-del", [_u("delete me")])
    sidecar = jsonl.parent / "term-del"
    (sidecar / "tool-results").mkdir(parents=True)  # CLI sidecar dir
    assert any(d["id"] == "term-del" for d in svc.session_dicts())

    assert svc.delete_session("term-del") is True
    assert not jsonl.exists() and not sidecar.exists()
    assert not any(d["id"] == "term-del" for d in svc.session_dicts())


async def test_turn_refused_when_session_live_in_terminal(tmp_path: Path) -> None:
    """A session adopted into the web store that is now live in a terminal must be
    read-only: driving it would `--resume` a session another process owns → the
    conflicting/hanging turn that showed up as 'stuck'."""
    svc, store, _ = _service(tmp_path)
    projects = tmp_path / "projects"
    _write_native(projects, "term-live", [_u("hi"), _a([{"type": "text", "text": "ok"}])])
    store.ensure("term-live", title="t", cwd=CWD, claude_session_id="term-live", origin="terminal")
    _mark_live(projects, "term-live")
    assert await svc.start_turn("term-live", "do it") == (False, "live")


def test_resume_pending_skips_live_sessions(tmp_path: Path) -> None:
    svc, store, _ = _service(tmp_path)
    projects = tmp_path / "projects"
    _write_native(projects, "term-live2", [_u("hi")])
    store.ensure("term-live2", title="t", cwd=CWD, claude_session_id="term-live2", origin="terminal")
    store.update_meta("term-live2", pending=["x"])
    _mark_live(projects, "term-live2")
    assert svc.resume_pending() == 0   # skipped — the terminal owns it


def test_delete_native_blocked_when_live(tmp_path: Path) -> None:
    svc, _, _ = _service(tmp_path)
    projects = tmp_path / "projects"
    jsonl = _write_native(projects, "live-del", [_u("open in terminal")])
    _mark_live(projects, "live-del")
    assert svc.delete_session("live-del") is False
    assert jsonl.exists()  # a live session's file is never yanked


# ── cross-folder native sync (sessions started in ANY in-scope folder, not just
#    default_cwd's slug — what the dynamic folder picker depends on) ────────────

def test_session_dicts_merges_sessions_from_other_folders(tmp_path: Path) -> None:
    svc, _, _ = _service(tmp_path)             # default_cwd = CWD (a configured root)
    projects = tmp_path / "projects"
    other = CWD + "/subproject"                # a different cwd → a different CLI slug
    _write_native(projects, "in-default", [_u("from the repo root")])
    _write_native(projects, "in-other", [_u("from a subfolder", cwd=other)], cwd=other)
    _write_native(projects, "in-tmp", [_u("scratch", cwd="/tmp/scratchpad")], cwd="/tmp/scratchpad")
    ids = {d["id"] for d in svc.session_dicts()}
    assert {"in-default", "in-other"} <= ids   # both in-scope slugs surface
    assert "in-tmp" not in ids                 # out-of-scope (outside $HOME / roots) hidden


async def test_native_session_in_other_folder_streams_and_deletes(tmp_path: Path) -> None:
    svc, _, _ = _service(tmp_path)
    projects = tmp_path / "projects"
    other = CWD + "/svc-x"
    jsonl = _write_native(projects, "other1",
                          [_u("hi", cwd=other), _a([{"type": "text", "text": "yo"}], cwd=other)],
                          cwd=other)
    got = svc.get_session("other1")            # found + replayed by id alone (no cwd hint)
    assert got is not None and [e["kind"] for e in got["transcript"]] == ["user", "text"]
    out = [ev async for ev in svc.stream("other1")]
    assert any(e["kind"] == "text" for e in out)
    assert svc.delete_session("other1") is True and not jsonl.exists()  # purged from its real slug


async def test_out_of_scope_native_session_is_unreachable_by_id(tmp_path: Path) -> None:
    """An out-of-scope folder's terminal session is not just hidden from the list —
    it can't be fetched, streamed, adopted/run, or deleted by id either (the agent
    must never be spawned in /tmp via a back-door id)."""
    svc, store, _ = _service(tmp_path)
    projects = tmp_path / "projects"
    _write_native(projects, "tmp-sess",
                  [_u("scratch", cwd="/tmp/work"), _a([{"type": "text", "text": "x"}], cwd="/tmp/work")],
                  cwd="/tmp/work")
    assert not any(d["id"] == "tmp-sess" for d in svc.session_dicts())   # not listed
    assert svc.get_session("tmp-sess") is None                          # not fetchable
    out = [ev async for ev in svc.stream("tmp-sess")]
    assert any(e["kind"] == "error" for e in out)                       # stream → not found
    assert await svc.start_turn("tmp-sess", "go") == (False, "not_found")  # not adoptable / runnable
    assert store.get_meta("tmp-sess") is None                           # nothing imported
    assert svc.delete_session("tmp-sess") is False                      # not deletable by id


def test_cwdless_native_session_is_not_leaked(tmp_path: Path) -> None:
    """A transcript with no cwd-bearing turn falls back to the projects dir for its
    cwd; that fallback must NOT pass the scope filter, or an out-of-scope session
    would silently leak into the web list."""
    svc, _, _ = _service(tmp_path)
    projects = tmp_path / "projects"
    _write_native(projects, "cwdless", [
        {"type": "ai-title", "sessionId": "cwdless", "aiTitle": "No cwd here"},
        _u("subagent prompt", sidechain=True),     # only sidechain → no cwd parsed
    ], cwd="/tmp/secret")                           # physically in an out-of-scope slug
    n = svc._native.find_anywhere("cwdless")
    assert n is not None and n.cwd == str(projects)  # the fallback cwd (= projects dir)
    assert not any(d["id"] == "cwdless" for d in svc.session_dicts())   # not leaked


def test_folder_scope_validation(tmp_path: Path) -> None:
    svc, _, _ = _service(tmp_path)             # default_cwd = CWD, home = the test user's $HOME
    home = str(Path.home())
    assert svc._allowed_cwd(home) is True
    assert svc._allowed_cwd(CWD) is True                          # under home + a configured root
    assert svc._allowed_cwd(home + "/.config") is False           # dot-prefixed component
    assert svc._allowed_cwd("/tmp") is False                      # outside home / roots
    assert svc._allowed_cwd(home + "/__no_such_dir_zzz__") is False  # missing dir
    # the in-scope predicate is lexical (no existence): dotfile + .. escapes rejected
    assert svc._folder_in_scope(home + "/projects") is True
    assert svc._folder_in_scope(home + "/../etc") is False
    assert svc._folder_in_scope("/etc") is False


def test_folder_scope_resolves_symlinks(tmp_path: Path) -> None:
    """Scope compares resolved paths: a session recorded under the real path is in
    scope when the configured root is a symlink to it (e.g. ``~/agent-me`` →
    ``/data/...``), so sessions born on either spelling of a symlinked workspace
    stay visible on the web."""
    real = tmp_path / "disk" / "workspace"
    (real / "repo").mkdir(parents=True)
    link = tmp_path / "link-ws"
    link.symlink_to(real, target_is_directory=True)
    projects = tmp_path / "projects"
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(link / "repo"),
                     claude_projects_dir=projects)
    svc = ChatService(DiskSessionStore(cfg.data_dir), FakeClaudeRunner([]), cfg,
                      native=NativeSessionReader(projects, tool_result_max=50))
    assert svc._folder_in_scope(str(real / "repo")) is True          # alias of the root
    assert svc._folder_in_scope(str(real / "repo" / "sub")) is True  # descendant via alias
    assert svc._folder_in_scope(str(real)) is False                  # parent of the root
    real_cwd = str(real / "repo")
    _write_native(projects, "sym-real", [
        _u("hi", cwd=real_cwd), _a([{"type": "text", "text": "yo"}], cwd=real_cwd),
    ], cwd=real_cwd)
    assert any(d["id"] == "sym-real" for d in svc.session_dicts())


def test_workspace_root_add_dir_brings_umbrella_sessions_in_scope(tmp_path: Path) -> None:
    """A CLI session started AT the workspace umbrella root (the parent of the
    repo, e.g. ``/data/agent-me.nvidia.com``) lists on the web once the umbrella
    is a configured add-dir — it is neither under $HOME nor under default_cwd."""
    umbrella = tmp_path / "workspace"
    (umbrella / "repo").mkdir(parents=True)
    projects = tmp_path / "projects"
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(umbrella / "repo"),
                     add_dirs=(str(umbrella),), claude_projects_dir=projects)
    svc = ChatService(DiskSessionStore(cfg.data_dir), FakeClaudeRunner([]), cfg,
                      native=NativeSessionReader(projects, tool_result_max=50))
    assert svc._folder_in_scope(str(umbrella)) is True
    _write_native(projects, "umbrella-root", [
        _u("hi", cwd=str(umbrella)), _a([{"type": "text", "text": "yo"}], cwd=str(umbrella)),
    ], cwd=str(umbrella))
    assert any(d["id"] == "umbrella-root" for d in svc.session_dicts())
