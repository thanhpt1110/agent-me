"""Tests for syncing Slack agent turns into the web chat.

Covers the shared operating-context, the ``DiskSessionStore`` extensions
(``ensure``/``last_event``), the ``WebSessionMirror`` write path, the Slack
runner's ``event_tap`` hook, and the service-side external busy/tail behaviour
that surfaces an in-flight Slack turn live in the browser.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent_me.claude_agent import EventKind, make_event, operating_context
from agent_me.dashboard.chat.config import ChatConfig
from agent_me.dashboard.chat.local_sessions import project_dir_name
from agent_me.dashboard.chat.mirror import WebSessionMirror, web_session_id_for
from agent_me.dashboard.chat.service import ChatService
from agent_me.dashboard.chat.store import DiskSessionStore
from agent_me.slack_bridge.claude_agent import run_turn_with_progress

from .test_chat import FakeClaudeRunner
from .test_slack_bridge_claude import FakeRunner

# ── shared operating context (web /chat + Slack stay in step) ────────────────

def test_operating_context_mentions_repo_and_branch() -> None:
    txt = operating_context("/home/x/agent-me")
    assert "/home/x/agent-me" in txt
    assert "big-update" in txt
    assert "gh` is NOT installed" in txt
    assert "feature-y" in operating_context("/r", branch="feature-y")


def test_chat_config_delegates_to_shared_operating_context() -> None:
    from agent_me.dashboard.chat.config import _default_operating_context

    assert "big-update" in _default_operating_context("/repo")


# ── store: ensure() + last_event() ───────────────────────────────────────────

def test_store_ensure_is_idempotent(tmp_path: Path) -> None:
    store = DiskSessionStore(tmp_path / "cs")
    m = store.ensure("sid-1", title="T", cwd="/c", origin="slack", slack_thread_ts="9.9")
    assert (m.id, m.origin, m.slack_thread_ts) == ("sid-1", "slack", "9.9")
    assert m.claude_session_id is None  # not pinned until the external turn reports it
    # second ensure returns the existing session unchanged
    m2 = store.ensure("sid-1", title="OTHER", cwd="/x")
    assert m2.title == "T" and m2.origin == "slack"


def test_store_last_event_reads_tail(tmp_path: Path) -> None:
    store = DiskSessionStore(tmp_path / "cs")
    store.ensure("s", title="t", cwd="/c")
    assert store.last_event("s") is None
    store.append_event("s", make_event(EventKind.TEXT, text="hi", seq=1))
    store.append_event("s", make_event(EventKind.TURN_END, seq=2))
    last = store.last_event("s")
    assert last["kind"] == EventKind.TURN_END.value and last["seq"] == 2
    assert store.last_event("missing") is None


# ── mirror write path ────────────────────────────────────────────────────────

def test_web_session_id_for_is_stable_and_safe() -> None:
    assert web_session_id_for("1719.55") == "slack-1719-55"
    assert web_session_id_for("../etc/passwd") == "slack-etc-passwd"
    assert web_session_id_for("") == "slack-session"


def test_mirror_begin_tap_end(tmp_path: Path) -> None:
    store = DiskSessionStore(tmp_path / "cs")
    mirror = WebSessionMirror(store)

    sid = mirror.begin_turn(key="1719.55", title="Do the thing please",
                            cwd="/repo", user_text="Do the thing")
    assert sid == web_session_id_for("1719.55")
    meta = store.get_meta(sid)
    assert meta.origin == "slack" and meta.slack_thread_ts == "1719.55"

    tap = mirror.tap(sid)
    tap(make_event(EventKind.THINKING, text="hmm"))
    tap(make_event(EventKind.INIT, session_id="x"))            # internal → not mirrored
    tap(make_event(EventKind.TOOL_USE, name="Bash", input={"command": "ls"}))
    mirror.end_turn(sid, claude_session_id="real-1")

    transcript = store.transcript(sid)
    assert [e["kind"] for e in transcript] == ["user", "thinking", "tool_use", "turn_end"]
    assert [e["seq"] for e in transcript] == [1, 2, 3, 4]      # monotonic, INIT skipped

    m2 = store.get_meta(sid)
    assert m2.claude_session_id == "real-1" and m2.claude_started is True
    assert m2.turns == 1 and m2.claude_session_ids == ["real-1"]

    # a second turn on the same thread reuses the session; seq keeps climbing
    sid2 = mirror.begin_turn(key="1719.55", title="x", cwd="/repo", user_text="again")
    assert sid2 == sid
    assert store.transcript(sid)[-1]["seq"] == 5


def test_mirror_end_turn_records_error(tmp_path: Path) -> None:
    store = DiskSessionStore(tmp_path / "cs")
    mirror = WebSessionMirror(store)
    sid = mirror.begin_turn(key="t", title="x", cwd="/c", user_text="hi")
    mirror.end_turn(sid, error="claude exited 1: boom")
    kinds = [e["kind"] for e in store.transcript(sid)]
    assert kinds == ["user", "error", "turn_end"]


# ── Slack runner event_tap ───────────────────────────────────────────────────

async def test_run_turn_with_progress_taps_raw_events() -> None:
    events = [make_event(EventKind.TEXT, text="hi"), make_event(EventKind.RESULT, result="done")]
    tapped: list[str] = []
    _result, _state, final = await run_turn_with_progress(
        runner=FakeRunner(events, result="done"), prompt="p", cwd="/c",
        session_id="s", resume=False, model=None,
        event_tap=lambda e: tapped.append(e.get("kind")), heartbeat_interval_s=0,
    )
    assert final == "done"
    assert tapped == [EventKind.TEXT.value, EventKind.RESULT.value]


async def test_event_tap_failure_never_breaks_the_turn() -> None:
    def boom(_e: dict) -> None:
        raise RuntimeError("mirror down")

    _r, _s, final = await run_turn_with_progress(
        runner=FakeRunner([make_event(EventKind.TEXT, text="x")], result="ok"),
        prompt="p", cwd="/c", session_id="s", resume=False, model=None,
        event_tap=boom, heartbeat_interval_s=0,
    )
    assert final == "ok"


# ── service: external busy + live tail (cross-process Slack turn) ─────────────

def test_external_busy_recent_running_then_finished(tmp_path: Path) -> None:
    store = DiskSessionStore(tmp_path / "cs")
    mirror = WebSessionMirror(store)
    sid = mirror.begin_turn(key="t", title="x", cwd=str(tmp_path), user_text="hi")

    busy_cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path))
    assert ChatService(store, FakeClaudeRunner([]), busy_cfg).is_busy(sid) is True

    # a zero window models a crashed/stale turn → never reported busy
    stale_cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path),
                           external_busy_window_s=0.0)
    assert ChatService(store, FakeClaudeRunner([]), stale_cfg).is_busy(sid) is False

    mirror.end_turn(sid, claude_session_id="r")
    assert ChatService(store, FakeClaudeRunner([]), busy_cfg).is_busy(sid) is False


async def test_stream_tails_external_in_flight_turn(tmp_path: Path) -> None:
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path),
                     keepalive_s=1.0, external_tail_poll_s=0.02)
    store = DiskSessionStore(cfg.data_dir)
    svc = ChatService(store, FakeClaudeRunner([]), cfg)
    mirror = WebSessionMirror(store)

    sid = mirror.begin_turn(key="123.45", title="from slack", cwd=str(tmp_path), user_text="hi")
    assert svc.is_busy(sid) is True

    received: list[dict] = []

    async def consume() -> None:
        async for evt in svc.stream(sid):
            received.append(evt)
            if evt.get("kind") == EventKind.TURN_END.value:
                return

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.06)                 # replay history, start tailing
    mirror.tap(sid)(make_event(EventKind.TEXT, text="working"))
    await asyncio.sleep(0.06)
    mirror.end_turn(sid, claude_session_id="real-sid")
    await asyncio.wait_for(task, timeout=2.0)

    kinds = [e.get("kind") for e in received]
    assert EventKind.USER.value in kinds      # history replay
    assert EventKind.TEXT.value in kinds       # tailed live
    assert EventKind.TURN_END.value in kinds   # tail stops here
    assert svc.is_busy(sid) is False
    assert store.get_meta(sid).claude_session_id == "real-sid"


def test_delete_slack_session_preserves_cli_transcript(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    cwd = str(tmp_path / "work")
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=cwd, claude_projects_dir=projects)
    store = DiskSessionStore(cfg.data_dir)
    svc = ChatService(store, FakeClaudeRunner([]), cfg)
    mirror = WebSessionMirror(store)

    sid = mirror.begin_turn(key="th", title="x", cwd=cwd, user_text="hi")
    mirror.end_turn(sid, claude_session_id="real-cli")

    pdir = projects / project_dir_name(cwd)
    pdir.mkdir(parents=True)
    cli = pdir / "real-cli.jsonl"
    cli.write_text("{}")

    assert svc.delete_session(sid) is True
    assert not store.exists(sid)
    assert cli.exists()  # Slack thread still owns its CLI transcript


# ── post-review regression guards ────────────────────────────────────────────

def test_transcript_since_reads_only_the_appended_tail(tmp_path: Path) -> None:
    store = DiskSessionStore(tmp_path / "cs")
    store.ensure("s", title="t", cwd="/c")
    store.append_event("s", make_event(EventKind.USER, text="a", seq=1))
    evs, off = store.transcript_since("s", 0)
    assert [e["kind"] for e in evs] == [EventKind.USER.value] and off > 0
    # nothing new since `off`
    assert store.transcript_since("s", off) == ([], off)
    # a later append is read incrementally, not from the top
    store.append_event("s", make_event(EventKind.TEXT, text="b", seq=2))
    evs2, off2 = store.transcript_since("s", off)
    assert [e["kind"] for e in evs2] == [EventKind.TEXT.value] and off2 > off


async def test_start_turn_rejected_while_slack_turn_in_flight(tmp_path: Path) -> None:
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path))
    store = DiskSessionStore(cfg.data_dir)
    svc = ChatService(store, FakeClaudeRunner([]), cfg)
    mirror = WebSessionMirror(store)
    sid = mirror.begin_turn(key="t", title="x", cwd=str(tmp_path), user_text="hi")
    # a mirrored Slack turn is mid-flight → a web turn on the same session is refused
    ok, err = await svc.start_turn(sid, "continue from the browser")
    assert (ok, err) == (False, "busy")


def test_web_origin_session_is_not_reported_external_busy(tmp_path: Path) -> None:
    # A crashed/half-written WEB session (no turn_end) must not be reported busy:
    # only Slack-origin sessions are driven by another process.
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path))
    store = DiskSessionStore(cfg.data_dir)
    svc = ChatService(store, FakeClaudeRunner([]), cfg)
    meta = store.create(title="web", cwd=str(tmp_path), model=None)
    store.append_event(meta.id, make_event(EventKind.TEXT, text="partial", seq=1))
    assert svc.is_busy(meta.id) is False
