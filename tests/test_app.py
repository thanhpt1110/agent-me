"""Smoke + integration tests for the Starlette app routes.

These exercise the full middleware stack via Starlette's TestClient,
which is synchronous (it drives the ASGI app with anyio under the hood
but doesn't require pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(temp_state_dir: Path, with_token: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Reset the cached MCP probe + return a TestClient bound to the live app."""
    monkeypatch.setenv("DASHBOARD_OPERATOR_ACTION_CODE", "test-operator-code")
    monkeypatch.setenv("AGENT_ME_RELEASE_TAG", "v9.9.9")
    monkeypatch.setenv("AGENT_ME_RELEASE_DATE", "2099-01-02")
    from agent_me.dashboard import app as app_module
    app_module._MCP_CACHE["servers"] = []
    app_module._MCP_CACHE["checked_at"] = 0
    app_module._RELEASE_CACHE["info"] = None
    app_module._RELEASE_CACHE["checked_at"] = 0
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
    assert "Built by Thanh Phan" in body
    assert 'href="mailto:thaphan@nvidia.com">thaphan@nvidia.com</a>' in body
    assert "NVIDIA VRDC SWQA" in body
    assert "Last Update" in body
    assert 'href="https://github.com/thanhpt1110/agent-me/releases/tag/v9.9.9"' in body
    assert "v9.9.9</a>:" in body
    assert "2099-01-02" in body
    assert "NVIDIA Internal Automation" not in body
    assert "Auto SFA MCP · updated" not in body
    assert "agent-me.theme.v1" in body
    assert "data-theme-toggle" in body
    assert "prefers-color-scheme: dark" in body
    # All brief sources should appear in the nav at minimum
    for label in ("NVBugs", "GitLab", "GitHub", "Meetings",
                  "Email", "Jira", "Teams", "Slack"):
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


def test_index_pending_calendar_shows_meeting_time(client: TestClient, temp_state_dir: Path,
                                                   with_token: str) -> None:
    from agent_me.dashboard.state_reader import StateReader

    StateReader.write_cache("calendar", {
        "source": "calendar",
        "items": [{
            "source": "calendar",
            "icon": "📅",
            "item_id": "",
            "title": "Model Free 2.0 sync",
            "url": "https://outlook.office.com/calendar/item/1",
            "group": "2026-05-11",
            "extras": {
                "start": "2026-05-11T09:00:00",
                "end": "2026-05-11T09:30:00",
            },
        }],
        "error": None,
        "fetched_at": int(time.time() * 1000),
        "seconds": 3,
    })

    r = client.get("/", headers=_auth(with_token))

    assert r.status_code == 200
    assert "Model Free 2.0 sync" in r.text
    assert "09:00" in r.text
    assert "09:30" in r.text
    assert "Mon 2026-05-11 09:00-09:30" in r.text
    assert "Meetings" in r.text
    assert "Last updated" in r.text

    api = client.get("/api/source/calendar", headers=_auth(with_token))
    assert api.status_code == 200
    item = api.json()["items"][0]
    assert item["meeting_time"] == "Mon 2026-05-11 09:00-09:30"
    assert item["meeting_time_display"] == "Mon 2026-05-11 09:00-09:30"
    assert item["meeting_start_time"] == "09:00"
    assert item["meeting_end_time"] == "09:30"


def test_source_calendar_shows_meeting_time(client: TestClient, temp_state_dir: Path,
                                            with_token: str) -> None:
    from agent_me.dashboard.state_reader import StateReader

    StateReader.write_cache("calendar", {
        "source": "calendar",
        "items": [{
            "source": "calendar",
            "icon": "📅",
            "item_id": "",
            "title": "Model Free 2.0 sync",
            "url": "https://outlook.office.com/calendar/item/1",
            "group": "2026-05-11",
            "extras": {
                "start": "2026-05-11T09:00:00",
                "end": "2026-05-11T09:30:00",
            },
        }],
        "error": None,
        "fetched_at": int(time.time() * 1000),
        "seconds": 3,
    })

    r = client.get("/source/calendar", headers=_auth(with_token))

    assert r.status_code == 200
    assert "Model Free 2.0 sync" in r.text
    assert "Mon 2026-05-11 09:00-09:30" in r.text
    assert "meeting_time_display" in r.text


