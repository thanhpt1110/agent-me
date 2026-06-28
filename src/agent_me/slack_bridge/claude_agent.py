"""Map the shared claude engine's events onto the Slack bridge's progress shape."""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from agent_me.claude_agent import ClaudeRunner, EventKind, TurnResult

ProgressCb = Callable[[dict[str, Any]], Awaitable[None]]


def initial_state(session_id: str | None = None) -> dict[str, Any]:
    return {
        "tools_started": 0,
        "tools_done": 0,
        "in_flight": {},
        "completed": [],
        "session_id": session_id,
        "final_text": None,
        "is_error": False,
        "error_message": None,
        # Live-progress extras: `started` anchors elapsed-time rendering and
        # `activity` is a ready-to-show one-liner of the freshest thing the
        # agent is doing (thinking text, narration, or the current tool) so the
        # placeholder shows life even on turns that never call a tool.
        "started": time.monotonic(),
        "activity": None,
    }


def short_tool_name(name: str | None) -> str:
    if not name:
        return "tool"
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = (parts[1] if len(parts) > 1 else "mcp").removeprefix("maas-")
        tool = parts[2] if len(parts) > 2 else parts[-1]
        return f"{server}:{tool}"
    return name


def one_line(text: str | None, limit: int = 180) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)].rstrip() + "…"


_TOOL_HINT_KEYS = ("file_path", "path", "command", "pattern", "query", "url", "prompt")


def tool_hint(tool_input: Any) -> str:
    """A short, human one-liner for what a tool call is about (file, command, query)."""
    if not isinstance(tool_input, dict):
        return ""
    for key in _TOOL_HINT_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return one_line(val, 60)
    return ""


def make_event_handler(
    state: dict[str, Any], schedule: Callable[[dict[str, Any]], None],
) -> Callable[[dict], None]:
    def on_event(evt: dict) -> None:
        kind = evt.get("kind")
        changed = False
        if kind == EventKind.INIT.value:
            if evt.get("session_id"):
                state["session_id"] = evt["session_id"]
            state["activity"] = "💭 starting up…"
            changed = True
        elif kind == EventKind.THINKING.value:
            snippet = one_line(evt.get("text"))
            if snippet:
                state["activity"] = f"💭 {snippet}"
                changed = True
        elif kind == EventKind.TEXT.value:
            snippet = one_line(evt.get("text"))
            if snippet:
                state["activity"] = f"💬 {snippet}"
                changed = True
        elif kind == EventKind.TOOL_USE.value:
            tid = evt.get("id") or f"t{state['tools_started']}"
            name = short_tool_name(evt.get("name"))
            state["in_flight"][tid] = name
            state["tools_started"] += 1
            hint = tool_hint(evt.get("input"))
            state["activity"] = f"🔧 {name}" + (f" — {hint}" if hint else "")
            changed = True
        elif kind == EventKind.TOOL_RESULT.value:
            tid = evt.get("id")
            if tid in state["in_flight"]:
                state["completed"].append(state["in_flight"].pop(tid))
                state["tools_done"] += 1
                changed = True
        elif kind == EventKind.ERROR.value:
            state["is_error"] = True
            state["error_message"] = evt.get("text")
            changed = True
        if changed:
            schedule(state)
    return on_event


async def run_turn_with_progress(
    *,
    runner: ClaudeRunner,
    prompt: str,
    cwd: str,
    session_id: str | None,
    resume: bool,
    model: str | None,
    append_system_prompt: str | None = None,
    progress_cb: ProgressCb | None = None,
    event_tap: Callable[[dict], None] | None = None,
    add_dirs: Sequence[str] = (),
    heartbeat_interval_s: float = 4.0,
) -> tuple[TurnResult, dict[str, Any], str]:
    state = initial_state(session_id)
    pending: set[asyncio.Task] = set()

    def schedule(_state: dict[str, Any]) -> None:
        if progress_cb is None:
            return
        task = asyncio.ensure_future(progress_cb(_state))
        pending.add(task)
        task.add_done_callback(pending.discard)

    # Heartbeat: claude can go quiet for many seconds mid-turn (a slow MCP call,
    # extended thinking with no streamed text). Without this, the placeholder
    # freezes and looks stalled. Re-scheduling the callback on a fixed tick lets
    # the renderer advance its elapsed-time/spinner so the user keeps seeing life.
    async def heartbeat() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(heartbeat_interval_s)
                schedule(state)

    hb_task: asyncio.Task | None = None
    if progress_cb is not None and heartbeat_interval_s > 0:
        hb_task = asyncio.ensure_future(heartbeat())

    # The Slack progress handler updates the placeholder; an optional tap also
    # forwards every raw event to a mirror (so the turn shows up in the web chat).
    slack_handler = make_event_handler(state, schedule)

    def on_event(evt: dict) -> None:
        if event_tap is not None:
            with contextlib.suppress(Exception):  # mirroring must never break the turn
                event_tap(evt)
        slack_handler(evt)

    try:
        result = await runner.run_turn(
            prompt=prompt, cwd=cwd, session_id=session_id, resume=resume,
            model=model, add_dirs=tuple(add_dirs),
            on_event=on_event,
            append_system_prompt=append_system_prompt,
        )
    finally:
        if hb_task is not None:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hb_task

    if pending:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*list(pending), return_exceptions=True), timeout=5.0,
            )

    final = (result.result or "").strip()
    state["final_text"] = final
    if result.session_id:
        state["session_id"] = result.session_id
    return result, state, final
