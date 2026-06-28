"""Tests for the chat-window optimizations: local-session purge on delete,
rename, the slash-command/skill menu, and the Ctrl+~ host terminal."""
from __future__ import annotations

import os
import select
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_me.dashboard.chat.commands import discover_commands
from agent_me.dashboard.chat.config import ChatConfig
from agent_me.dashboard.chat.local_sessions import (
    project_dir_name,
    purge_local_sessions,
)
from agent_me.dashboard.chat.models import EventKind, make_event
from agent_me.dashboard.chat.service import ChatService
from agent_me.dashboard.chat.store import DiskSessionStore
from agent_me.dashboard.chat.terminal import TerminalSession, resolve_shell

from .test_chat import FakeClaudeRunner, _drain

# ── local session purge ─────────────────────────────────────────────────────

def test_project_dir_name_encoding() -> None:
    assert project_dir_name("/home/thaphan/agent-me") == "-home-thaphan-agent-me"
    # every non-alphanumeric char (incl. the dot in .config) maps to '-'
    assert project_dir_name("/home/thaphan/.config/caddy") == "-home-thaphan--config-caddy"


def test_purge_local_sessions(tmp_path: Path) -> None:
    cwd = "/home/x/y"
    pdir = tmp_path / project_dir_name(cwd)
    pdir.mkdir(parents=True)
    (pdir / "a.jsonl").write_text("x")
    (pdir / "b.jsonl").write_text("y")
    removed = purge_local_sessions(tmp_path, cwd, ["a", "b", "missing", "", None])
    assert set(removed) == {"a.jsonl", "b.jsonl"}
    assert not (pdir / "a.jsonl").exists() and not (pdir / "b.jsonl").exists()
    # missing files / re-runs never raise
    assert purge_local_sessions(tmp_path, cwd, ["a"]) == []


async def test_delete_purges_local_claude_sessions(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    cwd = str(tmp_path / "work")
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=cwd, keepalive_s=0.2,
                     claude_projects_dir=projects)
    store = DiskSessionStore(cfg.data_dir)
    runner = FakeClaudeRunner([make_event(EventKind.TEXT, text="hi")], session_id="forked-id")
    svc = ChatService(store, runner, cfg)
    meta = svc.create_session(title=None, cwd=None, model=None)

    pdir = projects / project_dir_name(cwd)
    pdir.mkdir(parents=True)
    (pdir / f"{meta.id}.jsonl").write_text("{}")
    await svc.start_turn(meta.id, "hi")
    await _drain(svc, meta.id)

    # a resume that forks the CLI id is still tracked so delete is complete
    assert "forked-id" in store.get_meta(meta.id).claude_session_ids
    (pdir / "forked-id.jsonl").write_text("{}")

    assert svc.delete_session(meta.id) is True
    assert not (pdir / f"{meta.id}.jsonl").exists()
    assert not (pdir / "forked-id.jsonl").exists()


# ── rename ───────────────────────────────────────────────────────────────────

async def test_rename_session(tmp_path: Path) -> None:
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=str(tmp_path), keepalive_s=0.2)
    svc = ChatService(DiskSessionStore(cfg.data_dir), FakeClaudeRunner([]), cfg)
    meta = svc.create_session(title="old", cwd=None, model=None)
    out = svc.rename_session(meta.id, "  My  New   Title  ")
    assert out is not None and out["title"] == "My New Title"  # whitespace collapsed
    assert svc.rename_session(meta.id, "   ") is None          # empty rejected
    assert svc.rename_session("nope", "x") is None             # unknown id


# ── slash-command / skill discovery ──────────────────────────────────────────

