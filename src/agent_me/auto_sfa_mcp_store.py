"""Long-lived Auto SFA MCP token store.

The setup page verifies a user's DevTest credentials once, stores the
password encrypted on the server, and returns an Agent Me bearer token.
MCP clients then authenticate with that bearer token instead of keeping
DevTest passwords in local client config.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import shlex
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from agent_me.dashboard import state_reader

TOKEN_PREFIX = "agm_"
KEY_FILENAME = "auto-sfa-mcp.fernet"
DB_FILENAME = "auto-sfa-mcp.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_sfa_mcp_tokens (
    token_digest       TEXT PRIMARY KEY,
    username           TEXT NOT NULL,
    encrypted_password TEXT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    last_used_at       INTEGER,
    expires_at         INTEGER,
    revoked_at         INTEGER
);
CREATE INDEX IF NOT EXISTS auto_sfa_mcp_tokens_username_idx
    ON auto_sfa_mcp_tokens(username, created_at DESC);
"""


@dataclass(frozen=True)
class StoredMcpCredentials:
    username: str
    password: str
    token_digest: str
    label: str = ""


@dataclass(frozen=True)
class CreatedMcpToken:
    token: str
    username: str
    label: str
    created_at: int
    expires_at: int | None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _state_dir() -> Path:
    path = state_reader.STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_db_path() -> Path:
    raw = os.environ.get("AUTO_SFA_MCP_TOKEN_DB_PATH")
    if raw:
        return Path(raw).expanduser()
    return _state_dir() / DB_FILENAME


def key_path() -> Path:
    raw = os.environ.get("AUTO_SFA_MCP_CREDENTIAL_KEY_FILE")
    if raw:
        return Path(raw).expanduser()
    return _state_dir() / KEY_FILENAME


def _file_mode_private(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _fernet_key() -> bytes:
    configured = os.environ.get("AUTO_SFA_MCP_CREDENTIAL_KEY", "").strip()
    if configured:
        return configured.encode()

    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = path.read_text(encoding="utf-8").strip().encode()
        _file_mode_private(path)
        return key

    key = Fernet.generate_key()
    path.write_text(key.decode(), encoding="utf-8")
    _file_mode_private(path)
    return key


def _fernet() -> Fernet:
    return Fernet(_fernet_key())


def _digest_secret() -> bytes:
    return hashlib.sha256(_fernet_key()).digest()


def _connect() -> sqlite3.Connection:
    db_path = token_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=1.5)
    conn.row_factory = sqlite3.Row
    with contextlib.suppress(sqlite3.Error):
        conn.execute("PRAGMA busy_timeout = 1500")
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _file_mode_private(db_path)
    return conn


def normalize_devtest_username(value: str) -> str:
    raw = value.strip()
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    return raw.strip().lower()


def _token_digest(token: str) -> str:
    return hmac.new(_digest_secret(), token.encode(), hashlib.sha256).hexdigest()


def token_ttl_ms() -> int | None:
    raw = os.environ.get("AUTO_SFA_MCP_TOKEN_TTL_DAYS", "").strip()
    if not raw:
        return None
    try:
        days = float(raw)
    except ValueError:
        return None
    if days <= 0:
        return None
    return int(days * 24 * 60 * 60 * 1000)


def create_mcp_token(
    *,
    username: str,
    password: str,
    label: str = "",
) -> CreatedMcpToken:
    normalized_username = normalize_devtest_username(username)
    if not normalized_username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")

    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    digest = _token_digest(token)
    now = _now_ms()
    ttl = token_ttl_ms()
    expires_at = now + ttl if ttl else None
    encrypted_password = _fernet().encrypt(password.encode()).decode()

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO auto_sfa_mcp_tokens
                (token_digest, username, encrypted_password, label,
                 created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                digest,
                normalized_username,
                encrypted_password,
                label.strip()[:120],
                now,
                now,
                expires_at,
            ),
        )
    finally:
        conn.close()

    return CreatedMcpToken(
        token=token,
        username=normalized_username,
        label=label.strip()[:120],
        created_at=now,
        expires_at=expires_at,
    )


