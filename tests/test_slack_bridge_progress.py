"""Tests for the Slack bridge live-progress renderer (no Slack, no real CLI)."""
from __future__ import annotations

import asyncio
import importlib

from agent_me.slack_bridge.claude_agent import initial_state


def _load_app(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")
    return importlib.import_module("agent_me.slack_bridge.app")


class _CaptureClient:
    def __init__(self) -> None:
        self.updates: list[str] = []

    async def chat_update(self, *, channel, ts, text) -> None:
        self.updates.append(text)


def _activity_state(activity: str):
    st = initial_state("s")
    st["activity"] = activity
    return st


def test_fmt_elapsed(monkeypatch, tmp_path) -> None:
    app = _load_app(monkeypatch, tmp_path)
    assert app._fmt_elapsed(0) == "0s"
    assert app._fmt_elapsed(5.9) == "5s"
    assert app._fmt_elapsed(65) == "1m05s"


def test_format_progress_shows_thinking_without_any_tools(monkeypatch, tmp_path) -> None:
    app = _load_app(monkeypatch, tmp_path)
    st = initial_state("s")
    st["activity"] = "💭 reading your latest email"
    out = app.format_progress(st)
    # The old renderer only ever showed "0/0 tool calls done"; now a tool-less
    # turn still shows the phase, an animated spinner, and what the agent is doing.
    assert "thinking" in out
    assert "0/0 tools" in out
    assert "reading your latest email" in out
    assert any(frame in out for frame in app.PROGRESS_SPINNER)


def test_format_progress_running_single_tool(monkeypatch, tmp_path) -> None:
    app = _load_app(monkeypatch, tmp_path)
    st = initial_state("s")
    st["tools_started"] = 2
    st["tools_done"] = 1
    st["in_flight"] = {"t2": "jira:jira_search"}
    st["completed"] = ["outlook:outlook_list_messages"]
    st["activity"] = "🔧 jira:jira_search — project = ABC"
    out = app.format_progress(st)
    assert "1/2 tools" in out
    assert "running 1 tool" in out
    assert "jira:jira_search" in out
    assert "done: `outlook:outlook_list_messages`" in out
    # A single in-flight tool is named in the activity line; no separate list.
    assert "▸ running:" not in out


def test_format_progress_lists_multiple_running_tools(monkeypatch, tmp_path) -> None:
    app = _load_app(monkeypatch, tmp_path)
    st = initial_state("s")
    st["tools_started"] = 2
    st["in_flight"] = {"t1": "jira:jira_search", "t2": "Bash"}
    st["activity"] = "🔧 Bash — git status"
    out = app.format_progress(st)
    assert "running 2 tools" in out
    assert "▸ running:" in out
    assert "`jira:jira_search`" in out and "`Bash`" in out


def test_format_progress_tolerates_legacy_state(monkeypatch, tmp_path) -> None:
    app = _load_app(monkeypatch, tmp_path)
    # A state dict without the new keys (e.g. an older path) must still render.
    st = {"tools_started": 0, "tools_done": 0, "in_flight": {}, "completed": []}
    out = app.format_progress(st)
    assert "0/0 tools" in out


async def test_progress_callback_coalesces_and_flushes_latest(monkeypatch, tmp_path) -> None:
    app = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "PROGRESS_UPDATE_MIN_INTERVAL_S", 0.05)
    client = _CaptureClient()
    cb = app.make_slack_progress_callback(client, "C1", "1.0")

    await cb(_activity_state("💭 first"))    # window open -> sent immediately
    await cb(_activity_state("💭 second"))   # within cooldown -> coalesced + flush scheduled
    await cb(_activity_state("💬 third"))    # coalesced -> latest wins
    await asyncio.sleep(0.12)                # let the trailing flush fire

    # The original bug dropped everything after the first frame; now the latest
    # state is always delivered, and intermediate frames are coalesced (not "second").
    assert len(client.updates) == 2
    assert "💭 first" in client.updates[0]
    assert "💬 third" in client.updates[1]


async def test_progress_callback_skips_identical_renders(monkeypatch, tmp_path) -> None:
    app = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "PROGRESS_UPDATE_MIN_INTERVAL_S", 0.05)
    client = _CaptureClient()
    cb = app.make_slack_progress_callback(client, "C1", "1.0")
    st = _activity_state("💭 same")

    await cb(st)
    await asyncio.sleep(0.08)
    await cb(st)              # identical render -> no second API call
    await asyncio.sleep(0.08)
    assert len(client.updates) == 1


async def test_progress_callback_aclose_cancels_pending_and_stops(monkeypatch, tmp_path) -> None:
    app = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "PROGRESS_UPDATE_MIN_INTERVAL_S", 0.05)
    client = _CaptureClient()
    cb = app.make_slack_progress_callback(client, "C1", "1.0")

    await cb(_activity_state("💭 a"))   # immediate send
    await cb(_activity_state("💭 b"))   # schedules a trailing flush
    await cb.aclose()                   # cancels the flush; closes the callback
    await asyncio.sleep(0.12)
    assert len(client.updates) == 1     # the scheduled flush never fired

    await cb(_activity_state("💬 c"))   # post-close updates are ignored
    await asyncio.sleep(0.12)
    assert len(client.updates) == 1


async def test_aclose_cancels_flush_parked_in_send(monkeypatch, tmp_path) -> None:
    # Regression: the trailing flush is parked mid-`chat.update` (network in
    # flight) when aclose() runs. aclose() must cancel it so the stale progress
    # frame never lands AFTER the final reply.
    app = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "PROGRESS_UPDATE_MIN_INTERVAL_S", 0.02)

    class GatedClient:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.gate = asyncio.Event()
            self.park = False

        async def chat_update(self, *, channel, ts, text) -> None:
            if self.park and not text.startswith("FINAL"):
                await self.gate.wait()  # simulate a slow in-flight send
            self.sent.append(text)

    client = GatedClient()
    cb = app.make_slack_progress_callback(client, "C1", "1.0")

    await cb(_activity_state("💭 frame-A"))   # window open -> sent immediately
    client.park = True
    await cb(_activity_state("💭 frame-B"))   # coalesced -> schedules a flush
    await asyncio.sleep(0.05)                 # flush fires and parks inside _send

    await cb.aclose()                         # must cancel the parked flush
    await client.chat_update(channel="C1", ts="1.0", text="FINAL the answer")
    client.gate.set()                         # release; the cancelled flush must not send
    await asyncio.sleep(0.05)

    assert "💭 frame-B" not in "".join(client.sent)
    assert client.sent[-1].startswith("FINAL")


async def test_progress_callback_ignores_final_state(monkeypatch, tmp_path) -> None:
    app = _load_app(monkeypatch, tmp_path)
    client = _CaptureClient()
    cb = app.make_slack_progress_callback(client, "C1", "1.0")
    st = _activity_state("done")
    st["final_text"] = "the answer"
    await cb(st)
    assert client.updates == []
