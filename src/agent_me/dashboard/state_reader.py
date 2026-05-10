"""Read-only access to the bridge's SQLite state + brief cache + logs.

Three responsibilities:

1. **SQLite RO**: open `state.db` in read-only mode (URI form
   `file:...?mode=ro`) and expose snapshot queries for the
   `threads`, `messages`, `pending_approvals`, `claude_sessions`
   tables. Each call uses a fresh connection — no long-running
   transactions, no shared writers.

2. **Brief cache**: parse the on-disk dashboard cache files at
   `${STATE_DIR}/dashboard-cache/<source>.json` (written by the
   on-demand refresh job in `brief_runner`) and the most recent
   morning brief output (read from `brief.log` if no cache exists
   yet).

3. **Log tail**: async generator over the rotating JSON log files
   produced by the bridge (`bridge.log`) and the brief subprocess
   (`brief.log`). Used by the SSE log endpoint.

The class `StateReader` is a thin façade so the Starlette routes
get a single dependency to inject.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger("dashboard.state")

# ── Layout (mirrors bridge / brief; keep these in lockstep) ──────────────


def resolve_state_dir() -> Path:
    """Same logic as the bridge — single source of truth via env vars."""
    if env := os.environ.get("AGENT_ME_STATE_DIR"):
        return Path(env).expanduser()
    if env := os.environ.get("XDG_STATE_HOME"):
        return Path(env).expanduser() / "agent-me"
    return Path.home() / ".local" / "state" / "agent-me"


STATE_DIR = resolve_state_dir()
DB_PATH = STATE_DIR / "state.db"
BRIDGE_LOG = STATE_DIR / "bridge.log"
BRIEF_LOG = STATE_DIR / "brief.log"
CACHE_DIR = STATE_DIR / "dashboard-cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Sources mirrored from `daily_brief.py`. Keep manually in sync; the
# brief refactor that introduced fan-out hard-codes these too, so this
# is acceptable duplication for the draft.
SOURCES: tuple[tuple[str, str, str], ...] = (
    ("jira", "Jira", "📋"),
    ("gitlab", "GitLab", "🦊"),
    ("confluence", "Confluence", "📚"),
    ("nvbugs", "NVBugs", "🐛"),
    ("slack", "Slack", "💬"),
    ("outlook", "Outlook", "📧"),
    ("github", "GitHub", "🐱"),
)
SOURCE_IDS = {s[0] for s in SOURCES}


# ── DB connection helpers ────────────────────────────────────────────────


def _ro_connect() -> sqlite3.Connection:
    """Open `state.db` strictly read-only.

    URI form `file:.../state.db?mode=ro` is the canonical way to ask
    SQLite for a read-only handle that still respects WAL — i.e. it
    sees writer-committed data without blocking the writer. If the
    file doesn't exist yet (bridge never started), we return a
    connection to an in-memory empty DB so callers don't need to
    branch on existence.

    `busy_timeout` is set to 1500 ms so that the rare case of a
    checkpoint blocking a read returns briefly-blocked instead of
    immediately raising `database is locked`. WAL readers don't take
    locks that block writers, but the inverse can briefly bite during
    a passive checkpoint.
    """
    if not DB_PATH.exists():
        log.warning("state_db_missing", path=str(DB_PATH))
        return sqlite3.connect(":memory:")
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False,
                           isolation_level=None, timeout=1.5)
    conn.row_factory = sqlite3.Row
    with contextlib.suppress(sqlite3.Error):
        conn.execute("PRAGMA busy_timeout = 1500")
    return conn


# ── DTOs (kept loose; templates can pull anything off the dict) ──────────


@dataclass
class BridgeStats:
    threads_total: int = 0
    threads_active_24h: int = 0
    sessions_total: int = 0
    pending_approvals: int = 0
    last_thread_active_at: int | None = None  # ms epoch
    last_session_used_at: int | None = None


@dataclass
class BriefSnapshot:
    """Most recent brief output for a single source.

    `items` is a list[dict] (raw `BriefItem.__dict__` layout from the
    brief subprocess) so templates can iterate without us having to
    keep a parallel dataclass in sync with `BriefItem`.
    """
    source: str
    label: str
    icon: str
    items: list[dict[str, Any]]
    error: str | None
    fetched_at: int  # ms epoch
    seconds: int = 0
    stale: bool = False  # True if older than 24h OR cache file missing


@dataclass
class McpStatus:
    name: str
    connected: bool
    needs_auth: bool
    raw_line: str = ""


@dataclass
class OpsSnapshot:
    bridge_stats: BridgeStats
    mcps: list[McpStatus] = field(default_factory=list)
    mcps_checked_at: int | None = None
    recent_brief_runs: list[dict[str, Any]] = field(default_factory=list)


# ── Public API ───────────────────────────────────────────────────────────


class StateReader:
    """Stateless façade — one shared instance is fine."""

    @staticmethod
    def bridge_stats() -> BridgeStats:
        conn = _ro_connect()
        try:
            now_ms = int(time.time() * 1000)
            cutoff_24h = now_ms - 24 * 60 * 60 * 1000

            stats = BridgeStats()
            try:
                stats.threads_total = (
                    conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
                )
                stats.threads_active_24h = conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE last_active_at >= ?",
                    (cutoff_24h,),
                ).fetchone()[0]
                stats.sessions_total = (
                    conn.execute("SELECT COUNT(*) FROM claude_sessions").fetchone()[0]
                )
                stats.pending_approvals = conn.execute(
                    "SELECT COUNT(*) FROM pending_approvals WHERE status='pending'"
                ).fetchone()[0]
                row = conn.execute(
                    "SELECT MAX(last_active_at) FROM threads"
                ).fetchone()
                stats.last_thread_active_at = row[0] if row and row[0] else None
                row = conn.execute(
                    "SELECT MAX(last_used_at) FROM claude_sessions"
                ).fetchone()
                stats.last_session_used_at = row[0] if row and row[0] else None
            except sqlite3.Error as exc:
                # Schema drift / fresh DB / table missing / DB busy —
                # all soft fails. Caller gets a zeroed BridgeStats.
                log.warning("bridge_stats_query_failed", err=str(exc))
            return stats
        finally:
            conn.close()

    @staticmethod
    def recent_threads(limit: int = 20) -> list[dict[str, Any]]:
        conn = _ro_connect()
        try:
            try:
                rows = conn.execute(
                    """
                    SELECT t.thread_ts, t.channel, t.user_id, t.auto_approve,
                           t.last_active_at,
                           (SELECT COUNT(*) FROM messages m WHERE m.thread_ts = t.thread_ts) AS msg_count,
                           (SELECT s.session_id FROM claude_sessions s
                              WHERE s.thread_ts = t.thread_ts) AS session_id,
                           (SELECT s.turn_count FROM claude_sessions s
                              WHERE s.thread_ts = t.thread_ts) AS turn_count
                    FROM threads t
                    ORDER BY t.last_active_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.Error as exc:
                log.warning("recent_threads_failed", err=str(exc))
                return []
        finally:
            conn.close()

    @staticmethod
    def pending_approvals() -> list[dict[str, Any]]:
        conn = _ro_connect()
        try:
            try:
                rows = conn.execute(
                    """SELECT id, thread_ts, action_type, status, created_at, resolved_at
                         FROM pending_approvals
                        ORDER BY created_at DESC
                        LIMIT 50"""
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.Error:
                return []
        finally:
            conn.close()

    # ── Brief snapshots ──────────────────────────────────────────────────

    @staticmethod
    def cache_file(source: str) -> Path:
        if source not in SOURCE_IDS:
            raise ValueError(f"unknown source: {source!r}")
        return CACHE_DIR / f"{source}.json"

    @classmethod
    def brief_snapshot(cls, source: str) -> BriefSnapshot:
        """Latest brief items for one source.

        Lookup order:
        1. Dashboard cache file (written by `brief_runner`).
        2. Most recent successful entry in `brief.log` for that source.
        3. Empty list with `stale=True`.
        """
        for src_id, _label, _icon in SOURCES:
            if src_id == source:
                label, icon = _label, _icon
                break
        else:
            raise ValueError(f"unknown source: {source!r}")

        path = cls.cache_file(source)
        now_ms = int(time.time() * 1000)
        if path.exists():
            try:
                blob = json.loads(path.read_text())
                fetched_at = int(blob.get("fetched_at", 0))
                stale = (now_ms - fetched_at) > 24 * 60 * 60 * 1000
                return BriefSnapshot(
                    source=src_id,
                    label=label,
                    icon=icon,
                    items=blob.get("items", []),
                    error=blob.get("error"),
                    fetched_at=fetched_at,
                    seconds=int(blob.get("seconds", 0)),
                    stale=stale,
                )
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("cache_read_failed", source=source, err=str(exc))

        # Fall back to brief.log scrape — best-effort, returns empty if
        # the bridge hasn't run a brief yet on this host.
        items = _scrape_brief_log_for_source(source)
        return BriefSnapshot(
            source=src_id,
            label=label,
            icon=icon,
            items=items,
            error=None,
            fetched_at=0,
            stale=True,
        )

    @classmethod
    def all_snapshots(cls) -> list[BriefSnapshot]:
        return [cls.brief_snapshot(s[0]) for s in SOURCES]

    @staticmethod
    def write_cache(source: str, payload: dict[str, Any]) -> None:
        path = StateReader.cache_file(source)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    # ── Recent brief.log runs (for ops panel) ───────────────────────────

    @staticmethod
    def recent_brief_runs(limit: int = 10) -> list[dict[str, Any]]:
        """Scan brief.log for `fan_out_done` events; newest first.

        Each `fan_out_done` line is a JSON-line containing the per-source
        elapsed seconds and total. We return the last `limit` of those.
        """
        if not BRIEF_LOG.exists():
            return []
        runs: list[dict[str, Any]] = []
        try:
            # Tail-bias read: just slurp the file (rotating JSON, capped
            # at 10 MB x 5). Cheap.
            for line in BRIEF_LOG.read_text(errors="replace").splitlines():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("event") == "fan_out_done":
                    runs.append(obj)
        except OSError as exc:
            log.warning("brief_log_read_failed", err=str(exc))
            return []
        return list(reversed(runs[-limit:]))

    # ── Log tail (async generator for SSE) ──────────────────────────────

    @staticmethod
    async def tail_logs(
        sources: tuple[Path, ...] = (BRIDGE_LOG, BRIEF_LOG),
        from_lines: int = 50,
        poll_interval_s: float = 0.5,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield last `from_lines` of each source, then stream new lines.

        Output shape: `{"source": "bridge" | "brief", "line": <raw json
        decoded as dict, or {"raw": <str>} if not JSON>, "ts": <ms>}`.
        Stops only when the consumer cancels.

        Rotation handling: structlog's RotatingFileHandler renames
        `bridge.log` → `bridge.log.1` and creates a fresh `bridge.log`,
        which means the path stays valid but points to a new inode.
        Detection has to handle three rotation flavours:
          1. Truncate-in-place — new size < tracked offset → reset offset.
          2. Rename + create new — new inode at same path → reset offset.
          3. File temporarily missing during rotation → skip iteration,
             pick up next loop.
        """
        # Replay tail
        for path in sources:
            label = path.stem  # "bridge" / "brief"
            if not path.exists():
                continue
            try:
                lines = path.read_text(errors="replace").splitlines()[-from_lines:]
            except OSError:
                lines = []
            for raw in lines:
                yield {"source": label, "line": _try_parse_json(raw),
                       "ts": int(time.time() * 1000), "replay": True}

        # Live tail. Track (size, inode) per path so we can detect
        # rotation even when the new file has grown past the old size
        # before our next poll.
        positions: dict[Path, tuple[int, int]] = {}
        for p in sources:
            positions[p] = _safe_stat(p)

        while True:
            await asyncio.sleep(poll_interval_s)
            for path in sources:
                label = path.stem
                if not path.exists():
                    continue
                cur_size, cur_inode = _safe_stat(path)
                old_size, old_inode = positions[path]

                rotated = (cur_inode != old_inode) or (cur_size < old_size)
                if rotated:
                    # New file (or truncation): start from byte 0.
                    old_size = 0
                if cur_size == old_size:
                    positions[path] = (cur_size, cur_inode)
                    continue
                try:
                    with path.open("rb") as f:
                        f.seek(old_size)
                        data = f.read(cur_size - old_size)
                except OSError:
                    continue
                positions[path] = (cur_size, cur_inode)
                if rotated:
                    yield {"source": label,
                           "line": {"event": "log_rotated",
                                    "note": f"{path.name} rotated; resuming from offset 0"},
                           "ts": int(time.time() * 1000), "replay": False}
                for raw in data.decode(errors="replace").splitlines():
                    if not raw.strip():
                        continue
                    yield {"source": label, "line": _try_parse_json(raw),
                           "ts": int(time.time() * 1000), "replay": False}


def _safe_stat(path: Path) -> tuple[int, int]:
    """Return (size, inode) or (0, 0) if the file is missing.

    Both fields are tracked so log rotation that creates a new file
    (different inode) is distinguishable from in-place writes.
    """
    try:
        st = path.stat()
        return st.st_size, st.st_ino
    except OSError:
        return 0, 0


def _try_parse_json(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _scrape_brief_log_for_source(source: str) -> list[dict[str, Any]]:
    """Best-effort: pull the most recent `subagent_done` items dict.

    The brief subagents emit a `subagent_done` log event with the
    item count; the actual items aren't logged (they go straight to
    Slack). So this fallback returns an empty list — telling the UI
    "no cache yet, click Refresh". Documented as deliberate.
    """
    return []


# ── MCP health (synchronous shell-out to `claude mcp list`) ─────────────


_MCP_LINE_RE = re.compile(
    r"^(?P<name>maas-[a-z0-9_-]+):\s+.*?-\s+(?P<state>✓\s+Connected|!\s+Needs\s+authentication|✗\s+Failed.*)$"
)


async def check_mcp_health(timeout_s: float = 15.0) -> tuple[list[McpStatus], int]:
    """Run `claude mcp list` and parse it. Returns (servers, checked_at_ms).

    Cached at the call site; this function is the raw probe.
    """
    proc = await asyncio.create_subprocess_exec(
        "claude", "mcp", "list",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return [], int(time.time() * 1000)

    servers: list[McpStatus] = []
    for line in stdout.decode(errors="replace").splitlines():
        m = _MCP_LINE_RE.search(line)
        if not m:
            continue
        state = m.group("state")
        servers.append(McpStatus(
            name=m.group("name"),
            connected=state.startswith("✓"),
            needs_auth=state.startswith("!"),
            raw_line=line.strip(),
        ))
    return servers, int(time.time() * 1000)