def test_discover_commands(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "skills" / "my-skill").mkdir(parents=True)
    (home / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Does a thing.\n---\nbody")
    (home / "commands").mkdir()
    (home / "commands" / "deploy.md").write_text("---\ndescription: Deploy it.\n---\nrun")
    (home / "commands" / "git").mkdir()
    (home / "commands" / "git" / "commit.md").write_text("Just commit.\n")
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)

    ordered = discover_commands(repo, claude_home=home)
    cmds = {c.name: c for c in ordered}
    assert cmds["/my-skill"].source == "skill" and cmds["/my-skill"].description == "Does a thing."
    assert cmds["/deploy"].source == "command" and cmds["/deploy"].description == "Deploy it."
    assert "/git:commit" in cmds  # nested dir → namespaced command
    assert cmds["/git:commit"].description == "Just commit."  # no frontmatter → first line
    # the recognisable TUI built-ins are surfaced, in a curated order, before skills
    assert cmds["/context"].source == "builtin" and cmds["/usage"].source == "builtin"
    assert ordered[0].name == "/context"  # built-ins lead the menu
    builtins = [c.name for c in ordered if c.source == "builtin"]
    assert builtins[:2] == ["/context", "/usage"]
    last_builtin = max(i for i, c in enumerate(ordered) if c.source == "builtin")
    first_skill = min(i for i, c in enumerate(ordered) if c.source == "skill")
    assert last_builtin < first_skill  # built-ins before skills


# ── host terminal ────────────────────────────────────────────────────────────

def test_resolve_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    assert resolve_shell("/bin/sh") == "/bin/sh"
    assert os.path.exists(resolve_shell())          # always lands on a real shell
    assert resolve_shell("/no/such/shell").startswith("/")


def test_terminal_session_echo_and_resize() -> None:
    sess = TerminalSession(shell=resolve_shell(), cwd="/tmp", cols=80, rows=24)
    sess.start()
    try:
        sess.resize(100, 40)
        assert (sess.cols, sess.rows) == (100, 40)
        sess.write("echo term_marker_42\n")
        out = b""
        deadline = time.time() + 5
        while time.time() < deadline and b"term_marker_42" not in out:
            r, _, _ = select.select([sess.fd], [], [], 0.3)
            if r:
                try:
                    out += os.read(sess.fd, 4096)
                except OSError:
                    break
        assert b"term_marker_42" in out
    finally:
        sess.close()
    assert sess.fd is None and sess.pid is None


def test_terminal_ws_rejected_when_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_OPERATOR_ACTION_CODE", "secret-code")
    from agent_me.dashboard import app as dash
    client = TestClient(dash.app)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/chat/terminal"):
        pass


def test_terminal_ws_echo_when_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_OPERATOR_ACTION_CODE", raising=False)  # gate off
    from agent_me.dashboard import app as dash
    client = TestClient(dash.app)
    with client.websocket_connect("/ws/chat/terminal") as ws:
        ws.send_json({"type": "resize", "cols": 90, "rows": 30})
        ws.send_json({"type": "stdin", "data": "echo wsterm_ok\n"})
        buf = ""
        for _ in range(80):
            buf += ws.receive_text()
            if "wsterm_ok" in buf:
                break
        assert "wsterm_ok" in buf


# ── HTTP wiring (rename + commands routes) ───────────────────────────────────

def test_http_rename_and_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_OPERATOR_ACTION_CODE", raising=False)
    from agent_me.dashboard import app as dash
    client = TestClient(dash.app)

    sid = client.post("/api/chat/sessions", json={"title": "before", "cwd": "/tmp"}).json()["id"]
    r = client.patch(f"/api/chat/sessions/{sid}", json={"title": "after rename"})
    assert r.status_code == 200 and r.json()["title"] == "after rename"
    assert client.patch(f"/api/chat/sessions/{sid}", json={"title": "   "}).status_code == 400
    assert client.patch("/api/chat/sessions/nope", json={"title": "x"}).status_code == 404

    cmds = client.get("/api/chat/commands")
    assert cmds.status_code == 200
    assert any(c["source"] == "builtin" for c in cmds.json()["commands"])
