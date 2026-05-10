"""Tests for the dashboard log_sources module.

Covers all three streams plus the two pure helpers:

- `SLACK_INTERACTION_EVENTS` constant shape.
- `tail_bridge_slack_filtered` — allowlist filtering on top of the
  inode-aware tail in `state_reader`.
- `resolve_session_jsonl_path` — literal sanitized lookup + glob fallback.
- `tail_session_jsonl` — partial-line safety + missing-file path.
- `tail_journal_unit` — subprocess stdout streaming + missing-binary path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────


async def _drain(
    aiter,
    *,
    max_items: int = 100,
    timeout_s: float = 1.0,
) -> list[Any]:
    """Drain an async iterator until it stalls for `timeout_s` seconds.

    The iterators under test are deliberately infinite (live tails), so
    we don't expect StopAsyncIteration. Instead we use `wait_for` on
    each `__anext__` call and treat a TimeoutError as "no more items
    coming right now". This is the same pattern the SSE consumer uses
    (it just keeps the connection open until the client disconnects).
    """
    out: list[Any] = []
    it = aiter.__aiter__()
    while len(out) < max_items:
        try:
            item = await asyncio.wait_for(it.__anext__(), timeout=timeout_s)
        except (TimeoutError, StopAsyncIteration):
            break
        out.append(item)
    return out


def _write_jsonl_lines(path: Path, lines: list[dict[str, Any]]) -> None:
    """Write a list of dicts as a JSON-line file (newline-terminated)."""
    path.write_text("\n".join(json.dumps(d) for d in lines) + "\n")


# ── SLACK_INTERACTION_EVENTS constant ────────────────────────────────────


def test_slack_interaction_events_includes_known_buttons() -> None:
    from agent_me.dashboard.log_sources import SLACK_INTERACTION_EVENTS

    # Spot-check: messages, slash, buttons, soft-rejects all present.
    for evt in (
        "message_received",
        "query_handled",
        "slash_handled",
        "button_brief_refresh",
        "button_menu_brief_week",
        "message_rejected_user",
    ):
        assert evt in SLACK_INTERACTION_EVENTS

    # Sanity: it's a frozenset, not a list (so membership is O(1)).
    assert isinstance(SLACK_INTERACTION_EVENTS, frozenset)


# ── tail_bridge_slack_filtered ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_tail_bridge_slack_filtered_keeps_only_allowlisted(
    temp_state_dir: Path,
) -> None:
    """Mixed bridge.log → only events in the allowlist should survive."""
    from agent_me.dashboard import log_sources, state_reader

    # The temp_state_dir fixture monkeypatches state_reader.BRIDGE_LOG;
    # log_sources captured the original at import time, so re-bind it.
    monkey_log = state_reader.BRIDGE_LOG
    log_sources.BRIDGE_LOG = monkey_log  # type: ignore[attr-defined]

    keep_events = ["message_received", "button_brief_refresh", "query_handled"]
    drop_events = ["heartbeat", "internal_wiring", "brief_refresh_started"]

    payload = (
        [{"event": e, "thread_ts": "1.1"} for e in keep_events]
        + [{"event": e, "ts": i} for i, e in enumerate(drop_events)]
        # Non-JSON noise — `tail_logs` wraps it as `{"raw": ...}`, which
        # the filter must also drop (no .event field).
    )
    monkey_log.write_text(
        "\n".join(json.dumps(d) for d in payload) + "\nthis is not json\n"
    )

    items = await _drain(
        log_sources.tail_bridge_slack_filtered(from_lines=20),
        max_items=10,
        timeout_s=1.0,
    )

    seen = [it["line"]["event"] for it in items]
    assert sorted(seen) == sorted(keep_events)
    assert all(it["source"] == "slack" for it in items)


@pytest.mark.asyncio
async def test_tail_bridge_slack_filtered_empty_log(
    temp_state_dir: Path,
) -> None:
    """No bridge.log at all → tail returns nothing and doesn't crash."""
    from agent_me.dashboard import log_sources, state_reader
    log_sources.BRIDGE_LOG = state_reader.BRIDGE_LOG  # type: ignore[attr-defined]

    items = await _drain(
        log_sources.tail_bridge_slack_filtered(from_lines=10),
        max_items=5,
        timeout_s=0.6,
    )
    assert items == []


