"""Helpers for passing MaaS MCP bearer tokens into Codex subprocesses.

Codex CLI can attach a bearer token to streamable HTTP MCP servers via
`--bearer-token-env-var`, but it currently cannot run the MaaS OAuth login
flow itself. Claude Code already stores MaaS OAuth access tokens in
`~/.claude/.credentials.json`, so agent-me uses those tokens as the local
credential source while Codex remains the only chat/brief agent backend.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


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


def codex_mcp_token_env(path: Path | None = None) -> dict[str, str]:
    """Return env vars expected by Codex MCP configs.

    Secret values are returned to the caller for subprocess env injection only;
    do not log the returned dict.
    """
    cred_path = path or credentials_path()
    try:
        raw = json.loads(cred_path.read_text())
    except (OSError, json.JSONDecodeError):
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
