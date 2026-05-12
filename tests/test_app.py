"""Smoke + integration tests for the Starlette app routes.

These exercise the full middleware stack via Starlette's TestClient,
which is synchronous (it drives the ASGI app with anyio under the hood
but doesn't require pytest-asyncio).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(temp_state_dir: Path, with_token: str) -> TestClient:
    """Reset the cached MCP probe + return a TestClient bound to the live app."""
    from agent_me.dashboard import app as app_module
    app_module._MCP_CACHE["servers"] = []
    app_module._MCP_CACHE["checked_at"] = 0
    return TestClient(app_module.app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_healthz_unauth_ok(temp_state_dir: Path) -> None:
    """Healthz must work even without DASHBOARD_TOKEN."""
    from agent_me.dashboard.app import app

    r = TestClient(app).get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "uptime_s" in body


def test_index_renders_html(client: TestClient, with_token: str) -> None:
    r = client.get("/", headers=_auth(with_token))
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    body = r.text
    assert "agent-me" in body
    assert "agent-me-avatar.svg" in body
    assert "Overview" in body
    # All brief sources should appear in the nav at minimum
    for label in ("Jira", "GitLab", "NVBugs",
                  "Slack", "Outlook", "Outlook Calendar", "GitHub"):
        assert label in body


def test_source_page_known_source_renders(client: TestClient, with_token: str) -> None:
    r = client.get("/source/jira", headers=_auth(with_token))
    assert r.status_code == 200
    assert "Jira" in r.text


def test_static_avatar_asset_served(client: TestClient) -> None:
    r = client.get("/static/agent-me-avatar.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers.get("content-type", "")
    assert "agent-me avatar" in r.text


def test_source_page_unknown_source_404(client: TestClient, with_token: str) -> None:
    r = client.get("/source/madeup", headers=_auth(with_token))
    assert r.status_code == 404


def test_ops_page_renders(client: TestClient, with_token: str) -> None:
    r = client.get("/ops", headers=_auth(with_token))
    assert r.status_code == 200
    assert "Operations" in r.text
    assert "MCP servers" in r.text


def test_api_state_returns_all_snapshots(client: TestClient, with_token: str) -> None:
    r = client.get("/api/state", headers=_auth(with_token))
    assert r.status_code == 200
    body = r.json()
    assert "uptime_s" in body
    assert len(body["snapshots"]) == 7
    sources = {s["source"] for s in body["snapshots"]}
    assert sources == {"jira", "gitlab", "nvbugs",
                       "slack", "outlook", "calendar", "github"}


def test_api_source_returns_cached_snapshot(client: TestClient, temp_state_dir: Path,
                                            with_token: str) -> None:
    from agent_me.dashboard.state_reader import StateReader

    payload = {
        "source": "jira",
        "items": [{"item_id": "PROJ-1", "title": "test", "url": "https://x/1",
                   "group": "PROJ", "icon": "📋", "source": "jira"}],
        "error": None,
        "fetched_at": int(time.time() * 1000),
        "seconds": 5,
    }
    StateReader.write_cache("jira", payload)
    r = client.get("/api/source/jira", headers=_auth(with_token))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "jira"
    assert body["items_count"] == 1


def test_api_source_unknown_returns_404(client: TestClient, with_token: str) -> None:
    r = client.get("/api/source/madeup", headers=_auth(with_token))
    assert r.status_code == 404


def test_login_page_unauth_renders(temp_state_dir: Path) -> None:
    from agent_me.dashboard.app import app

    r = TestClient(app).get("/login")
    assert r.status_code == 200
    assert "DASHBOARD_TOKEN" in r.text


def test_unauth_root_returns_401(temp_state_dir: Path, with_token: str) -> None:
    from agent_me.dashboard.app import app

    r = TestClient(app).get("/")
    assert r.status_code == 401


def test_refresh_all_returns_all_jobs(client: TestClient, monkeypatch,
                                      with_token: str) -> None:
    """The `/api/refresh/_all` endpoint should kick off one job per source.

    We patch `BriefRunner.start` so it returns immediately without
    spawning a real `codex exec` subprocess; the test only verifies the
    endpoint shape + that all sources are scheduled.
    """
    import uuid

    from agent_me.dashboard import app as app_module
    from agent_me.dashboard.brief_runner import BriefJob

    started: list[str] = []

    async def fake_start(self, source: str, period_days: int = 1):
        started.append(source)
        return BriefJob(
            job_id=uuid.uuid4().hex[:8], source=source,
            started_at=int(time.time() * 1000), status="pending",
        )

    monkeypatch.setattr(app_module.RUNNER.__class__, "start", fake_start)
    # active_job_for stays default (returns None) so nothing coalesces.

    r = client.post("/api/refresh/_all", headers=_auth(with_token))
    assert r.status_code == 202
    body = r.json()
    assert len(body["jobs"]) == 7
    assert sorted(j["source"] for j in body["jobs"]) == sorted([
        "calendar", "github", "gitlab", "jira", "nvbugs", "outlook", "slack"
    ])
    assert sorted(started) == sorted([
        "calendar", "github", "gitlab", "jira", "nvbugs", "outlook", "slack"
    ])