def test_source_page_known_source_renders(client: TestClient, with_token: str) -> None:
    r = client.get("/source/jira", headers=_auth(with_token))
    assert r.status_code == 200
    assert "Jira" in r.text


def test_static_avatar_asset_served(client: TestClient) -> None:
    r = client.get("/static/agent-me-avatar.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers.get("content-type", "")
    assert "agent-me avatar" in r.text


def test_static_css_includes_auto_sfa_light_theme(client: TestClient) -> None:
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert ".auto-sfa-terminal-body" in r.text
    assert "html.theme-light .auto-sfa-terminal-body" in r.text
    assert "html.theme-light .auto-sfa-cancel-button" in r.text
    assert "html.theme-light input::placeholder" in r.text
    assert "color: #94a3b8" in r.text


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


def test_auto_sfa_page_renders(
    client: TestClient, with_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_me.dashboard import app as app_module

    monkeypatch.setattr(app_module, "_auto_sfa_default_source_folder_id", lambda: "50722")

    r = client.get("/auto-sfa", headers=_auth(with_token))
    assert r.status_code == 200
    assert "Auto SFA" in r.text
    assert "Prepare templates, release SFA tasks, and stream terminal output here." in r.text
    assert "<span>MCP Setup</span>" in r.text
    assert 'href="https://agent-me.nvidia.com/mcp/setup"' in r.text
    assert "Copy MCP endpoint" not in r.text
    assert "mcpEndpointMenu" not in r.text
    assert "copiedMcp" not in r.text
    assert "Create SFA Tasks" in r.text
    assert "Release SFA Tasks" in r.text
    assert "display_name" in r.text
    assert "Display Name" in r.text
    assert "placeholder=\"Thanh Phan\"" in r.text
    assert "DevTest Automation Dev Linux Display Name." not in r.text
    assert "DevTest credentials" in r.text
    assert "Enter DevTest username and password" in r.text
    assert "Use default host credentials" in r.text
    assert "Required by default. Check above to use host credentials instead." in r.text
    assert "specific task IDs" in r.text
    assert "specific template IDs" in r.text
    assert "Win_Linux" in r.text
    assert "appearance-none" in r.text
    assert "right-3" in r.text
    assert "Linux Only" in r.text
    assert "Windows Only" in r.text
    assert "Both" in r.text
    assert "folder_id" in r.text
    assert "project_id is fixed to" not in r.text
    assert "project 1072" not in r.text
    assert "source_folder_id" in r.text
    assert "Linux Release" in r.text
    assert "source: \"50722\"" in r.text
    assert "source: \"47877\"" in r.text
    assert "`source ${option.source}`" not in r.text
    assert "Default for" in r.text
    assert "releaseDestinationPath" in r.text
    assert "auto by today; editable." in r.text
    assert "Current cycle folder; editable after auto-resolve." not in r.text
    assert "/api/auto-sfa/resolve-destination" in r.text
    assert "display_name: String(this.form.display_name" in r.text
    assert "async fetchJson" in r.text
    assert "scheduleDestinationResolve" not in r.text
    assert "auto_resolve_destination" not in r.text
    assert "use_default_source_folder" not in r.text
    assert "destination_folder_id" in r.text
    assert r.text.count('type="text" inputmode="numeric" pattern="[0-9]*"') >= 2
    assert "Merge Request / MR link" not in r.text
    assert "required templates already exist in SFA" in r.text
    assert "dashboard-date-input" in r.text
    assert "dashboard-date-field" in r.text
    assert r.text.count("dashboard-date-button") == 2
    assert "openDatePicker" in r.text
    assert "defaultReleaseStartDate" in r.text
    assert "defaultReleaseEndDate" in r.text
    assert "agent-me terminal" not in r.text
    assert "Cancel operation" in r.text
    assert "Open new terminal" in r.text
    assert "auto-sfa-terminal-action" in r.text
    assert "clearTerminal" not in r.text
    assert ">clear<" not in r.text
    assert "Auto SFA realtime terminal output" in r.text
    assert "auto-sfa-terminal-body" in r.text
    assert "streaming stdout" in r.text
    assert "terminal-cursor" in r.text
    assert "terminalLineClass" in r.text
    assert "Recent Auto SFA jobs" not in r.text
    assert "Create SFA Tasks trigger history" in r.text
    assert "Release SFA Tasks trigger history" in r.text
    assert "filteredHistory" in r.text
    assert "/api/auto-sfa/history" in r.text


def test_auto_sfa_page_can_resume_mcp_job(
    client: TestClient, with_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_me.dashboard import app as app_module

    class Job:
        def public_dict(self):
            return {
                "job_id": "mcp-create-1",
                "status": "running",
                "line_count": 2,
                "request": {
                    "flow_type": "create",
                    "display_name": "Thanh Phan",
                    "folder_id": 494139,
                },
            }

    monkeypatch.setattr(app_module.AUTO_SFA_RUNNER, "get_job", lambda job_id: None)
    monkeypatch.setattr(
        app_module.MCP_AUTO_SFA_RUNNER,
        "get_job",
        lambda job_id: Job() if job_id == "mcp-create-1" else None,
    )

    r = client.get("/auto-sfa?job_id=mcp-create-1", headers=_auth(with_token))

    assert r.status_code == 200
    assert '"job_id": "mcp-create-1"' in r.text
    assert '"display_name": "Thanh Phan"' in r.text


def test_auto_sfa_mcp_endpoint_uses_request_origin(
    client: TestClient, with_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_me.dashboard import app as app_module

    monkeypatch.delenv("AUTO_SFA_MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(app_module, "_auto_sfa_default_source_folder_id", lambda: "50722")

    r = client.get("/auto-sfa", headers={**_auth(with_token), "Host": "agent-me.nvidia.com"})

    assert r.status_code == 200
    assert 'href="http://agent-me.nvidia.com/mcp/setup"' in r.text
    assert "Copy MCP endpoint" not in r.text


def test_auto_sfa_mcp_endpoint_respects_forwarded_proto(
    client: TestClient, with_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_me.dashboard import app as app_module

    monkeypatch.delenv("AUTO_SFA_MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(app_module, "_auto_sfa_default_source_folder_id", lambda: "50722")

    r = client.get("/auto-sfa", headers={
        **_auth(with_token),
        "Host": "agent-me.nvidia.com",
        "X-Forwarded-Proto": "https",
    })

    assert r.status_code == 200
    assert 'href="https://agent-me.nvidia.com/mcp/setup"' in r.text
    assert "Copy MCP endpoint" not in r.text


def test_mcp_setup_page_renders_without_dashboard_auth(with_token: str) -> None:
    from agent_me.dashboard.app import app

    r = TestClient(app).get("/mcp/setup")

    assert r.status_code == 200
    assert "Connect Auto SFA tools" in r.text
    assert "Back to Auto SFA" in r.text
    assert "Generate MCP Token" in r.text
    assert "Generating..." in r.text
    assert "DevTest username" in r.text
    assert "Token label" not in r.text
    assert "Token labels are only display names" not in r.text


def test_mcp_setup_creates_long_lived_token(
    client: TestClient,
    with_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_me.dashboard import app as app_module

    async def fake_resolve(*args, **kwargs):
        return 1155188

    monkeypatch.setattr(app_module, "resolve_destination_folder_id", fake_resolve)

    r = client.post("/mcp/setup", data={
        "username": "Thanh.Phan@nvidia.com",
        "password": "devtest-password",
    })

    assert r.status_code == 200
    assert "Token created for" in r.text
    assert "thanh.phan" in r.text
    assert "agm_" in r.text
    assert "Bearer token" in r.text
    assert "Copy bearer token" in r.text
    assert "mcp-copy-check hidden" in r.text
    assert 'button.dataset.copyState = "Copied"' in r.text
    assert "Create another token" not in r.text
    assert "Token label" not in r.text
    assert "curl -fsSL https://agent-me.nvidia.com/mcp/install" in r.text
    assert "AGENT_ME_MCP_TOKEN=" in r.text
    assert "Bearer agm_" in r.text
    assert "claude mcp add --transport http" in r.text
    assert "agent-me https://agent-me.nvidia.com/mcp/ --header" in r.text
    assert "--header &#34;Authorization: Bearer agm_" in r.text
    assert "[mcp_servers.agent-me]" in r.text
    assert client.cookies.get("agent_me_auto_sfa_mcp_setup")


def test_mcp_setup_remembers_token_in_same_browser(
    client: TestClient,
    with_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_me.dashboard import app as app_module

    async def fake_resolve(*args, **kwargs):
        return 1155188

    monkeypatch.setattr(app_module, "resolve_destination_folder_id", fake_resolve)

    created = client.post("/mcp/setup", data={
        "username": "thaphan",
        "password": "devtest-password",
    })
    match = re.search(r"agm_[A-Za-z0-9_-]+", created.text)
    assert match is not None
    token = match.group(0)

    remembered = client.get("/mcp/setup")

    assert remembered.status_code == 200
    assert "Token ready for" in remembered.text
    assert token in remembered.text
    assert "Generate MCP Token" not in remembered.text
    assert "Create another token" not in remembered.text


def test_mcp_install_script_renders_for_request_origin(client: TestClient, with_token: str) -> None:
    r = client.get("/mcp/install", headers={"Host": "agent-me.nvidia.com"})

    assert r.status_code == 200
    assert "text/x-shellscript" in r.headers.get("content-type", "")
    assert "AGENT_ME_MCP_TOKEN is required" in r.text
    assert "agent-me" in r.text
    assert "endpoint=http://agent-me.nvidia.com/mcp/" in r.text


def test_auto_sfa_history_endpoint_returns_persisted_runs(
    client: TestClient, temp_state_dir: Path, with_token: str
) -> None:
    from agent_me.auto_sfa_history import record_auto_sfa_run
    from agent_me.dashboard import state_reader

    now = int(time.time() * 1000)
    record_auto_sfa_run(
        state_reader.DB_PATH,
        run_id="run-123",
        triggered_at_ms=now,
        display_name="Thanh Phan",
        status="done",
        flow_type="release",
    )
    record_auto_sfa_run(
        state_reader.DB_PATH,
        run_id="run-456",
        triggered_at_ms=now + 1,
        display_name="Hung Hoang",
        status="error",
        flow_type="create",
    )

    r = client.get("/api/auto-sfa/history", headers=_auth(with_token))
    assert r.status_code == 200
    body = r.json()
    assert body["runs"][0]["run_id"] == "run-456"
    assert body["runs"][0]["flow_type"] == "create"
    assert body["runs"][1]["run_id"] == "run-123"
    assert body["runs"][1]["flow_type"] == "release"
    assert body["runs_by_flow"]["create"][0]["run_id"] == "run-456"
    assert body["runs_by_flow"]["release"][0]["run_id"] == "run-123"
    assert body["runs_by_flow"]["release"][0]["display_name"] == "Thanh Phan"
    assert body["runs_by_flow"]["release"][0]["status"] == "done"

    page = client.get("/auto-sfa", headers=_auth(with_token))
    assert page.status_code == 200
    assert "run-123" in page.text
    assert "run-456" in page.text
    assert "Thanh Phan" in page.text
    assert "Hung Hoang" in page.text


@pytest.mark.asyncio
async def test_auto_sfa_runner_persists_trigger_history(
    temp_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_me.auto_sfa import build_auto_sfa_request
    from agent_me.dashboard import auto_sfa_runner as runner_module

    async def fake_run_auto_sfa(request, progress_cb=None):
        if progress_cb:
            await progress_cb({"event": "line", "line_no": 1, "line": "ok"})

    monkeypatch.setattr(runner_module, "run_auto_sfa", fake_run_auto_sfa)

    request = build_auto_sfa_request({
        "display_name": "Thanh Phan",
        "source_folder_id": "50722",
        "devtest_folder_id": "1155188",
        "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
        "start_date": "2026-04-16",
        "finish_date": "2026-04-27",
    })
    runner = runner_module.AutoSFARunner()
    job = await runner.start(request)

    for _ in range(50):
        if runner.get_job(job.job_id).status == "done":
            break
        await asyncio.sleep(0.01)

    rows = runner.recent_history(limit=5)
    assert rows[0]["run_id"] == job.job_id
    assert rows[0]["flow_type"] == "release"
    assert rows[0]["display_name"] == "Thanh Phan"
    assert rows[0]["status"] == "done"


@pytest.mark.asyncio
async def test_auto_sfa_runner_runs_create_flow(
    temp_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_me.auto_sfa import build_update_template_request
    from agent_me.dashboard import auto_sfa_runner as runner_module

    calls = []

    async def fake_run_update_template(request, progress_cb=None):
        calls.append(request)
        if progress_cb:
            await progress_cb({"event": "line", "line_no": 1, "line": "ok"})

    monkeypatch.setattr(runner_module, "run_update_template", fake_run_update_template)

    request = build_update_template_request({
        "display_name": "Thanh Phan",
        "folder_id": "494139",
    })
    runner = runner_module.AutoSFARunner()
    job = await runner.start(request)

    for _ in range(50):
        if runner.get_job(job.job_id).status == "done":
            break
        await asyncio.sleep(0.01)

    assert calls == [request]
    assert runner.get_job(job.job_id).status == "done"
    rows = runner.recent_history(limit=5, flow_type="create")
    assert rows[0]["run_id"] == job.job_id
    assert rows[0]["flow_type"] == "create"


@pytest.mark.asyncio
async def test_auto_sfa_runner_allows_concurrent_jobs(
    temp_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_me.auto_sfa import build_update_template_request
    from agent_me.dashboard import auto_sfa_runner as runner_module

    started: list[str] = []
    release = asyncio.Event()

    async def fake_run_update_template(request, progress_cb=None):
        started.append(request.display_name)
        if len(started) == 2:
            release.set()
        await release.wait()

    monkeypatch.setattr(runner_module, "run_update_template", fake_run_update_template)

    runner = runner_module.AutoSFARunner()
    first = await runner.start(build_update_template_request({
        "display_name": "Thanh Phan",
        "folder_id": "494139",
    }))
    second = await runner.start(build_update_template_request({
        "display_name": "Hung Hoang",
        "folder_id": "494140",
    }))

    for _ in range(50):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)

    assert sorted(started) == ["Hung Hoang", "Thanh Phan"]
    assert runner.get_job(first.job_id).status in {"running", "done"}
    assert runner.get_job(second.job_id).status in {"running", "done"}


@pytest.mark.asyncio
async def test_auto_sfa_runner_cancel_marks_job_cancelled(
    temp_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_me.auto_sfa import build_update_template_request
    from agent_me.dashboard import auto_sfa_runner as runner_module

    started = asyncio.Event()

    async def fake_run_update_template(request, progress_cb=None):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(runner_module, "run_update_template", fake_run_update_template)

    runner = runner_module.AutoSFARunner()
    job = await runner.start(build_update_template_request({
        "display_name": "Thanh Phan",
        "folder_id": "494139",
    }))
    await asyncio.wait_for(started.wait(), timeout=1)

    cancelled = await runner.cancel(job.job_id)

    assert cancelled is not None
    for _ in range(50):
        if runner.get_job(job.job_id).status == "cancelled":
            break
        await asyncio.sleep(0.01)
    assert runner.get_job(job.job_id).status == "cancelled"


def test_api_state_returns_all_snapshots(client: TestClient, with_token: str) -> None:
    r = client.get("/api/state", headers=_auth(with_token))
    assert r.status_code == 200
    body = r.json()
    assert "uptime_s" in body
    assert len(body["snapshots"]) == 8
    sources = [s["source"] for s in body["snapshots"]]
    assert sources == [
        "nvbugs", "gitlab", "github", "calendar",
        "outlook", "jira", "teams", "slack",
    ]


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
    assert len(body["jobs"]) == 8
    assert [j["source"] for j in body["jobs"]] == [
        "nvbugs", "gitlab", "github", "calendar",
        "outlook", "jira", "teams", "slack",
    ]
    assert started == [
        "nvbugs", "gitlab", "github", "calendar",
        "outlook", "jira", "teams", "slack",
    ]
    assert r.headers.get("x-agent-me-operator-token")


def test_refresh_one_requires_passcode_and_accepts_operator_token(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    with_token: str,
) -> None:
    import uuid

    from agent_me.dashboard import app as app_module
    from agent_me.dashboard.brief_runner import BriefJob

    started: list[str] = []

    async def fake_start(self, source: str, period_days: int = 1):
        started.append(f"{source}:{period_days}")
        return BriefJob(
            job_id=uuid.uuid4().hex[:8],
            source=source,
            started_at=int(time.time() * 1000),
            status="pending",
        )

    monkeypatch.setattr(app_module.RUNNER.__class__, "start", fake_start)

    denied = client.post("/api/refresh/calendar", headers=_auth(with_token))
    assert denied.status_code == 403

    first = client.post(
        "/api/refresh/calendar?period=week",
        headers=_operator_auth(with_token),
    )
    assert first.status_code == 202
    token = first.headers.get("x-agent-me-operator-token")
    assert token

    second = client.post(
        "/api/refresh/jira?period=day",
        headers={
            **_auth(with_token),
            "X-Agent-Me-Operator-Token": token,
        },
    )
    assert second.status_code == 202
    assert started == ["calendar:7", "jira:1"]


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
            "source_folder_id": "50722",
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
    assert captured["request"].source_folder_id == 50722
    assert captured["request"].devtest_folder_id == 1155188
    assert captured["request"].task_ids == "824423,824424"
    assert captured["request"].auth_username == "thaphan"
    assert captured["request"].auth_password == "dummy-password"
    assert "dummy-password" not in json.dumps(body)


def test_api_auto_sfa_run_starts_create_job(client: TestClient, monkeypatch,
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
            "flow_type": "create",
            "display_name": "Thanh Phan",
            "folder_id": "494139",
            "win_linux": "Windows Only",
            "template_ids_enabled": True,
            "template_ids": "5996784,5996785",
            "use_personal_credentials": True,
            "auth_username": "thaphan",
            "auth_password": "dummy-password",
        },
        headers=_auth(with_token),
    )

    assert r.status_code == 202
    body = r.json()
    assert body["request"]["flow_type"] == "create"
    assert body["request"]["win_linux"] == "Windows Only"
    assert captured["request"].display_name == "Thanh Phan"
    assert captured["request"].template_project_id == 1072
    assert captured["request"].folder_id == 494139
    assert captured["request"].template_ids == "5996784,5996785"
    assert captured["request"].win_linux == "Windows Only"
    assert captured["request"].auth_username == "thaphan"
    assert captured["request"].auth_password == "dummy-password"
    assert "dummy-password" not in json.dumps(body)


def test_api_auto_sfa_resolve_destination(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, with_token: str
) -> None:
    from agent_me.dashboard import app as app_module

    captured: dict[str, object] = {}

    async def fake_resolve(source_folder_id, **kwargs):
        captured["source_folder_id"] = source_folder_id
        captured["kwargs"] = kwargs
        return 1155189

    monkeypatch.setattr(app_module, "resolve_destination_folder_id", fake_resolve)

    r = client.post(
        "/api/auto-sfa/resolve-destination",
        json={
            "source_folder_id": "50722",
            "use_personal_credentials": True,
            "auth_username": "thaphan@nvidia.com",
            "auth_password": "dummy-password",
        },
        headers=_auth(with_token),
    )

    assert r.status_code == 200
    assert r.json() == {"source_folder_id": 50722, "devtest_folder_id": 1155189}
    assert captured["source_folder_id"] == 50722
    assert captured["kwargs"]["auth_username"] == "thaphan"
    assert captured["kwargs"]["auth_password"] == "dummy-password"


def test_api_auto_sfa_run_infers_create_job_from_folder_id(
    client: TestClient, monkeypatch, with_token: str
) -> None:
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
            "folder_id": "494139",
            "source_folder_id": "50722",
            "devtest_folder_id": "",
            "url_path": "",
            "start_date": "",
            "finish_date": "",
        },
        headers=_auth(with_token),
    )

    assert r.status_code == 202
    body = r.json()
    assert body["request"]["flow_type"] == "create"
    assert captured["request"].display_name == "Thanh Phan"
    assert captured["request"].folder_id == 494139


def test_api_auto_sfa_cancel_job(client: TestClient, monkeypatch,
                                 with_token: str) -> None:
    from agent_me.dashboard import app as app_module

    calls = []

    async def fake_cancel(job_id):
        calls.append(job_id)

        class Job:
            def public_dict(self):
                return {"job_id": job_id, "status": "cancelled"}

        return Job()

    monkeypatch.setattr(app_module.AUTO_SFA_RUNNER, "cancel", fake_cancel)

    r = client.post("/api/auto-sfa/job-123/cancel", headers=_auth(with_token))

    assert r.status_code == 202
    assert r.json() == {"job_id": "job-123", "status": "cancelled"}
    assert calls == ["job-123"]


def test_operator_action_endpoints_require_passcode(client: TestClient,
                                                    with_token: str) -> None:
    r = client.post("/api/refresh/calendar", headers=_auth(with_token))
    assert r.status_code == 403
    assert r.json()["error"] == "operator passcode required"

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
            "source_folder_id": "",
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
