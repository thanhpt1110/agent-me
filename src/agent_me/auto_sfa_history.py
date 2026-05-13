"""Persisted Auto SFA trigger history."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger("auto_sfa.history")

AUTO_SFA_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_sfa_runs (
    run_id         TEXT PRIMARY KEY,
    triggered_at   INTEGER NOT NULL,
    display_name   TEXT NOT NULL,
    status         TEXT NOT NULL,
    trigger_source TEXT NOT NULL DEFAULT 'dashboard',
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS auto_sfa_runs_triggered_idx
    ON auto_sfa_runs(triggered_at DESC);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=1.5)
    conn.row_factory = sqlite3.Row
    with contextlib.suppress(sqlite3.Error):
        conn.execute("PRAGMA busy_timeout = 1500")
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(AUTO_SFA_HISTORY_SCHEMA)


def _time_label(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def record_auto_sfa_run(
    db_path: Path,
    *,
    run_id: str,
    triggered_at_ms: int,
    display_name: str,
    status: str,
    trigger_source: str = "dashboard",
    updated_at_ms: int | None = None,
) -> None:
    """Insert or update one Auto SFA trigger row."""
    now_ms = updated_at_ms or int(time.time() * 1000)
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO auto_sfa_runs
                (run_id, triggered_at, display_name, status, trigger_source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                display_name=excluded.display_name,
                status=excluded.status,
                trigger_source=excluded.trigger_source,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                triggered_at_ms,
                display_name.strip() or "(unknown)",
                status,
                trigger_source,
                now_ms,
            ),
        )
    except sqlite3.Error as exc:
        log.warning("auto_sfa_history_write_failed", err=str(exc), db_path=str(db_path))
    finally:
        conn.close()


def recent_auto_sfa_runs(db_path: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    """Return the latest Auto SFA trigger rows for dashboard rendering."""
    safe_limit = max(1, min(int(limit), 500))
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT run_id, triggered_at, display_name, status, trigger_source, updated_at
            FROM auto_sfa_runs
            ORDER BY triggered_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        runs: list[dict[str, Any]] = []
        for row in rows:
            triggered_at = int(row["triggered_at"])
            runs.append({
                "run_id": row["run_id"],
                "triggered_at": triggered_at,
                "triggered_at_label": _time_label(triggered_at),
                "display_name": row["display_name"],
                "status": row["status"],
                "trigger_source": row["trigger_source"],
                "updated_at": int(row["updated_at"]),
            })
        return runs
    except sqlite3.Error as exc:
        log.warning("auto_sfa_history_read_failed", err=str(exc), db_path=str(db_path))
        return []
    finally:
        conn.close()