def credentials_for_bearer_token(token: str) -> StoredMcpCredentials | None:
    token = token.strip()
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    digest = _token_digest(token)
    now = _now_ms()
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT token_digest, username, encrypted_password, label,
                   expires_at, revoked_at
            FROM auto_sfa_mcp_tokens
            WHERE token_digest = ?
            """,
            (digest,),
        ).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None:
            return None
        expires_at = row["expires_at"]
        if expires_at is not None and int(expires_at) <= now:
            return None
        try:
            password = _fernet().decrypt(str(row["encrypted_password"]).encode()).decode()
        except (InvalidToken, UnicodeDecodeError):
            return None
        conn.execute(
            "UPDATE auto_sfa_mcp_tokens SET last_used_at = ?, updated_at = ? WHERE token_digest = ?",
            (now, now, digest),
        )
        return StoredMcpCredentials(
            username=str(row["username"]),
            password=password,
            token_digest=digest,
            label=str(row["label"] or ""),
        )
    finally:
        conn.close()


def revoke_mcp_token(token: str) -> bool:
    digest = _token_digest(token.strip())
    now = _now_ms()
    conn = _connect()
    try:
        cur = conn.execute(
            """
            UPDATE auto_sfa_mcp_tokens
            SET revoked_at = ?, updated_at = ?
            WHERE token_digest = ? AND revoked_at IS NULL
            """,
            (now, now, digest),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


def bearer_header(token: str) -> str:
    return f"Bearer {token.strip()}"


def cursor_config_json(*, endpoint: str, token: str) -> str:
    return json.dumps(
        {
            "mcpServers": {
                "agent-me": {
                    "url": endpoint,
                    "headers": {
                        "Authorization": bearer_header(token),
                    },
                },
            },
        },
        indent=2,
    )


def install_script(*, endpoint: str) -> str:
    """Return a shell installer that reads AGENT_ME_MCP_TOKEN from env."""
    quoted_endpoint = shlex.quote(endpoint)
    return f"""#!/usr/bin/env bash
set -euo pipefail

if [ -z "${{AGENT_ME_MCP_TOKEN:-}}" ]; then
  echo "AGENT_ME_MCP_TOKEN is required" >&2
  exit 2
fi

endpoint={quoted_endpoint}
auth_header="Bearer $AGENT_ME_MCP_TOKEN"

python3 - "$endpoint" "$auth_header" <<'PY'
import json
import pathlib
import re
import sys

endpoint = sys.argv[1]
auth_header = sys.argv[2]

cursor_path = pathlib.Path.home() / ".cursor" / "mcp.json"
cursor_path.parent.mkdir(parents=True, exist_ok=True)
try:
    cursor_config = json.loads(cursor_path.read_text())
except Exception:
    cursor_config = {{}}
cursor_config.setdefault("mcpServers", {{}})["agent-me"] = {{
    "url": endpoint,
    "headers": {{"Authorization": auth_header}},
}}
cursor_path.write_text(json.dumps(cursor_config, indent=2) + "\\n")
print(f"Updated {{cursor_path}}")

codex_path = pathlib.Path.home() / ".codex" / "config.toml"
codex_path.parent.mkdir(parents=True, exist_ok=True)
text = codex_path.read_text() if codex_path.exists() else ""
section = (
    "[mcp_servers.agent-me]\\n"
    f'url = "{{endpoint}}"\\n'
    f'http_headers = {{{{ Authorization = "{{auth_header}}" }}}}\\n'
)
pattern = re.compile(r'(?ms)^\\[mcp_servers\\.agent-me\\]\\n.*?(?=^\\[|\\Z)')
if pattern.search(text):
    text = pattern.sub(section, text)
else:
    if text and not text.endswith("\\n"):
        text += "\\n"
    text += "\\n" + section
codex_path.write_text(text)
print(f"Updated {{codex_path}}")
PY

if command -v claude >/dev/null 2>&1; then
  claude mcp remove agent-me -s user >/dev/null 2>&1 || true
  claude mcp add --transport http --scope user --header "Authorization: $auth_header" agent-me "$endpoint"
  echo "Updated Claude Code user MCP config"
else
  echo "Claude Code CLI not found; skipped Claude config"
fi

echo "Agent Me MCP installed. Restart Cursor/Codex/Claude sessions if they were already open."
"""


def install_command(*, base_url: str, token: str) -> str:
    return (
        f"curl -fsSL {base_url.rstrip('/')}/mcp/install "
        f"| AGENT_ME_MCP_TOKEN='{token}' bash"
    )
