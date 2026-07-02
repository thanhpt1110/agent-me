"""Tests for the chat-window optimizations: local-session purge on delete,
rename, the slash-command/skill menu, and the Ctrl+~ host terminal."""
from __future__ import annotations

import os
import select
import subprocess
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
from agent_me.dashboard.chat.terminal import (
    TerminalSession,
    list_tmux_sessions,
    resolve_shell,
    resolve_tmux,
    valid_tmux_name,
)

from .test_chat import FakeClaudeRunner, _drain, _wire

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


def test_purge_local_sessions_resolves_symlinked_cwd(tmp_path: Path) -> None:
    """The CLI slugs its PHYSICAL getcwd; a session whose stored cwd is a symlink
    alias (e.g. ``~/agent-me`` → ``/data/.../agent-me``) must purge the transcript
    under the resolved slug too, or deleted chats linger in ``claude --resume``."""
    real = tmp_path / "disk" / "repo"
    real.mkdir(parents=True)
    alias = tmp_path / "alias-repo"
    alias.symlink_to(real, target_is_directory=True)
    physical = tmp_path / project_dir_name(str(real.resolve()))
    physical.mkdir(parents=True)
    (physical / "s1.jsonl").write_text("x")
    removed = purge_local_sessions(tmp_path, str(alias), ["s1"])
    assert removed == ["s1.jsonl"]
    assert not (physical / "s1.jsonl").exists()


