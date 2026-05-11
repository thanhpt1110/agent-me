"""Phase 2b approval-gate module tests.

The bridge module itself (`slack_bridge/app.py`) requires SLACK_*_TOKEN
env vars at import time, so these tests target the pure-helper module
`slack_bridge/approvals.py` instead. That module has no Slack
dependency and exposes everything the bridge needs at runtime: hook
bootstrap, decision-file writer, request scanner, DB CRUD.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from agent_me.slack_bridge import approvals

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """Per-test STATE_DIR with the approvals subtree pre-created."""
    sd = tmp_path / "state"
    sd.mkdir()
    approvals.ensure_dirs(sd)
    return sd


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    """In-memory DB? No — file DB so WAL-mode SELECT round-trips work
    the same way the bridge sees them. The schema mirrors the bridge."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path, isolation_level=None)
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
        CREATE TABLE pending_approvals (
            id              TEXT PRIMARY KEY,
            thread_ts       TEXT NOT NULL,
            action_type     TEXT,
            payload_json    TEXT,
            status          TEXT NOT NULL,
            slack_message_ts TEXT,
            created_at      INTEGER NOT NULL,
            resolved_at     INTEGER,
            tool_use_id     TEXT,
            session_id      TEXT,
            tool_name       TEXT,
            decision_reason TEXT,
            slack_channel   TEXT,
            auto_approved   INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX pending_tool_use_idx ON pending_approvals(tool_use_id);
    """)
    return conn


# ── Hook bootstrap ──────────────────────────────────────────────────────


def test_bootstrap_writes_executable_hook_and_settings(state: Path, tmp_path: Path) -> None:
    chat_cwd = tmp_path / "chat-cwd"
    chat_cwd.mkdir()
    hook_path = approvals.bootstrap_hooks(chat_cwd=chat_cwd, state_dir=state)
    assert hook_path.exists()
    assert hook_path.stat().st_mode & 0o111  # executable bit
    body = hook_path.read_text()
    # Hook references the same approvals dir we passed in.
    assert str(approvals.approvals_dir(state)) in body
    # And shells out to jq + cats decisions/<TOOL_ID>.
    assert "jq -r" in body
    assert "decisions/${TOOL_ID}" in body
    assert "permissionDecision" in body  # fallback timeout JSON

    settings = json.loads((chat_cwd / ".claude" / "settings.json").read_text())
    assert "PreToolUse" in settings["hooks"]
    matchers = [h["matcher"] for h in settings["hooks"]["PreToolUse"]]
    # The matcher pattern shape may vary (anchored `^Write$` vs unanchored
    # `Write`); we only care that the high-impact write tools the design
    # doc lists are matched. Check tool-name presence rather than exact
    # alternation syntax.
    matcher_blob = " ".join(matchers)
    assert "Write" in matcher_blob
    assert "Edit" in matcher_blob
    assert "mcp__maas-jira__jira_create_issue" in matcher_blob
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd == str(hook_path)


def test_bootstrap_is_idempotent(state: Path, tmp_path: Path) -> None:
    chat_cwd = tmp_path / "chat-cwd"
    chat_cwd.mkdir()
    p1 = approvals.bootstrap_hooks(chat_cwd=chat_cwd, state_dir=state)
    body1 = p1.read_text()
    p2 = approvals.bootstrap_hooks(chat_cwd=chat_cwd, state_dir=state)
    body2 = p2.read_text()
    assert p1 == p2
    assert body1 == body2


# ── Decision file ────────────────────────────────────────────────────────


def test_write_decision_emits_hook_specific_output(state: Path) -> None:
    p = approvals.write_decision(state, "tool-abc", "allow", "ok")
    assert p.exists()
    blob = json.loads(p.read_text())
    out = blob["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"] == "ok"


def test_write_decision_rejects_invalid_choice(state: Path) -> None:
    with pytest.raises(ValueError):
        approvals.write_decision(state, "x", "shrug", "no")


def test_write_decision_atomic_no_partial_files(state: Path) -> None:
    """Tempfile leftovers shouldn't pile up if write succeeds."""
    for i in range(5):
        approvals.write_decision(state, f"tool-{i}", "allow", "ok")
    # Only 5 named files, no .tmp / hidden artifacts.
    contents = list(approvals.decisions_dir(state).iterdir())
    assert len(contents) == 5
    assert all(not p.name.startswith(".") and not p.name.endswith(".tmp")
               for p in contents)


# ── Request scanning ────────────────────────────────────────────────────


def _write_request(state: Path, tool_use_id: str, **extra) -> Path:
    body = {
        "tool_use_id": tool_use_id,
        "tool_name": extra.pop("tool_name", "Bash"),
        "tool_input": extra.pop("tool_input", {"command": "echo hi"}),
        "session_id": extra.pop("session_id", "sess-123"),
        **extra,
    }
    p = approvals.requests_dir(state) / f"{tool_use_id}.json"
    p.write_text(json.dumps(body))
    return p


def test_scan_pending_returns_parsed_requests(state: Path) -> None:
    _write_request(state, "tu_1", tool_name="Write")
    _write_request(state, "tu_2", tool_name="Bash")
    requests = approvals.scan_pending_requests(state)
    assert len(requests) == 2
    by_id = {r.tool_use_id: r for r in requests}
    assert by_id["tu_1"].tool_name == "Write"
    assert by_id["tu_2"].tool_name == "Bash"
    assert by_id["tu_2"].tool_input == {"command": "echo hi"}


def test_scan_pending_skips_dotfiles(state: Path) -> None:
    """Atomic-write tempfiles use a leading dot; we ignore them."""
    _write_request(state, "tu_real")
    (approvals.requests_dir(state) / ".tu_temp.abcd1234").write_text("{}")
    requests = approvals.scan_pending_requests(state)
    assert {r.tool_use_id for r in requests} == {"tu_real"}


def test_archive_request_moves_file_with_status(state: Path) -> None:
    _write_request(state, "tu_done")
    approvals.archive_request(state, "tu_done", "approved")
    assert not (approvals.requests_dir(state) / "tu_done.json").exists()
    archived = list(approvals.archive_dir(state).glob("tu_done.approved.*"))
    assert len(archived) == 1


# ── DB CRUD ──────────────────────────────────────────────────────────────


def test_insert_pending_round_trips_through_get_by_id(db: sqlite3.Connection) -> None:
    approvals.insert_pending(
        db,
        approval_id="appr-1",
        thread_ts="123.45",
        tool_use_id="tu_x",
        tool_name="Write",
        tool_input_json=json.dumps({"file_path": "/tmp/x.txt"}),
        session_id="sess-1",
        slack_channel="D123",
        slack_message_ts="987.65",
    )
    row = approvals.get_by_id(db, "appr-1")
    assert row is not None
    assert row["status"] == "pending"
    assert row["tool_use_id"] == "tu_x"
    assert row["tool_name"] == "Write"
    assert row["session_id"] == "sess-1"


def test_get_by_tool_use_id_returns_latest(db: sqlite3.Connection) -> None:
    """If somehow two rows share a tool_use_id, return the newest."""
    for i, ts in enumerate([1000, 2000, 3000]):
        db.execute(
            """INSERT INTO pending_approvals
                (id, thread_ts, action_type, payload_json, status, slack_message_ts,
                 created_at, tool_use_id, tool_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"appr-{i}", "1.1", "Bash", "{}", "pending", None,
             ts, "tu_dup", "Bash"),
        )
    row = approvals.get_by_tool_use_id(db, "tu_dup")
    assert row is not None
    assert row["id"] == "appr-2"  # newest by created_at


def test_resolve_marks_status_and_reason(db: sqlite3.Connection) -> None:
    approvals.insert_pending(
        db, approval_id="r1", thread_ts="t1", tool_use_id="tu1",
        tool_name="Write", tool_input_json="{}", session_id=None,
        slack_channel=None, slack_message_ts=None,
    )
    out = approvals.resolve(db, approval_id="r1", status="approved",
                            decision_reason="LGTM")
    assert out is not None
    assert out["status"] == "approved"
    assert out["decision_reason"] == "LGTM"
    assert out["resolved_at"] is not None
    assert out["auto_approved"] == 0


def test_resolve_auto_flag_set(db: sqlite3.Connection) -> None:
    approvals.insert_pending(
        db, approval_id="r2", thread_ts="t1", tool_use_id="tu2",
        tool_name="Write", tool_input_json="{}", session_id=None,
        slack_channel=None, slack_message_ts=None,
    )
    out = approvals.resolve(db, approval_id="r2", status="approved",
                            decision_reason="auto", auto=True)
    assert out is not None
    assert out["auto_approved"] == 1


def test_resolve_invalid_status_raises(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        approvals.resolve(db, approval_id="r1", status="???")


def test_thread_auto_approve_toggle(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
        ("1.1", "D1", "U1", 0, 0, 0),
    )
    assert approvals.thread_auto_approve(db, "1.1") is False
    approvals.set_thread_auto_approve(db, "1.1", True)
    assert approvals.thread_auto_approve(db, "1.1") is True
    approvals.set_thread_auto_approve(db, "1.1", False)
    assert approvals.thread_auto_approve(db, "1.1") is False


def test_expire_stale_pending(db: sqlite3.Connection) -> None:
    now_ms = int(time.time() * 1000)
    fresh_ts = now_ms - 60_000           # 1 min — fresh
    stale_ts = now_ms - 20 * 60 * 1000   # 20 min — stale
    db.execute(
        """INSERT INTO pending_approvals
           (id, thread_ts, action_type, payload_json, status, slack_message_ts,
            created_at, tool_use_id, tool_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("appr-fresh", "t1", "Bash", "{}", "pending", None, fresh_ts, "tu_f", "Bash"),
    )
    db.execute(
        """INSERT INTO pending_approvals
           (id, thread_ts, action_type, payload_json, status, slack_message_ts,
            created_at, tool_use_id, tool_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("appr-stale", "t1", "Bash", "{}", "pending", None, stale_ts, "tu_s", "Bash"),
    )
    n = approvals.expire_stale_pending(db, max_age_s=600)
    assert n == 1
    assert approvals.get_by_id(db, "appr-stale")["status"] == "expired"
    assert approvals.get_by_id(db, "appr-fresh")["status"] == "pending"


# ── Slack block formatting ──────────────────────────────────────────────


def test_format_request_for_slack_emits_three_buttons() -> None:
    req = approvals.ApprovalRequest(
        tool_use_id="tu_long_abc_def_ghi",
        tool_name="Bash",
        tool_input={"command": "rm -rf /tmp/x"},
        session_id="sess-1",
        cwd="/some/cwd", transcript_path=None,
        thread_ts=None,
        raw={}, request_path=Path("/tmp/x.json"),
    )
    fallback, blocks = approvals.format_request_for_slack(req)
    assert fallback == "Bash"
    actions_block = next(b for b in blocks if b["type"] == "actions")
    action_ids = [el["action_id"] for el in actions_block["elements"]]
    assert action_ids == ["approval_approve", "approval_reject", "approval_auto_thread"]
    # tool_use_id is carried in `value` (Slack's 2K limit > our short ids)
    assert all(el["value"] == "tu_long_abc_def_ghi"
               for el in actions_block["elements"])


# ── End-to-end: approval_loop dispatches new requests ───────────────────


@pytest.mark.asyncio
async def test_approval_loop_dispatches_new_requests(state: Path,
                                                     db: sqlite3.Connection) -> None:
    seen: list[str] = []

    async def on_request(req: approvals.ApprovalRequest) -> None:
        seen.append(req.tool_use_id)

    # Start the loop, drop in 2 requests, give it a couple of poll cycles.
    task = asyncio.create_task(
        approvals.approval_loop(
            db=db, state_dir=state,
            on_request=on_request, poll_interval_s=0.05,
        )
    )
    try:
        _write_request(state, "tu_a")
        _write_request(state, "tu_b")
        # Wait long enough for poll to scan twice
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    assert sorted(seen) == ["tu_a", "tu_b"]


@pytest.mark.asyncio
async def test_approval_loop_skips_already_resolved(state: Path,
                                                    db: sqlite3.Connection) -> None:
    """If we have a row in DB marked approved/rejected, don't re-dispatch."""
    # Pre-create row in approved state (simulating a bridge restart mid-session).
    approvals.insert_pending(
        db, approval_id="appr-done", thread_ts="t1", tool_use_id="tu_done",
        tool_name="Write", tool_input_json="{}", session_id=None,
        slack_channel=None, slack_message_ts=None,
    )
    approvals.resolve(db, approval_id="appr-done", status="approved",
                      decision_reason="prev session")
    _write_request(state, "tu_done")

    seen: list[str] = []

    async def on_request(req: approvals.ApprovalRequest) -> None:
        seen.append(req.tool_use_id)

    task = asyncio.create_task(
        approvals.approval_loop(
            db=db, state_dir=state,
            on_request=on_request, poll_interval_s=0.05,
        )
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    assert seen == []  # not re-dispatched
