from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import agent_me.mcp_tokens as mcp_tokens
from agent_me.mcp_tokens import codex_mcp_token_env, refresh_codex_mcp_env_file


def _write_credentials(path: Path, oauth: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpOAuth": oauth}))


def test_codex_mcp_token_env_reads_maas_credentials(tmp_path: Path) -> None:
    creds = tmp_path / ".claude" / ".credentials.json"
    _write_credentials(
        creds,
        {
            "maas-jira|abc": {"serverName": "maas-jira", "accessToken": "jira-token"},
            "maas-gitlab|def": {"accessToken": "gitlab-token"},
            "other|ghi": {"serverName": "other", "accessToken": "ignored"},
            "maas-empty|jkl": {"serverName": "maas-empty"},
        },
    )

    assert codex_mcp_token_env(creds) == {
        "AGENT_ME_MCP_TOKEN_MAAS_GITLAB": "gitlab-token",
        "AGENT_ME_MCP_TOKEN_MAAS_JIRA": "jira-token",
    }


def test_codex_mcp_token_env_loads_persistent_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    creds = tmp_path / ".claude" / ".credentials.json"
    env_file = tmp_path / ".config" / "agent-me" / "codex-mcp-env.sh"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "\n".join(
            (
                "# generated",
                "export AGENT_ME_MCP_TOKEN_MAAS_JIRA='persisted jira'",
                "AGENT_ME_MCP_TOKEN_MAAS_GITLAB=persisted-gitlab",
                "export NOT_AGENT_ME=ignored",
            )
        )
        + "\n"
    )
    monkeypatch.setenv("AGENT_ME_CLAUDE_MCP_CREDENTIALS", str(creds))
    monkeypatch.setenv("AGENT_ME_CODEX_MCP_ENV_FILE", str(env_file))

    assert codex_mcp_token_env() == {
        "AGENT_ME_MCP_TOKEN_MAAS_GITLAB": "persisted-gitlab",
        "AGENT_ME_MCP_TOKEN_MAAS_JIRA": "persisted jira",
    }


def test_codex_mcp_token_env_prefers_credentials_over_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    creds = tmp_path / ".claude" / ".credentials.json"
    env_file = tmp_path / ".config" / "agent-me" / "codex-mcp-env.sh"
    _write_credentials(
        creds,
        {"maas-jira|abc": {"serverName": "maas-jira", "accessToken": "fresh"}},
    )
    env_file.parent.mkdir(parents=True)
    env_file.write_text("export AGENT_ME_MCP_TOKEN_MAAS_JIRA=stale\n")
    monkeypatch.setenv("AGENT_ME_CLAUDE_MCP_CREDENTIALS", str(creds))
    monkeypatch.setenv("AGENT_ME_CODEX_MCP_ENV_FILE", str(env_file))

    assert codex_mcp_token_env()["AGENT_ME_MCP_TOKEN_MAAS_JIRA"] == "fresh"


def test_refresh_codex_mcp_env_file_writes_private_exports(tmp_path: Path) -> None:
    creds = tmp_path / ".claude" / ".credentials.json"
    env_file = tmp_path / ".config" / "agent-me" / "codex-mcp-env.sh"
    _write_credentials(
        creds,
        {"maas-jira|abc": {"serverName": "maas-jira", "accessToken": "fresh token"}},
    )

    assert refresh_codex_mcp_env_file(creds, env_file) == 1
    text = env_file.read_text()
    assert "export AGENT_ME_MCP_TOKEN_MAAS_JIRA='fresh token'" in text
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_refresh_codex_mcp_env_file_rejects_missing_tokens(tmp_path: Path) -> None:
    creds = tmp_path / ".claude" / ".credentials.json"
    _write_credentials(creds, {"other|abc": {"serverName": "other", "accessToken": "x"}})

    with pytest.raises(RuntimeError, match="no maas-\\* MCP OAuth access tokens"):
        refresh_codex_mcp_env_file(creds, tmp_path / "env.sh")