# ── resolve_session_jsonl_path ──────────────────────────────────────────


@pytest.fixture
def fake_claude_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                         temp_state_dir: Path) -> tuple[Path, Path]:
    """Point CLAUDE_PROJECTS_DIR at a tmp path and create chat-cwd.

    Returns `(projects_dir, chat_cwd_dir)` so tests can scaffold
    arbitrary `~/.claude/projects/<sanitized>/<sid>.jsonl` layouts.
    """
    from agent_me.dashboard import log_sources

    projects = tmp_path / "claude-projects"
    projects.mkdir()
    chat_cwd = temp_state_dir / "chat-cwd"
    chat_cwd.mkdir()

    monkeypatch.setattr(log_sources, "CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr(log_sources, "STATE_DIR", temp_state_dir)
    return projects, chat_cwd


def test_resolve_session_jsonl_path_finds_literal_match(
    fake_claude_projects: tuple[Path, Path],
) -> None:
    from agent_me.dashboard.log_sources import (
        _sanitize_path,
        resolve_session_jsonl_path,
    )

    projects, chat_cwd = fake_claude_projects
    sanitized = _sanitize_path(chat_cwd.resolve())
    proj_dir = projects / sanitized
    proj_dir.mkdir()
    sid = "abc123-session"
    jsonl = proj_dir / f"{sid}.jsonl"
    jsonl.write_text('{"type":"user","content":"hi"}\n')

    found = resolve_session_jsonl_path(sid)
    assert found == jsonl


def test_resolve_session_jsonl_path_returns_none_when_missing(
    fake_claude_projects: tuple[Path, Path],
) -> None:
    from agent_me.dashboard.log_sources import resolve_session_jsonl_path

    # No proj dir created at all.
    assert resolve_session_jsonl_path("does-not-exist") is None


def test_resolve_session_jsonl_path_returns_none_when_jsonl_missing(
    fake_claude_projects: tuple[Path, Path],
) -> None:
    """Project dir exists, but no jsonl for the requested session id."""
    from agent_me.dashboard.log_sources import (
        _sanitize_path,
        resolve_session_jsonl_path,
    )

    projects, chat_cwd = fake_claude_projects
    (projects / _sanitize_path(chat_cwd.resolve())).mkdir()

    assert resolve_session_jsonl_path("missing-sid") is None


def test_resolve_session_jsonl_path_no_projects_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If `~/.claude/projects/` doesn't exist (no Claude Code installed),
    we return None without raising — the SSE handler will surface a
    `missing` event to the UI."""
    from agent_me.dashboard import log_sources

    monkeypatch.setattr(
        log_sources, "CLAUDE_PROJECTS_DIR", tmp_path / "no-such-dir"
    )
    assert log_sources.resolve_session_jsonl_path("anything") is None


# ── tail_session_jsonl ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tail_session_jsonl_yields_missing_when_unresolved(
    fake_claude_projects: tuple[Path, Path],
) -> None:
    from agent_me.dashboard.log_sources import tail_session_jsonl

    items = await _drain(
        tail_session_jsonl("nope-sid", from_lines=5, follow=False),
        max_items=2,
        timeout_s=0.5,
    )
    assert len(items) == 1
    assert items[0]["source"] == "session"
    assert items[0]["line"]["event"] == "missing"
    assert items[0]["line"]["session_id"] == "nope-sid"


@pytest.mark.asyncio
async def test_tail_session_jsonl_replay_drops_trailing_partial(
    fake_claude_projects: tuple[Path, Path],
) -> None:
    """File ends mid-line during initial read → partial NOT emitted as
    a replay line. With `follow=False` the iterator stops after the
    single complete line, proving the partial was filtered."""
    from agent_me.dashboard.log_sources import (
        _sanitize_path,
        tail_session_jsonl,
    )

    projects, chat_cwd = fake_claude_projects
    proj_dir = projects / _sanitize_path(chat_cwd.resolve())
    proj_dir.mkdir()
    sid = "partial-replay"
    jsonl = proj_dir / f"{sid}.jsonl"
    jsonl.write_bytes(b'{"type":"user","content":"complete"}\n{"type":"asst')

    items = await _drain(
        tail_session_jsonl(sid, from_lines=10, follow=False),
        max_items=10,
        timeout_s=0.5,
    )
    assert len(items) == 1
    assert items[0]["replay"] is True
    assert items[0]["line"]["content"] == "complete"


@pytest.mark.asyncio
async def test_tail_session_jsonl_live_partial_buffered_until_newline(
    fake_claude_projects: tuple[Path, Path],
) -> None:
    """During live tail, bytes without a trailing newline are buffered.
    They only emit once a newline arrives, never as `{"raw": ...}` halfsies."""
    from agent_me.dashboard.log_sources import (
        _sanitize_path,
        tail_session_jsonl,
    )

    projects, chat_cwd = fake_claude_projects
    proj_dir = projects / _sanitize_path(chat_cwd.resolve())
    proj_dir.mkdir()
    sid = "partial-live"
    jsonl = proj_dir / f"{sid}.jsonl"
    jsonl.write_bytes(b'{"type":"user","content":"first"}\n')

    # Run the consumer as a background task so the generator's
    # internal `asyncio.sleep` isn't cancelled by `wait_for` timeouts.
    received: list[dict[str, Any]] = []

    async def consumer() -> None:
        async for evt in tail_session_jsonl(sid, from_lines=10,
                                            poll_interval_s=0.02):
            received.append(evt)

    task = asyncio.create_task(consumer())
    try:
        # Replay landed.
        await asyncio.sleep(0.1)
        assert [e["line"].get("content") for e in received] == ["first"]

        # Append partial (no newline). Multiple polls should pass
        # without emitting anything new.
        with jsonl.open("ab") as f:
            f.write(b'{"type":"assi')
        await asyncio.sleep(0.15)
        assert len(received) == 1, (
            f"partial emitted prematurely: {received[1:]}"
        )

        # Now complete the line. The very next poll should emit it.
        with jsonl.open("ab") as f:
            f.write(b'stant","content":"second"}\n')
        await asyncio.sleep(0.15)
        assert len(received) == 2
        assert received[1]["replay"] is False
        assert received[1]["line"]["type"] == "assistant"
        assert received[1]["line"]["content"] == "second"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_tail_session_jsonl_no_follow_terminates(
    fake_claude_projects: tuple[Path, Path],
) -> None:
    """`follow=False` → emit replay then StopAsyncIteration."""
    from agent_me.dashboard.log_sources import (
        _sanitize_path,
        tail_session_jsonl,
    )

    projects, chat_cwd = fake_claude_projects
    proj_dir = projects / _sanitize_path(chat_cwd.resolve())
    proj_dir.mkdir()
    sid = "non-follow"
    (proj_dir / f"{sid}.jsonl").write_text(
        '{"type":"user","content":"a"}\n{"type":"user","content":"b"}\n'
    )

    items = await _drain(
        tail_session_jsonl(sid, from_lines=10, follow=False),
        max_items=10,
        timeout_s=0.5,
    )
    assert [i["line"]["content"] for i in items] == ["a", "b"]
    assert all(i["replay"] for i in items)


# ── tail_journal_unit ───────────────────────────────────────────────────


class _FakeStdout:
    """Minimal asyncio.StreamReader stand-in: feed `lines`, then EOF."""

    def __init__(self, lines: list[bytes]) -> None:
        self._queue: list[bytes] = [*list(lines), b""]  # b"" = EOF

    async def readline(self) -> bytes:
        # Mimic StreamReader.readline semantics: non-blocking pop from
        # the queue. EOF is signalled by an empty bytes object.
        await asyncio.sleep(0)  # yield to the loop
        return self._queue.pop(0) if self._queue else b""


class _FakeProc:
    """Subset of asyncio.subprocess.Process the function under test uses."""

    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.asyncio
async def test_tail_journal_unit_streams_subprocess_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_me.dashboard import log_sources

    fake_lines = [
        b"2026-05-11T00:00:00+0000 watcher[1]: tick 1\n",
        b"2026-05-11T00:00:01+0000 watcher[1]: tick 2\n",
        b"\n",  # blank lines should be skipped
        b"2026-05-11T00:00:02+0000 watcher[1]: tick 3\n",
    ]
    captured_cmd: list[str] = []

    async def fake_create(*cmd, stdout=None, stderr=None):
        captured_cmd.extend(cmd)
        return _FakeProc(fake_lines)

    monkeypatch.setattr(
        log_sources.asyncio, "create_subprocess_exec", fake_create
    )

    items = await _drain(
        log_sources.tail_journal_unit("agent-me-watch", from_lines=2),
        max_items=10,
        timeout_s=1.0,
    )

    assert captured_cmd[:3] == ["journalctl", "--user", "-u"]
    assert "agent-me-watch" in captured_cmd
    assert "-f" in captured_cmd  # follow on by default

    seen = [it["line"] for it in items]
    assert seen == [
        "2026-05-11T00:00:00+0000 watcher[1]: tick 1",
        "2026-05-11T00:00:01+0000 watcher[1]: tick 2",
        "2026-05-11T00:00:02+0000 watcher[1]: tick 3",
    ]
    # First two lines are within the replay window, third isn't.
    assert [it["replay"] for it in items] == [True, True, False]
    assert all(it["source"] == "watcher" for it in items)


@pytest.mark.asyncio
async def test_tail_journal_unit_handles_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `journalctl` isn't on PATH (CI container, macOS), we yield a
    diagnostic event instead of crashing the SSE coroutine."""
    from agent_me.dashboard import log_sources

    async def fake_create(*_args, **_kwargs):
        raise FileNotFoundError("journalctl: No such file or directory")

    monkeypatch.setattr(
        log_sources.asyncio, "create_subprocess_exec", fake_create
    )

    items = await _drain(
        log_sources.tail_journal_unit("agent-me-watch"),
        max_items=2,
        timeout_s=0.5,
    )
    assert len(items) == 1
    assert items[0]["source"] == "watcher"
    assert items[0]["line"]["event"] == "journalctl_unavailable"


# ── End-to-end: app routes ──────────────────────────────────────────────


def test_logs_page_renders(temp_state_dir: Path, with_token: str) -> None:
    """The /logs HTML page renders with the 3 tabs visible."""
    from starlette.testclient import TestClient

    from agent_me.dashboard.app import app

    r = TestClient(app).get("/logs", headers={"Authorization": f"Bearer {with_token}"})
    assert r.status_code == 200
    body = r.text
    assert "Watcher" in body
    assert "Slack" in body
    assert "Session" in body
    # The session dropdown appears (placeholder option).
    assert "select a session" in body


def test_sse_session_requires_session_id(temp_state_dir: Path,
                                         with_token: str) -> None:
    from starlette.testclient import TestClient

    from agent_me.dashboard.app import app

    r = TestClient(app).get(
        "/api/sse/logs/session",
        headers={"Authorization": f"Bearer {with_token}"},
    )
    assert r.status_code == 400


def test_logs_page_filters_threads_with_session_id(seeded_db: Path,
                                                   with_token: str) -> None:
    """The recent_threads list passed to the template must only include
    threads that have a non-null session_id (others can't be traced)."""
    from starlette.testclient import TestClient

    from agent_me.dashboard.app import app

    # The seeded_db fixture creates one session-bound thread (1234.5)
    # and one session-less thread (9999.5). Only the bound one should
    # show up in the dropdown.
    r = TestClient(app).get(
        "/logs", headers={"Authorization": f"Bearer {with_token}"},
    )
    assert r.status_code == 200
    body = r.text
    # session-bound thread renders its short session id
    assert "session-" in body
    # session-less thread (9999.5) should NOT have an option entry; we
    # check the dropdown doesn't have an option for "9999.5".
    assert 'thread 9999.5' not in body


# Reset BRIDGE_LOG patch leakage between modules. log_sources captures
# state_reader.BRIDGE_LOG at import time, so the slack-filtered tests
# overwrite it; restore on teardown so subsequent test files (which use
# the temp_state_dir fixture without re-binding) don't see a stale path.
@pytest.fixture(autouse=True)
def _restore_bridge_log_path():
    from agent_me.dashboard import log_sources, state_reader
    saved = log_sources.BRIDGE_LOG
    yield
    log_sources.BRIDGE_LOG = saved  # type: ignore[attr-defined]
    # Also restore state_reader's path if a test mutated it.
    if saved != state_reader.BRIDGE_LOG:
        log_sources.BRIDGE_LOG = state_reader.BRIDGE_LOG  # type: ignore[attr-defined]
