from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient


def _mcp_headers(username: str = "thaphan", password: str = "dummy-password") -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Basic {token}",
    }


def _bearer_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _jsonrpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }


def _call_tool(
    client: TestClient,
    name: str,
    arguments: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    request_id: int = 1,
) -> dict[str, Any]:
    response = client.post(
        "/mcp/",
        headers=headers or _mcp_headers(),
        json=_jsonrpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            request_id=request_id,
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    return body["result"]["structuredContent"]


def _call_tool_body(
    client: TestClient,
    name: str,
    arguments: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    request_id: int = 1,
) -> dict[str, Any]:
    response = client.post(
        "/mcp/",
        headers=headers or _mcp_headers(),
        json=_jsonrpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            request_id=request_id,
        ),
    )
    assert response.status_code == 200
    return response.json()


def test_auto_sfa_mcp_requires_agent_me_token(with_token: str) -> None:
    from agent_me.dashboard.app import build_app

    with TestClient(build_app()) as client:
        response = client.post(
            "/mcp/",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json=_jsonrpc("tools/list"),
        )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer ")
    assert response.json()["error"] == "Agent Me MCP token is required"


def test_auto_sfa_mcp_lists_expected_tools(with_token: str) -> None:
    from agent_me.dashboard.app import build_app

    with TestClient(build_app()) as client:
        response = client.post(
            "/mcp/",
            headers=_mcp_headers(),
            json=_jsonrpc("tools/list"),
        )

    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert {"create_sfa_tasks", "release_sfa_tasks"} <= names
    assert "get_sfa_job_status" not in names
    create = next(tool for tool in tools if tool["name"] == "create_sfa_tasks")
    release = next(tool for tool in tools if tool["name"] == "release_sfa_tasks")
    assert create["annotations"]["destructiveHint"] is True
    assert release["annotations"]["destructiveHint"] is True
    assert "auto template" in release["description"].lower()
    assert "confirmed=true" in create["description"]
    assert "confirmed=true" in release["description"]
    assert "job_url" in create["description"]
    assert "job_url" in release["description"]
    assert "confirmed" in create["inputSchema"]["properties"]
    assert "confirmed" in release["inputSchema"]["properties"]
    assert "confirmation_token" in create["inputSchema"]["properties"]
    assert "confirmation_token" in release["inputSchema"]["properties"]
    assert "prompt" not in create["inputSchema"]["properties"]
    assert "prompt" not in release["inputSchema"]["properties"]


def test_auto_sfa_mcp_rejects_prompt_argument(with_token: str) -> None:
    from agent_me.dashboard.app import build_app

    with TestClient(build_app()) as client:
        body = _call_tool_body(
            client,
            "create_sfa_tasks",
            {
                "prompt": 'Create SFA Tasks for "Thanh Phan" in folder "494139"',
            },
        )

    assert body["error"]["code"] == -32602
    assert body["error"]["data"]["tool_name"] == "create_sfa_tasks"
    assert body["error"]["data"]["unknown_arguments"] == ["prompt"]
    assert "prompt" not in body["error"]["data"]["allowed_arguments"]


def test_create_sfa_tasks_preview_uses_basic_auth_credentials(with_token: str) -> None:
    from agent_me.dashboard.app import build_app

    with TestClient(build_app()) as client:
        result = _call_tool(
            client,
            "create_sfa_tasks",
            {
                "display_name": "Thanh Phan",
                "folder_id": "494139",
                "confirmed": False,
            },
        )

    assert result["status"] == "needs_confirmation"
    assert result["plan_mode_required"] is True
    assert result["confirmation_token"].startswith("v1.")
    assert result["summary"]["display_name"] == "Thanh Phan"
    assert result["summary"]["folder_id"] == 494139
    assert result["summary"]["devtest_username"] == "thaphan"
    assert "Default: Win_Linux = Linux Only." in result["confirmation_options"]
    assert result["resolved_fields"]["auth_password_set"] is True
    assert "auth_password" not in result["resolved_fields"]