def test_refresh_expired_mcp_tokens_updates_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    creds = tmp_path / ".claude" / ".credentials.json"
    _write_credentials(
        creds,
        {
            "maas-nvbugs|abc": {
                "serverName": "maas-nvbugs",
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "clientId": "client-1",
                "scope": "nvbugs:bugs:read",
                "expiresAt": 1,
                "discoveryState": {
                    "authorizationServerUrl": "https://auth.example/nvbugs",
                },
            }
        },
    )
    monkeypatch.setattr(mcp_tokens.time, "time", lambda: 1000.0)

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, url: str, timeout: float) -> FakeResponse:
            assert url == "https://auth.example/nvbugs/.well-known/oauth-authorization-server"
            assert timeout == 20.0
            return FakeResponse({"token_endpoint": "https://auth.example/token"})

        def post(self, url: str, data: dict, timeout: float) -> FakeResponse:
            assert url == "https://auth.example/token"
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "old-refresh"
            assert data["client_id"] == "client-1"
            assert data["scope"] == "nvbugs:bugs:read"
            assert timeout == 30.0
            return FakeResponse(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "scope": "nvbugs:bugs:read nvbugs:bugs:write",
                }
            )

    monkeypatch.setattr(mcp_tokens.httpx, "Client", FakeClient)

    assert mcp_tokens.refresh_expired_mcp_tokens(creds) == 1
    raw = json.loads(creds.read_text())
    entry = raw["mcpOAuth"]["maas-nvbugs|abc"]
    assert entry["accessToken"] == "new-access"
    assert entry["refreshToken"] == "new-refresh"
    assert entry["scope"] == "nvbugs:bugs:read nvbugs:bugs:write"
    assert entry["expiresAt"] == 4_600_000


def test_refresh_mcp_tokens_force_attempts_unexpired_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    creds = tmp_path / ".claude" / ".credentials.json"
    _write_credentials(
        creds,
        {
            "maas-jira|abc": {
                "serverName": "maas-jira",
                "accessToken": "old-jira",
                "refreshToken": "jira-refresh",
                "clientId": "client-1",
                "expiresAt": 9_999_999,
                "discoveryState": {
                    "authorizationServerUrl": "https://auth.example/jira",
                },
            },
            "maas-nvbugs|abc": {
                "serverName": "maas-nvbugs",
                "accessToken": "old-nvbugs",
                "refreshToken": "nvbugs-refresh",
                "clientId": "client-1",
                "expiresAt": 9_999_999,
                "discoveryState": {
                    "authorizationServerUrl": "https://auth.example/nvbugs",
                },
            },
        },
    )
    monkeypatch.setattr(mcp_tokens.time, "time", lambda: 1000.0)

    class FakeHTTPStatusError(mcp_tokens.httpx.HTTPStatusError):
        pass

    class FakeBadResponse:
        status_code = 400
        reason_phrase = "Bad Request"
        text = "Failed to refresh token from Jira"
        request = mcp_tokens.httpx.Request("POST", "https://auth.example/jira/token")

        def raise_for_status(self) -> None:
            raise FakeHTTPStatusError(
                "bad request",
                request=self.request,
                response=self,
            )

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, url: str, timeout: float) -> FakeResponse:
            endpoint = url.replace(
                "/.well-known/oauth-authorization-server",
                "/token",
            )
            return FakeResponse({"token_endpoint": endpoint})

        def post(self, url: str, data: dict, timeout: float):
            if data["refresh_token"] == "jira-refresh":
                return FakeBadResponse()
            return FakeResponse(
                {
                    "access_token": "new-nvbugs",
                    "refresh_token": "new-nvbugs-refresh",
                    "expires_in": 3600,
                }
            )

    monkeypatch.setattr(mcp_tokens.httpx, "Client", FakeClient)

    report = mcp_tokens.refresh_mcp_tokens(creds, force=True)

    assert report.attempted == ("maas-jira", "maas-nvbugs")
    assert report.refreshed == ("maas-nvbugs",)
    assert "maas-jira" in report.failed
    assert "HTTP 400" in report.failed["maas-jira"]
    raw = json.loads(creds.read_text())
    assert raw["mcpOAuth"]["maas-nvbugs|abc"]["accessToken"] == "new-nvbugs"
    assert raw["mcpOAuth"]["maas-jira|abc"]["accessToken"] == "old-jira"
