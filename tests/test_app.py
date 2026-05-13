"""Smoke + integration tests for the Starlette app routes.

These exercise the full middleware stack via Starlette's TestClient,
which is synchronous (it drives the ASGI app with anyio under the hood
but doesn't require pytest-asyncio).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(temp_state_dir: Path, with_token: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Reset the cached MCP probe + return a TestClient bound to the live app."""
    monkeypatch.setenv("DASHBOARD_OPERATOR_ACTION_CODE", "test-operator-code")
    from agent_me.dashboard import app as app_module
    app_module._MCP_CACHE["servers"] = []
    app_module._MCP_CACHE["checked_at"] = 0
    return TestClient(app_module.app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _operator_auth(token: str) -> dict[str, str]:
    return {**_auth(token), "X-Agent-Me-Action-Code": "test-operator-code"}


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
    assert "Agent Me" in body
    assert "agent-me" in body
    assert "agent-me-avatar.svg" in body
    assert "Overview" in body
    assert 'href="/auto-sfa"' in body
    assert "Hey hey hey, this button is for Thanh Phan only :)" in body
    assert "Auto SFA" in body
    assert "Operator actions" in body
    assert "Refresh MCP auth" in body
    assert "Pending across platforms" in body
    assert "Briefs by source" not in body
    # All brief sources should appear in the nav at minimum
    for label in ("Jira", "GitLab", "NVBugs",
                  "Slack", "Outlook", "Outlook Calendar", "GitHub"):
        assert label in body


def test_index_pending_uses_brief_cache(client: TestClient, temp_state_dir: Path,
                                        with_token: str) -> None:
    from agent_me.dashboard.state_reader import StateReader

    StateReader.write_cache("jira", {
        "source": "jira",
        "items": [{
            "source": "jira",
            "icon": "📋",
            "item_id": "PROJ-1",
            "title": "Cached Jira task",
            "url": "https://jirasw.nvidia.com/browse/PROJ-1",
            "group": "PROJ",
            "priority": "P1",
            "deadline": "2026-05-20",
            "last_activity": "2026-05-13T00:00:00Z",
        }],
        "error": None,
        "fetched_at": int(time.time() * 1000),
        "seconds": 4,
    })
    r = client.get("/", headers=_auth(with_token))
    assert r.status_code == 200
    assert "PROJ-1" in r.text
    assert "Cached Jira task" in r.text
    assert "IPP-4521" not in r.text


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
    assert "Refresh auth" in r.text
    assert "No MCP probe cached yet" in r.text


def test_auto_sfa_page_renders(client: TestClient, with_token: str) -> None:
    r = client.get("/auto-sfa", headers=_auth(with_token))
    assert r.status_code == 200
    assert "Auto SFA" in r.text
    assert "display_name" in r.text
    assert "placeholder=\"Thanh Phan\"" in r.text
    assert "DevTest credentials" in r.text
    assert "specific task IDs" in r.text
    assert "destination_folder_id" in r.text
    assert "05-2026/Week3-4" in r.text
    assert "Merge Request / MR link" not in r.text
    assert "required templates already exist in SFA" in r.text
    assert "dashboard-date-input" in r.text
    assert "dashboard-date-field" in r.text


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


def test_api_mcp_auth_refresh_returns_report(client: TestClient, monkeypatch,
                                             with_token: str) -> None:
    from agent_me.dashboard import app as app_module
    from agent_me.dashboard.state_reader import McpStatus
    from agent_me.mcp_tokens import McpTokenRefreshReport

    def fake_refresh_mcp_tokens(force: bool = False):
        assert force is True
        return McpTokenRefreshReport(
            attempted=("maas-jira", "maas-nvbugs"),
            refreshed=("maas-nvbugs",),
            failed={"maas-jira": "HTTP 400: rejected"},
        )

    def fake_refresh_env(refresh_tokens: bool = True):
        assert refresh_tokens is False
        return 2

    async def fake_check_mcp_health():
        return [
            McpStatus("maas-jira", connected=True, needs_auth=False),
            McpStatus("maas-nvbugs", connected=True, needs_auth=False),
        ], 12345

    monkeypatch.setattr(app_module, "refresh_mcp_tokens", fake_refresh_mcp_tokens)
    monkeypatch.setattr(app_module, "refresh_codex_mcp_env_file", fake_refresh_env)
    monkeypatch.setattr(app_module, "check_mcp_health", fake_check_mcp_health)

    r = client.post("/api/mcp/auth-refresh", headers=_operator_auth(with_token))

    assert r.status_code == 200
    body = r.json()
    assert body["attempted"] == ["maas-jira", "maas-nvbugs"]
    assert body["refreshed"] == ["maas-nvbugs"]
    assert body["failed"] == {"maas-jira": "HTTP 400: rejected"}
    assert body["needs_mac_sync"] is True
    assert body["env_exports"] == 2
    assert body["checked_at"] == 12345
    assert len(body["servers"]) == 2


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

    r = client.post("/api/refresh/_all", headers=_operator_auth(with_token))
    assert r.status_code == 202
    body = r.json()
    assert len(body["jobs"]) == 7
    assert sorted(j["source"] for j in body["jobs"]) == sorted([
        "calendar", "github", "gitlab", "jira", "nvbugs", "outlook", "slack"
    ])
    assert sorted(started) == sorted([
        "calendar", "github", "gitlab", "jira", "nvbugs", "outlook", "slack"
    ])


def test_api_auto_sfa_run_starts_job(client: TestClient, monkeypatch,
                                     with_token: str) -> None:
    import uuid

    from agent_me.dashboard import app as app_module
    from agent_me.dashboard.auto_sfa_runner import AutoSFAJob

    captured = {}

    async def fake_start(self, request):
        captured["request"] = request
        return AutoSFAJob(
            job_id=uuid.uuid4().hex[:8],
            started_at=int(time.time() * 1000),
            request=request,
            status="pending",
        )

    monkeypatch.setattr(app_module.AUTO_SFA_RUNNER.__class__, "start", fake_start)

    r = client.post(
        "/api/auto-sfa/run",
        json={
            "display_name": "Thanh Phan",
            "devtest_folder_id": "1155188",
            "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
            "start_date": "2026-04-16",
            "finish_date": "2026-04-27",
            "task_ids_enabled": True,
            "task_ids": "824423,824424",
            "use_personal_credentials": True,
            "auth_username": "thaphan",
            "auth_password": "dummy-password",
        },
        headers=_auth(with_token),
    )

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    assert captured["request"].display_name == "Thanh Phan"
    assert captured["request"].devtest_folder_id == 1155188
    assert captured["request"].task_ids == "824423,824424"
    assert captured["request"].auth_username == "thaphan"
    assert captured["request"].auth_password == "dummy-password"
    assert "dummy-password" not in json.dumps(body)


def test_operator_action_endpoints_require_passcode(client: TestClient,
                                                    with_token: str) -> None:
    r = client.post("/api/refresh/_all", headers=_auth(with_token))
    assert r.status_code == 403
    assert r.json()["error"] == "operator passcode required"

    r = client.post("/api/mcp/auth-refresh", headers={
        **_auth(with_token),
        "X-Agent-Me-Action-Code": "pumpk!n",
    })
    assert r.status_code == 403


def test_api_auto_sfa_run_rejects_bad_input(client: TestClient,
                                            with_token: str) -> None:
    r = client.post(
        "/api/auto-sfa/run",
        json={
            "display_name": "thaphan",
            "devtest_folder_id": "not-a-number",
            "url_path": "not-a-url",
            "start_date": "2026-04-16",
            "finish_date": "2026-04-27",
        },
        headers=_auth(with_token),
    )

    assert r.status_code == 400
    body = r.json()
    assert "errors" in body