def test_create_sfa_tasks_preview_uses_bearer_token_credentials(
    temp_state_dir,
    with_token: str,
) -> None:
    from agent_me.auto_sfa_mcp_store import create_mcp_token
    from agent_me.dashboard.app import build_app

    created = create_mcp_token(
        username="tnvidia",
        password="stored-password",
        label="pytest",
    )

    with TestClient(build_app()) as client:
        result = _call_tool(
            client,
            "create_sfa_tasks",
            {
                "display_name": "Thanh Phan",
                "folder_id": "494139",
                "confirmed": False,
            },
            headers=_bearer_headers(created.token),
        )

    assert result["status"] == "needs_confirmation"
    assert result["summary"]["devtest_username"] == "tnvidia"
    assert result["resolved_fields"]["auth_password_set"] is True
    assert "auth_password" not in result["resolved_fields"]


def test_release_sfa_tasks_general_request_requires_plan_mode(with_token: str) -> None:
    from agent_me.dashboard.app import build_app

    with TestClient(build_app()) as client:
        result = _call_tool(
            client,
            "release_sfa_tasks",
            {},
        )

    assert result["status"] == "needs_input"
    assert result["plan_mode_required"] is True
    assert result["missing_fields"] == ["display_name", "url_path"]


def test_release_sfa_tasks_preview_explains_defaults(with_token: str) -> None:
    from agent_me.dashboard.app import build_app

    with TestClient(build_app()) as client:
        result = _call_tool(
            client,
            "release_sfa_tasks",
            {
                "display_name": "Thanh Phan",
                "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123",
                "confirmed": False,
            },
        )

    assert result["status"] == "needs_confirmation"
    assert result["plan_mode_required"] is True
    assert result["confirmation_token"].startswith("v1.")
    assert result["summary"]["release_type"] == "Linux Release"
    assert result["summary"]["release_type_explicit"] is False
    assert any("Linux Release" in option for option in result["confirmation_options"])
    assert any("Release" in option for option in result["confirmation_options"])


