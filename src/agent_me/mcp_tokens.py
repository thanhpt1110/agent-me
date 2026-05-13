"""Helpers for passing MaaS MCP bearer tokens into Codex subprocesses.

Codex CLI can attach a bearer token to streamable HTTP MCP servers via
`--bearer-token-env-var`, but it currently cannot run the MaaS OAuth login
flow itself. Claude Code already stores MaaS OAuth access tokens in
`~/.claude/.credentials.json`, so agent-me uses those tokens as the local
credential source while Codex remains the only chat/brief agent backend.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

TOKEN_REFRESH_GRACE_MS = int(os.environ.get("AGENT_ME_MCP_TOKEN_REFRESH_GRACE_MS", 5 * 60 * 1000))
TOKEN_REFRESH_FAILURE_COOLDOWN_MS = int(
    os.environ.get("AGENT_ME_MCP_TOKEN_REFRESH_FAILURE_COOLDOWN_MS", 15 * 60 * 1000)
)
_REFRESH_LOCK = threading.Lock()
_REFRESH_FAILURES_MS: dict[str, int] = {}


@dataclass(frozen=True)
class McpTokenRefreshReport:
    attempted: tuple[str, ...] = ()
    refreshed: tuple[str, ...] = ()
    failed: dict[str, str] = field(default_factory=dict)
    skipped: tuple[str, ...] = ()


def token_env_var(server_name: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]", "_", server_name).upper()
    return f"AGENT_ME_MCP_TOKEN_{suffix}"


def credentials_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_ME_CLAUDE_MCP_CREDENTIALS",
            str(Path.home() / ".claude" / ".credentials.json"),
        )
    ).expanduser()


def codex_mcp_env_file_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_ME_CODEX_MCP_ENV_FILE",
            str(Path.home() / ".config" / "agent-me" / "codex-mcp-env.sh"),
        )
    ).expanduser()


def _credential_token_env(path: Path) -> dict[str, str]:
    raw = _read_credentials(path)
    if not raw:
        return {}

    oauth: dict[str, Any] = raw.get("mcpOAuth") or {}
    env: dict[str, str] = {}
    for key, value in oauth.items():
        if not isinstance(value, dict):
            continue
        access = value.get("accessToken")
        if not access:
            continue
        server = value.get("serverName") or str(key).split("|", 1)[0]
        if not server.startswith("maas-"):
            continue
        env[token_env_var(server)] = str(access)
    return env


def _read_credentials(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _write_credentials(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp_path, path)


def _token_endpoint(entry: dict[str, Any], client: httpx.Client) -> str | None:
    discovery = entry.get("discoveryState") or {}
    auth_server = discovery.get("authorizationServerUrl")
    if not isinstance(auth_server, str) or not auth_server:
        return None
    url = auth_server.rstrip("/") + "/.well-known/oauth-authorization-server"
    res = client.get(url, timeout=20.0)
    res.raise_for_status()
    metadata = res.json()
    endpoint = metadata.get("token_endpoint")
    return endpoint if isinstance(endpoint, str) and endpoint else None


def _refresh_entry(entry: dict[str, Any], client: httpx.Client, now_ms: int) -> bool:
    refresh_token = entry.get("refreshToken")
    client_id = entry.get("clientId")
    if not isinstance(refresh_token, str) or not isinstance(client_id, str):
        return False
    endpoint = _token_endpoint(entry, client)
    if not endpoint:
        return False

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if scope := entry.get("scope"):
        data["scope"] = str(scope)
    res = client.post(endpoint, data=data, timeout=30.0)
    res.raise_for_status()
    refreshed = res.json()
    access = refreshed.get("access_token")
    if not isinstance(access, str) or not access:
        return False

    entry["accessToken"] = access
    if new_refresh := refreshed.get("refresh_token"):
        entry["refreshToken"] = str(new_refresh)
    if scope := refreshed.get("scope"):
        entry["scope"] = str(scope)
    try:
        expires_in = int(refreshed.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        entry["expiresAt"] = now_ms + expires_in * 1000
    return True


def _refresh_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip().replace("\n", " ")[:240]
        return f"HTTP {exc.response.status_code}: {body or exc.response.reason_phrase}"
    return str(exc)[:240] or type(exc).__name__


def refresh_mcp_tokens(
    path: Path | None = None,
    *,
    force: bool = False,
    servers: set[str] | None = None,
) -> McpTokenRefreshReport:
    """Refresh MaaS OAuth access tokens in the Claude credential store."""
    cred_path = path or credentials_path()
    now_ms = int(time.time() * 1000)
    attempted: list[str] = []
    refreshed_servers: list[str] = []
    failed: dict[str, str] = {}
    skipped: list[str] = []
    with _REFRESH_LOCK:
        raw = _read_credentials(cred_path)
        oauth = raw.get("mcpOAuth") if raw else None
        if not isinstance(oauth, dict):
            return McpTokenRefreshReport()
        with httpx.Client() as client:
            for key, value in oauth.items():
                if not isinstance(value, dict):
                    continue
                server = value.get("serverName") or str(key).split("|", 1)[0]
                if not isinstance(server, str) or not server.startswith("maas-"):
                    continue
                if servers is not None and server not in servers:
                    continue
                failed_at = _REFRESH_FAILURES_MS.get(server)
                if (
                    not force
                    and failed_at is not None
                    and now_ms - failed_at < TOKEN_REFRESH_FAILURE_COOLDOWN_MS
                ):
                    skipped.append(server)
                    continue
                expires_at = value.get("expiresAt")
                try:
                    expires_ms = int(expires_at or 0)
                except (TypeError, ValueError):
                    expires_ms = 0
                if not force and expires_ms > now_ms + TOKEN_REFRESH_GRACE_MS:
                    skipped.append(server)
                    continue
                attempted.append(server)
                try:
                    refreshed = _refresh_entry(value, client, now_ms)
                except Exception as exc:
                    failed[server] = _refresh_error(exc)
                    refreshed = False
                if refreshed:
                    _REFRESH_FAILURES_MS.pop(server, None)
                    refreshed_servers.append(server)
                elif expires_ms <= now_ms + TOKEN_REFRESH_GRACE_MS:
                    failed.setdefault(server, "refresh endpoint returned no access token")
                    _REFRESH_FAILURES_MS[server] = now_ms
        if refreshed_servers:
            _write_credentials(cred_path, raw)
    return McpTokenRefreshReport(
        attempted=tuple(sorted(attempted)),
        refreshed=tuple(sorted(refreshed_servers)),
        failed=dict(sorted(failed.items())),
        skipped=tuple(sorted(skipped)),
    )


def refresh_expired_mcp_tokens(
    path: Path | None = None,
    *,
    force: bool = False,
    servers: set[str] | None = None,
) -> int:
    """Refresh expired MaaS OAuth access tokens in the Claude credential store."""
    return len(refresh_mcp_tokens(path, force=force, servers=servers).refreshed)


def _env_file_token_env(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}

    env: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not parts:
            continue
        assignments = parts[1:] if parts[0] == "export" else parts
        for assignment in assignments:
            if "=" not in assignment:
                continue
            name, value = assignment.split("=", 1)
            if not name.startswith("AGENT_ME_MCP_TOKEN_"):
                continue
            env[name] = value
    return env


def codex_mcp_token_env(path: Path | None = None) -> dict[str, str]:
    """Return env vars expected by Codex MCP configs.

    Secret values are returned to the caller for subprocess env injection only;
    do not log the returned dict.
    """
    cred_path = path or credentials_path()
    if path is None:
        with contextlib.suppress(Exception):
            refresh_expired_mcp_tokens(cred_path)
    env = _env_file_token_env(codex_mcp_env_file_path()) if path is None else {}
    env.update(_credential_token_env(cred_path))
    return env


def refresh_codex_mcp_env_file(
    credentials: Path | None = None,
    env_file: Path | None = None,
    *,
    refresh_tokens: bool = True,
    force_refresh: bool = False,
) -> int:
    """Persist MaaS access tokens as Codex bearer-token exports.

    The bridge can call this after the Mac-to-host sync updates
    ``~/.claude/.credentials.json``. It mirrors
    ``scripts/install-codex-mcp-env-on-host.sh`` without requiring a shell
    restart, then callers may merge ``codex_mcp_token_env()`` into their
    subprocess environment.
    """
    cred_path = credentials or credentials_path()
    target = env_file or codex_mcp_env_file_path()
    if refresh_tokens:
        refresh_mcp_tokens(cred_path, force=force_refresh)
    env = _credential_token_env(cred_path)
    if not env:
        raise RuntimeError(f"no maas-* MCP OAuth access tokens found in {cred_path}")

    lines = [
        "# Generated by agent_me.mcp_tokens.refresh_codex_mcp_env_file.",
        "# Source this before starting Codex so MaaS bearer-token MCPs can authenticate.",
        "# Contains short-lived access tokens; keep this file private.",
    ]
    for name in sorted(env):
        lines.append(f"export {name}={shlex.quote(env[name])}")

    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, stat.S_IRWXU)
    tmp_path = target.with_name(target.name + ".tmp")
    tmp_path.write_text("\n".join(lines) + "\n")
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp_path, target)
    return len(env)
