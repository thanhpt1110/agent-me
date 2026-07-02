"""Shared pytest fixtures for the agent-me dashboard test suite.

Covers two cross-cutting concerns:
- **Per-test temp state dir** — points the dashboard at a fresh
  STATE_DIR so each test gets isolation from real bridge data and
  from other tests.
- **Token toggling** — sets/clears `DASHBOARD_TOKEN` for the auth tests
  without leaking between tests.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

# Point the native-chat store at a throwaway dir BEFORE the dashboard is imported
# (it builds CHAT_SERVICE at import). Otherwise HTTP chat tests that create/rename
# sessions write into the real `data/chat-sessions/`, leaving junk like an
# "after rename" chat in the running dashboard. Set unconditionally — tests must
# never touch real chat data.
_CHAT_TEST_DIR = tempfile.mkdtemp(prefix="agent-me-chat-test-")
os.environ["CHAT_DATA_DIR"] = str(Path(_CHAT_TEST_DIR) / "chat-sessions")
atexit.register(lambda: shutil.rmtree(_CHAT_TEST_DIR, ignore_errors=True))

# Terminal WS tests must spawn plain shells, never attach the operator's real
# persistent tmux session (agme-web). Dedicated tmux tests build their own
# TerminalSession with a throwaway session name instead.
os.environ["CHAT_TERMINAL_PERSIST"] = "0"

# Load configs/.env once now (the dashboard does it at import) so the autouse
# delenv below reliably strips the operator passcode for every test.
with contextlib.suppress(Exception):
    import agent_me.dashboard.app  # noqa: F401


@pytest.fixture(autouse=True)
def _isolate_operator_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """configs/.env is loaded at import; keep its operator passcode out of tests
    so the chat/operator gate stays off unless a test sets it explicitly."""
    monkeypatch.delenv("DASHBOARD_OPERATOR_ACTION_CODE", raising=False)


@pytest.fixture
def temp_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Replace the dashboard state-dir paths with a per-test tmp_path.

    The dashboard reads STATE_DIR / DB_PATH / log paths at import time,
    so we monkeypatch the resolved attributes directly. CACHE_DIR is
    pre-created by the module's own import-time mkdir and we do the
    same here to keep the layout realistic.
    """
    state = tmp_path / "agent-me-state"
    state.mkdir()
    cache = state / "dashboard-cache"
    cache.mkdir()

    from agent_me.dashboard import state_reader

    monkeypatch.setattr(state_reader, "STATE_DIR", state)
    monkeypatch.setattr(state_reader, "DB_PATH", state / "state.db")
    monkeypatch.setattr(state_reader, "CACHE_DIR", cache)
    monkeypatch.setattr(state_reader, "BRIDGE_LOG", state / "bridge.log")
    monkeypatch.setattr(state_reader, "BRIEF_LOG", state / "brief.log")
    return state


@pytest.fixture
def seeded_db(temp_state_dir: Path) -> Path:
    """Create a state.db with the bridge's schema + a couple rows.

    Schema mirrors `agent_me.slack_bridge.app.DB_SCHEMA` — we duplicate
    it here intentionally (rather than importing from the bridge module,
    which has its own import-time setup that touches ~/.local/state)
    so the test stays hermetic.
    """
    db_path = temp_state_dir / "state.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE threads (
            thread_ts      TEXT PRIMARY KEY,
            channel        TEXT NOT NULL,
            user_id        TEXT,
            auto_approve   INTEGER NOT NULL DEFAULT 0,
            created_at     INTEGER NOT NULL,
            last_active_at INTEGER NOT NULL
        );
        CREATE TABLE messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_ts   TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT,
            slack_ts    TEXT,
            created_at  INTEGER NOT NULL
        );
        CREATE TABLE pending_approvals (
            id            TEXT PRIMARY KEY,
            thread_ts     TEXT NOT NULL,
            action_type   TEXT,
            payload_json  TEXT,
            status        TEXT NOT NULL,
            slack_message_ts TEXT,
            created_at    INTEGER NOT NULL,
            resolved_at   INTEGER
        );
        CREATE TABLE claude_sessions (
            thread_ts    TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL,
            started_at   INTEGER NOT NULL,
            last_used_at INTEGER NOT NULL,
            turn_count   INTEGER NOT NULL DEFAULT 0
        );
    """)
    now = int(time.time() * 1000)
    h_24_ago = now - 24 * 60 * 60 * 1000
    h_2_days_ago = now - 2 * 24 * 60 * 60 * 1000

    conn.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
        ("1234.5", "D123", "U_TEST", 0, h_2_days_ago, h_24_ago + 60_000),  # active
    )
    conn.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
        ("9999.5", "D123", "U_TEST", 0, h_2_days_ago, h_2_days_ago),  # stale
    )
    conn.execute(
        "INSERT INTO messages (thread_ts, role, content, slack_ts, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        ("1234.5", "user", "hi", "1234.5", h_24_ago + 60_000),
    )
    conn.execute(
        "INSERT INTO claude_sessions VALUES (?, ?, ?, ?, ?)",
        ("1234.5", "session-abc", h_24_ago, h_24_ago + 60_000, 3),
    )
    conn.execute(
        "INSERT INTO pending_approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("approval-1", "1234.5", "jira_create_issue", "{}", "pending",
         "9999.6", h_24_ago + 70_000, None),
    )
    conn.close()
    return db_path


@pytest.fixture
def with_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "test-token-abc123"
    monkeypatch.setenv("DASHBOARD_TOKEN", token)
    monkeypatch.delenv("DASHBOARD_TRUST_NETWORK", raising=False)
    return token


@pytest.fixture
def without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("DASHBOARD_TRUST_NETWORK", raising=False)