async def test_delete_purges_local_claude_sessions(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    cwd = str(tmp_path / "work")
    cfg = ChatConfig(data_dir=tmp_path / "cs", default_cwd=cwd, keepalive_s=0.2,
                     claude_projects_dir=projects)
    store = DiskSessionStore(cfg.data_dir)
    runner = FakeClaudeRunner([make_event(EventKind.TEXT, text="hi")], session_id="forked-id")
    svc = _wire(store, runner, cfg)
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


def _read_until(sess: TerminalSession, needle: bytes, timeout: float = 8.0) -> bool:
    out = b""
    deadline = time.time() + timeout
    while time.time() < deadline and needle not in out:
        r, _, _ = select.select([sess.fd], [], [], 0.3)
        if r:
            try:
                out += os.read(sess.fd, 4096)
            except OSError:
                break
    return needle in out


def test_terminal_tmux_session_default_is_per_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEV and PROD share the uid's tmux socket — a fixed default name would fuse
    their terminals into one live screen across both gates."""
    monkeypatch.delenv("CHAT_TERMINAL_TMUX_SESSION", raising=False)
    monkeypatch.setenv("DEPLOY_ENV", "dev")
    assert ChatConfig.from_env("/tmp").terminal_tmux_session == "agme-web-dev"
    monkeypatch.delenv("DEPLOY_ENV", raising=False)
    assert ChatConfig.from_env("/tmp").terminal_tmux_session == "agme-web-prod"
    monkeypatch.setenv("CHAT_TERMINAL_TMUX_SESSION", "custom-name")
    assert ChatConfig.from_env("/tmp").terminal_tmux_session == "custom-name"


def test_terminal_build_argv_tmux_and_fallback() -> None:
    plain = TerminalSession(shell="/bin/bash", cwd="/tmp")
    assert plain.build_argv() == ["/bin/bash", "-i"]
    tm = TerminalSession(shell="/bin/bash", cwd="/tmp",
                         tmux_session="agme-t", tmux_bin="/usr/bin/tmux")
    assert tm.build_argv() == ["/usr/bin/tmux", "new-session", "-A", "-s", "agme-t",
                               "-c", "/tmp", "/bin/bash",
                               ";", "set-option", "-g", "window-size", "latest"]
    # tmux requested but not installed → plain shell (feature degrades, never breaks)
    half = TerminalSession(shell="/bin/bash", cwd="/tmp", tmux_session="agme-t")
    assert half.build_argv() == ["/bin/bash", "-i"]


@pytest.mark.skipif(resolve_tmux() is None, reason="tmux not installed")
def test_terminal_tmux_session_survives_client_close() -> None:
    """The persistence contract: killing the PTY client (browser closed / SSH
    dropped) leaves the tmux session and whatever runs in it alive; a new client
    reattaches to the same screen."""
    tmux = resolve_tmux()
    name = f"agme-test-{os.getpid()}"

    def session_alive() -> bool:
        return subprocess.run([tmux, "has-session", "-t", name],
                              capture_output=True).returncode == 0

    def wait_session_alive(timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if session_alive():
                return True
            time.sleep(0.2)
        return False

    a = TerminalSession(shell=resolve_shell(), cwd="/tmp",
                        tmux_session=name, tmux_bin=tmux)
    a.start()
    try:
        # Let the tmux server/session finish starting before typing — killing the
        # first client mid-startup takes the still-forking server down with it.
        assert wait_session_alive()
        # $((70+7)) makes the OUTPUT differ from the typed line, so the PTY's
        # input echo can't satisfy the assertion.
        a.write("echo persist_$((70+7))_ok\n")
        assert _read_until(a, b"persist_77_ok")
        a.close()                                   # simulate browser/SSH death
        assert session_alive()                      # THE feature: session survives
        b = TerminalSession(shell=resolve_shell(), cwd="/tmp",
                            tmux_session=name, tmux_bin=tmux)
        b.start()
        # reattach redraws the surviving session's screen — the marker is still there
        assert _read_until(b, b"persist_77_ok")
        b.close()
        assert session_alive()
    finally:
        subprocess.run([tmux, "kill-session", "-t", name], capture_output=True)


def test_valid_tmux_name() -> None:
    assert valid_tmux_name("agme-web-dev") and valid_tmux_name("a_b.c-1")
    assert not valid_tmux_name("") and not valid_tmux_name("bad name")
    assert not valid_tmux_name("a;b") and not valid_tmux_name("x" * 65)


@pytest.mark.skipif(resolve_tmux() is None, reason="tmux not installed")
def test_list_tmux_sessions_lists_live_sessions() -> None:
    tmux = resolve_tmux()
    name = f"agme-list-{os.getpid()}"
    subprocess.run([tmux, "new-session", "-d", "-s", name], check=True)
    try:
        found = [s for s in list_tmux_sessions() if s["name"] == name]
        assert found and found[0]["windows"] >= 1 and found[0]["attached"] == 0
        assert found[0]["activity"] > 0
    finally:
        subprocess.run([tmux, "kill-session", "-t", name], capture_output=True)


def test_api_chat_tmux_persist_off_reports_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """With persistence off (the test suite's mode) the endpoint reports no
    sessions and persist=false, so the sidebar simply hides."""
    from agent_me.dashboard import app as dash
    client = TestClient(dash.app)
    d = client.get("/api/chat/tmux").json()
    assert d == {"sessions": [], "default": "", "persist": False}


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


def test_http_folder_repoint_unstarted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DASHBOARD_OPERATOR_ACTION_CODE", raising=False)
    from agent_me.dashboard import app as dash
    client = TestClient(dash.app)

    home = str(Path.home())                          # an in-scope cwd ($HOME subtree)
    sid = client.post("/api/chat/sessions", json={"cwd": "/tmp"}).json()["id"]
    # an unstarted chat can be re-pointed to another in-scope dir via the folder picker
    r = client.patch(f"/api/chat/sessions/{sid}", json={"cwd": home})
    assert r.status_code == 200 and r.json()["cwd"] == home
    # an out-of-scope dir (/tmp) and a non-existent dir are both refused
    assert client.patch(f"/api/chat/sessions/{sid}", json={"cwd": "/tmp"}).status_code == 409
    assert client.patch(f"/api/chat/sessions/{sid}",
                        json={"cwd": home + "/__no_such_dir__"}).status_code == 409


def test_chat_page_nav_sources_and_folder_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_OPERATOR_ACTION_CODE", raising=False)
    from agent_me.dashboard import app as dash
    # `sources` is a template global so every page (incl. /chat) renders the nav
    # Sources dropdown — it used to be empty on /chat (regression guard).
    assert "sources" in dash.TEMPLATES.env.globals
    client = TestClient(dash.app)
    assert 'href="/source/' in client.get("/chat").text
    folders = client.get("/api/chat/folders")
    assert folders.status_code == 200 and folders.json()["folders"]

    cmds = client.get("/api/chat/commands")
    assert cmds.status_code == 200
    assert any(c["source"] == "builtin" for c in cmds.json()["commands"])
