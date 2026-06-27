"""Tests for the Slack bridge's claude backend adapter (no Slack, no real CLI)."""
from __future__ import annotations

import asyncio

from agent_me.claude_agent import EventKind, TurnResult, make_event
from agent_me.slack_bridge.claude_agent import (
    initial_state,
    make_event_handler,
    one_line,
    run_turn_with_progress,
    short_tool_name,
    tool_hint,
)


class FakeRunner:
    def __init__(self, events: list[dict], *, result="ok", session_id="claude-abc",
                 is_error=False, exit_code=0, stderr="") -> None:
        self._events = events
        self._result = result
        self._sid = session_id
        self._is_error = is_error
        self._exit_code = exit_code
        self._stderr = stderr
        self.calls: list[dict] = []

    async def run_turn(self, *, prompt, cwd, session_id, resume, model, add_dirs,
                       on_event, append_system_prompt=None, **_kw) -> TurnResult:
        self.calls.append({
            "prompt": prompt, "cwd": cwd, "session_id": session_id, "resume": resume,
            "model": model, "append_system_prompt": append_system_prompt,
        })
        for ev in self._events:
            on_event(dict(ev))
        return TurnResult(
            session_id=self._sid, result=self._result, is_error=self._is_error,
            exit_code=self._exit_code, stderr=self._stderr,
        )


def test_short_tool_name() -> None:
    assert short_tool_name("Bash") == "Bash"
    assert short_tool_name("mcp__maas-jira__jira_search") == "jira:jira_search"
    assert short_tool_name("mcp__maas-outlook__outlook_create_draft") == "outlook:outlook_create_draft"
    assert short_tool_name(None) == "tool"


async def test_maps_events_to_progress_and_returns_final() -> None:
    events = [
        make_event(EventKind.INIT, session_id="claude-abc"),
        make_event(EventKind.TOOL_USE, id="t1", name="mcp__maas-jira__jira_search"),
        make_event(EventKind.TOOL_USE, id="t2", name="Bash"),
        make_event(EventKind.TOOL_RESULT, id="t1"),
        make_event(EventKind.TEXT, text="partial..."),
        make_event(EventKind.TOOL_RESULT, id="t2"),
        make_event(EventKind.RESULT, result="final answer", is_error=False),
    ]
    runner = FakeRunner(events, result="final answer", session_id="claude-abc")

    snapshots: list[int] = []

    async def progress_cb(_state) -> None:
        snapshots.append(_state["tools_started"])

    result, state, final = await run_turn_with_progress(
        runner=runner, prompt="hi", cwd="/repo", session_id="claude-abc",
        resume=False, model=None, append_system_prompt="SYS", progress_cb=progress_cb,
    )

    assert final == "final answer"
    assert not result.is_error
    assert state["tools_started"] == 2
    assert state["tools_done"] == 2
    assert state["in_flight"] == {}
    assert state["completed"] == ["jira:jira_search", "Bash"]
    assert state["session_id"] == "claude-abc"
    assert state["final_text"] == "final answer"
    assert snapshots
    assert runner.calls[0]["append_system_prompt"] == "SYS"
    assert runner.calls[0]["resume"] is False


async def test_in_flight_tracks_unfinished_tools() -> None:
    events = [
        make_event(EventKind.TOOL_USE, id="t1", name="Read"),
        make_event(EventKind.RESULT, result="done"),
    ]
    runner = FakeRunner(events, result="done")
    _result, state, final = await run_turn_with_progress(
        runner=runner, prompt="x", cwd="/repo", session_id="s", resume=True,
        model="claude-opus-4-8", progress_cb=None,
    )
    assert final == "done"
    assert state["tools_started"] == 1 and state["tools_done"] == 0
    assert state["in_flight"] == {"t1": "Read"}
    assert runner.calls[0]["resume"] is True
    assert runner.calls[0]["model"] == "claude-opus-4-8"


def test_initial_state_shape() -> None:
    st = initial_state("sid-1")
    assert st["session_id"] == "sid-1"
    assert st["tools_started"] == 0 and st["final_text"] is None
    assert st["in_flight"] == {} and st["completed"] == []
    assert st["activity"] is None and isinstance(st["started"], float)


def test_one_line_collapses_and_truncates() -> None:
    assert one_line("  hello\n  world  ") == "hello world"
    assert one_line(None) == ""
    long = "x" * 300
    out = one_line(long, 50)
    assert len(out) == 50 and out.endswith("…")


def test_tool_hint_picks_meaningful_arg() -> None:
    assert tool_hint({"file_path": "/repo/app.py"}) == "/repo/app.py"
    assert tool_hint({"command": "git status"}) == "git status"
    assert tool_hint({"unrelated": "x"}) == ""
    assert tool_hint(None) == ""


def test_thinking_and_text_surface_as_activity() -> None:
    # Drive the handler directly so each schedule() call is observed against the
    # live state at that instant (the async callback path mutates one shared dict).
    state = initial_state("s")
    seen: list[str | None] = []
    handler = make_event_handler(state, lambda s: seen.append(s["activity"]))

    handler(make_event(EventKind.INIT, session_id="s"))
    handler(make_event(EventKind.THINKING, text="Let me look at the calendar\nthen email"))
    handler(make_event(EventKind.TEXT, text="Checking your meetings now"))

    # No tool ever ran, yet every step still scheduled a live activity update.
    assert state["tools_started"] == 0
    assert seen[0] == "💭 starting up…"
    assert seen[1] == "💭 Let me look at the calendar then email"
    assert seen[2] == "💬 Checking your meetings now"
    assert state["activity"] == "💬 Checking your meetings now"


async def test_tool_use_activity_includes_hint() -> None:
    events = [
        make_event(EventKind.TOOL_USE, id="t1", name="Read", input={"file_path": "/repo/app.py"}),
    ]
    runner = FakeRunner(events, result="done")
    _r, state, _f = await run_turn_with_progress(
        runner=runner, prompt="x", cwd="/r", session_id="s", resume=False,
        model=None, progress_cb=None, heartbeat_interval_s=0,
    )
    assert state["activity"] == "🔧 Read — /repo/app.py"
    assert state["in_flight"] == {"t1": "Read"}


class SlowRunner:
    """A runner that goes quiet (no events) for a beat, like a slow MCP call."""

    def __init__(self, delay_s: float) -> None:
        self._delay = delay_s
        self.calls: list[dict] = []

    async def run_turn(self, *, on_event, **kw) -> TurnResult:
        self.calls.append(kw)
        await asyncio.sleep(self._delay)
        return TurnResult(session_id="s", result="done", is_error=False, exit_code=0)


async def test_heartbeat_keeps_progress_alive_while_quiet() -> None:
    runner = SlowRunner(0.25)
    ticks = 0

    async def cb(_state) -> None:
        nonlocal ticks
        ticks += 1

    await run_turn_with_progress(
        runner=runner, prompt="x", cwd="/r", session_id="s", resume=False,
        model=None, progress_cb=cb, heartbeat_interval_s=0.05,
    )
    # No events fired, so every callback came from the heartbeat ticker.
    assert ticks >= 2


async def test_heartbeat_disabled_when_interval_zero() -> None:
    runner = SlowRunner(0.15)
    ticks = 0

    async def cb(_state) -> None:
        nonlocal ticks
        ticks += 1

    await run_turn_with_progress(
        runner=runner, prompt="x", cwd="/r", session_id="s", resume=False,
        model=None, progress_cb=cb, heartbeat_interval_s=0,
    )
    assert ticks == 0
