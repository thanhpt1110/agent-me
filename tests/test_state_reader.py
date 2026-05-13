"""state_reader: read-only SQLite + brief cache + log scrape."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def test_bridge_stats_returns_zeros_when_db_missing(temp_state_dir: Path) -> None:
    """No state.db on disk yet (fresh host) → zeros, no exception."""
    from agent_me.dashboard.state_reader import StateReader

    stats = StateReader.bridge_stats()
    assert stats.threads_total == 0
    assert stats.threads_active_24h == 0
    assert stats.sessions_total == 0
    assert stats.pending_approvals == 0
    assert stats.last_thread_active_at is None


def test_bridge_stats_with_seeded_db(seeded_db: Path) -> None:
    from agent_me.dashboard.state_reader import StateReader

    stats = StateReader.bridge_stats()
    assert stats.threads_total == 2  # one fresh, one stale
    assert stats.threads_active_24h == 1
    assert stats.sessions_total == 1
    assert stats.pending_approvals == 1
    assert stats.last_thread_active_at is not None


def test_recent_threads_orders_by_last_active(seeded_db: Path) -> None:
    from agent_me.dashboard.state_reader import StateReader

    threads = StateReader.recent_threads(limit=10)
    assert len(threads) == 2
    # Active thread (1234.5) has more recent last_active_at → first.
    assert threads[0]["thread_ts"] == "1234.5"
    assert threads[0]["msg_count"] == 1
    assert threads[0]["session_id"] == "session-abc"
    assert threads[0]["turn_count"] == 3


def test_pending_approvals_returns_pending_only(seeded_db: Path) -> None:
    from agent_me.dashboard.state_reader import StateReader

    approvals = StateReader.pending_approvals()
    assert len(approvals) == 1
    assert approvals[0]["status"] == "pending"
    assert approvals[0]["action_type"] == "jira_create_issue"


def test_brief_snapshot_returns_stale_when_no_cache(temp_state_dir: Path) -> None:
    from agent_me.dashboard.state_reader import StateReader

    snap = StateReader.brief_snapshot("jira")
    assert snap.source == "jira"
    assert snap.label == "Jira"
    assert snap.items == []
    assert snap.stale is True
    assert snap.fetched_at == 0
    assert snap.error is None


def test_brief_snapshot_reads_cache_and_marks_fresh(temp_state_dir: Path) -> None:
    from agent_me.dashboard.state_reader import StateReader

    payload = {
        "source": "gitlab",
        "items": [{"item_id": "!42", "title": "review me", "url": "https://x/42",
                   "group": "repo-a", "icon": "🦊", "source": "gitlab"}],
        "error": None,
        "fetched_at": int(time.time() * 1000) - 60_000,  # 1 min ago
        "seconds": 12,
    }
    StateReader.write_cache("gitlab", payload)
    snap = StateReader.brief_snapshot("gitlab")
    assert len(snap.items) == 1
    assert snap.items[0]["item_id"] == "!42"
    assert snap.stale is False
    assert snap.seconds == 12


def test_brief_snapshot_marks_stale_after_24h(temp_state_dir: Path) -> None:
    from agent_me.dashboard.state_reader import StateReader

    payload = {
        "source": "jira", "items": [], "error": None,
        "fetched_at": int(time.time() * 1000) - (25 * 60 * 60 * 1000),
        "seconds": 0,
    }
    StateReader.write_cache("jira", payload)
    snap = StateReader.brief_snapshot("jira")
    assert snap.stale is True


def test_brief_snapshot_unknown_source_raises(temp_state_dir: Path) -> None:
    from agent_me.dashboard.state_reader import StateReader

    with pytest.raises(ValueError):
        StateReader.brief_snapshot("nonexistent")


def test_recent_brief_runs_empty_when_no_log(temp_state_dir: Path) -> None:
    from agent_me.dashboard.state_reader import StateReader

    assert StateReader.recent_brief_runs() == []


def test_recent_brief_runs_parses_fan_out_done_entries(temp_state_dir: Path) -> None:
    from agent_me.dashboard import state_reader
    from agent_me.dashboard.state_reader import StateReader

    log_lines = [
        json.dumps({"event": "brief_starting", "level": "info"}),
        json.dumps({"event": "subagent_done", "source": "jira"}),
        json.dumps({"event": "fan_out_done", "total_items": 42,
                    "total_seconds": 39, "err_count": 0,
                    "timestamp": "2026-05-10T06:00:00"}),
        json.dumps({"event": "fan_out_done", "total_items": 18,
                    "total_seconds": 27, "err_count": 1,
                    "timestamp": "2026-05-11T06:00:00"}),
        "not-json-noise-line",
    ]
    state_reader.BRIEF_LOG.write_text("\n".join(log_lines) + "\n")
    runs = StateReader.recent_brief_runs(limit=5)
    assert len(runs) == 2
    # newest first
    assert runs[0]["timestamp"] == "2026-05-11T06:00:00"
    assert runs[0]["err_count"] == 1
    assert runs[1]["total_items"] == 42


def test_all_snapshots_returns_all_sources(temp_state_dir: Path) -> None:
    from agent_me.dashboard.state_reader import SOURCES, StateReader

    snaps = StateReader.all_snapshots()
    assert len(snaps) == len(SOURCES) == 7
    assert {s.source for s in snaps} == {
        "jira", "gitlab", "nvbugs", "slack", "outlook", "calendar", "github"
    }


def test_parse_mcp_list_output_supports_codex_table() -> None:
    from agent_me.dashboard.state_reader import parse_mcp_list_output

    output = """Name                  Url                                                   Bearer Token Env Var                Status   Auth
maas-jira             https://nvaihub.nvidia.com/maas/jira/mcp/             AGENT_ME_MCP_TOKEN_MAAS_JIRA        enabled  Bearer token
maas-nvbugs           https://nvaihub.nvidia.com/maas/nvbugs/mcp/           AGENT_ME_MCP_TOKEN_MAAS_NVBUGS      enabled  Needs authentication
maas-playwright       npx -y @playwright/mcp@latest                         -                                   enabled  Unsupported
"""

    servers = parse_mcp_list_output(output)

    assert [(s.name, s.connected, s.needs_auth) for s in servers] == [
        ("maas-jira", True, False),
        ("maas-nvbugs", False, True),
        ("maas-playwright", True, False),
    ]


def test_parse_mcp_list_output_supports_legacy_lines() -> None:
    from agent_me.dashboard.state_reader import parse_mcp_list_output

    servers = parse_mcp_list_output(
        "maas-jira: https://example/mcp - ✓ Connected\n"
        "maas-gitlab: https://example/mcp - ! Needs authentication\n"
    )

    assert [(s.name, s.connected, s.needs_auth) for s in servers] == [
        ("maas-jira", True, False),
        ("maas-gitlab", False, True),
    ]