def test_create_sfa_tasks_confirmed_starts_background_job(
    with_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_me import auto_sfa_mcp
    from agent_me.dashboard.app import build_app

    captured: dict[str, Any] = {}

    async def fake_start(request):
        captured["request"] = request
        return SimpleNamespace(
            public_dict=lambda: {
                "job_id": "mcp-create-1",
                "status": "pending",
                "line_count": 0,
                "request": request.as_input_dict(),
            }
        )

    monkeypatch.setattr(auto_sfa_mcp.MCP_AUTO_SFA_RUNNER, "start", fake_start)

    with TestClient(build_app()) as client:
        result = _call_tool(
            client,
            "create_sfa_tasks",
            {
                "display_name": "Thanh Phan",
                "folder_id": "494139",
            },
        )

    assert result["status"] == "started"
    assert result["job_id"] == "mcp-create-1"
    assert "monitor_tool" not in result
    assert "monitor_arguments" not in result
    assert result["job_url"].endswith("/auto-sfa?job_id=mcp-create-1")
    assert "Open job_url" in result["message"]
    request = captured["request"]
    assert request.display_name == "Thanh Phan"
    assert request.folder_id == 494139
    assert request.auth_username == "thaphan"
    assert request.auth_password == "dummy-password"


def test_auto_sfa_mcp_job_url_uses_request_origin(
    with_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_me import auto_sfa_mcp
    from agent_me.dashboard.app import build_app

    async def fake_start(request):
        return SimpleNamespace(
            public_dict=lambda: {
                "job_id": "mcp-create-1",
                "status": "pending",
                "line_count": 0,
                "request": request.as_input_dict(),
            }
        )

    monkeypatch.delenv("AUTO_SFA_MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(auto_sfa_mcp.MCP_AUTO_SFA_RUNNER, "start", fake_start)

    with TestClient(build_app()) as client:
        result = _call_tool(
            client,
            "create_sfa_tasks",
            {
                "display_name": "Thanh Phan",
                "folder_id": "494139",
            },
            headers={**_mcp_headers(), "Host": "agent-me.nvidia.com"},
        )

    assert result["dashboard_url"] == "http://agent-me.nvidia.com/auto-sfa"
    assert result["job_url"] == "http://agent-me.nvidia.com/auto-sfa?job_id=mcp-create-1"


def test_release_sfa_tasks_confirmed_starts_without_agent_translation(
    with_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_me import auto_sfa_mcp
    from agent_me.dashboard.app import build_app

    captured: dict[str, Any] = {}

    async def fake_start(request):
        captured["request"] = request
        return SimpleNamespace(
            public_dict=lambda: {
                "job_id": "mcp-release-1",
                "status": "pending",
                "line_count": 0,
                "request": request.as_input_dict(),
            }
        )

    monkeypatch.setattr(auto_sfa_mcp.MCP_AUTO_SFA_RUNNER, "start", fake_start)

    with TestClient(build_app()) as client:
        args = {
            "display_name": "Thanh Phan",
            "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123",
            "source_folder_id": "50722",
            "devtest_folder_id": "1155188",
            "start_date": "2026-04-16",
            "finish_date": "2026-04-27",
        }
        result = _call_tool(client, "release_sfa_tasks", args)

    assert result["status"] == "started"
    assert result["job_id"] == "mcp-release-1"
    request = captured["request"]
    assert request.display_name == "Thanh Phan"
    assert request.devtest_folder_id == 1155188
    assert request.source_folder_id == 50722
    assert request.auth_username == "thaphan"
    assert request.auth_password == "dummy-password"


def test_release_sfa_tasks_release_type_keeps_explicit_destination(
    with_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_me import auto_sfa_mcp
    from agent_me.dashboard.app import build_app

    captured: dict[str, Any] = {}

    async def fake_start(request):
        captured["request"] = request
        return SimpleNamespace(
            public_dict=lambda: {
                "job_id": "mcp-release-2",
                "status": "pending",
                "line_count": 0,
                "request": request.as_input_dict(),
            }
        )

    async def fail_resolve(*args, **kwargs):
        raise AssertionError("explicit devtest_folder_id must skip destination resolve")

    monkeypatch.setattr(auto_sfa_mcp.MCP_AUTO_SFA_RUNNER, "start", fake_start)
    monkeypatch.setattr(auto_sfa_mcp, "resolve_destination_folder_id", fail_resolve)

    with TestClient(build_app()) as client:
        result = _call_tool(
            client,
            "release_sfa_tasks",
            {
                "display_name": "Thanh Phan",
                "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123",
                "release_type": "Linux Release",
                "devtest_folder_id": "1155188",
            },
        )

    assert result["status"] == "started"
    request = captured["request"]
    assert request.source_folder_id == 50722
    assert request.devtest_folder_id == 1155188


def test_release_sfa_tasks_structured_destination_skips_auto_resolve(
    with_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_me import auto_sfa_mcp
    from agent_me.dashboard.app import build_app

    captured: dict[str, Any] = {}

    async def fake_start(request):
        captured["request"] = request
        return SimpleNamespace(
            public_dict=lambda: {
                "job_id": "mcp-release-3",
                "status": "pending",
                "line_count": 0,
                "request": request.as_input_dict(),
            }
        )

    async def fail_resolve(*args, **kwargs):
        raise AssertionError("prompt folderID must skip destination resolve")

    monkeypatch.setattr(auto_sfa_mcp.MCP_AUTO_SFA_RUNNER, "start", fake_start)
    monkeypatch.setattr(auto_sfa_mcp, "resolve_destination_folder_id", fail_resolve)

    with TestClient(build_app()) as client:
        result = _call_tool(
            client,
            "release_sfa_tasks",
            {
                "display_name": "Thanh Phan",
                "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123",
                "release_type": "Linux Release",
                "devtest_folder_id": "1155188",
            },
        )

    assert result["status"] == "started"
    request = captured["request"]
    assert request.source_folder_id == 50722
    assert request.devtest_folder_id == 1155188
