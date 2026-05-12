"""agent-me Slack bridge — Python port (replaces services/slack-bridge/).

Run with:
    uv run agent-me-bridge

What this owns:
- Loading .env from ${AGENT_ME_REPO_DIR}/configs/.env
- AsyncApp + Socket Mode connection to Slack
- SQLite state DB (threads, messages, pending_approvals — schema in db/)
- Event handlers: DM messages and channel @mentions
- Native slash commands: /mcp /version /whoami /help /reauth /brief /brev /model-free-draft
- Text-prefix slash commands (same set, intercepted from message body)
- Spawning headless `codex exec` per query with app/MCP read access
- Routing permissioned connector/MCP writes through Codex app-server auto-review
- Hybrid streaming UX (placeholder → final via chat.update)
- 6h periodic MCP-auth health probe + DM notification
- Graceful shutdown on SIGINT/SIGTERM

Phase 2b legacy:
- Claude Code PreToolUse approval hook support remains in the repo, but the
  default chat orchestrator is now Codex and does not use PA-via-Bash.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import sys
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from agent_me.codex_app_server import (
    codex_app_server_args,
    run_codex_app_server,
)
from agent_me.codex_app_server import (
    parse_app_server_final_message as _parse_app_server_final_message,
)
from agent_me.mcp_tokens import codex_mcp_token_env
from agent_me.slack_bridge import approvals

# ── Repo dir resolution ──────────────────────────────────────────────────

MCP_TOKEN_ENV_RE = re.compile(r"\bAGENT_ME_MCP_TOKEN_[A-Z0-9_]+\b")
CONNECTOR_COVERED_MAAS = {
    "maas-gdrive",   # Codex Google Drive connector has richer Drive/Docs/Sheets/Slides access.
    "maas-outlook",  # Codex Outlook Email/Calendar connectors cover the operator workflows.
    "maas-slack",    # Codex Slack connector is the primary Slack read/write path.
}

def resolve_repo_dir() -> Path:
    env = os.environ.get("AGENT_ME_REPO_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else Path.cwd() / p
    # Walk up from this file: src/agent_me/slack_bridge/app.py → repo root
    here = Path(__file__).resolve()
    candidate = here.parents[3]
    if (candidate / "CLAUDE.md").exists():
        return candidate
    return Path("/home/agent/agent-me")


REPO_DIR = resolve_repo_dir()
ENV_PATH = REPO_DIR / "configs" / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()  # fallback to shell exports
os.environ["AGENT_ME_REPO_DIR"] = str(REPO_DIR)


# ── uv binary resolution ────────────────────────────────────────────────
#
# systemd `--user` services start with a minimal PATH that typically
# excludes `~/.local/bin` (where the official `uv` installer drops the
# binary). The bridge's `ExecStart` works because the unit file uses the
# absolute path `%h/.local/bin/uv`, but when the bridge then spawns
# `subprocess.create_subprocess_exec("uv", ...)` for `/brief` and
# `/reauth`, asyncio's PATH lookup fails with `[Errno 2] No such file or
# directory: 'uv'`. Resolve to an absolute path once at import so every
# subprocess inherits a working invocation regardless of PATH.


def resolve_uv_bin() -> str:
    if env := os.environ.get("UV_BIN"):
        p = Path(env).expanduser()
        if p.exists():
            return str(p)
    local_bin = Path.home() / ".local" / "bin"
    aug_path = f"{local_bin}:/usr/local/bin:{os.environ.get('PATH', '')}"
    if found := shutil.which("uv", path=aug_path):
        return found
    return "uv"


def resolve_cli_bin(env_var: str, name: str) -> str:
    if env := os.environ.get(env_var):
        p = Path(env).expanduser()
        if p.exists():
            return str(p)
    local_bin = Path.home() / ".local" / "bin"
    aug_path = f"{local_bin}:/usr/local/bin:/opt/homebrew/bin:{os.environ.get('PATH', '')}"
    if found := shutil.which(name, path=aug_path):
        return found
    return name


_UV_BIN = resolve_uv_bin()

# ── Logger: structlog → stdlib logging → console + rotating file ────────

LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()


def _state_dir_early() -> Path:
    """State dir resolution duplicated here so log file path is available
    before the main resolve_state_dir() call below."""
    if d := os.environ.get("AGENT_ME_STATE_DIR"):
        return Path(d).expanduser()
    if x := os.environ.get("XDG_STATE_HOME"):
        return Path(x).expanduser() / "agent-me"
    return Path.home() / ".local" / "state" / "agent-me"


_LOG_DIR = _state_dir_early()
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "bridge.log"

_shared_processors: list = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=False),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]

structlog.configure(
    processors=[
        *_shared_processors,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

_console_fmt = structlog.stdlib.ProcessorFormatter(
    foreign_pre_chain=_shared_processors,
    processors=[
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
    ],
)
_file_fmt = structlog.stdlib.ProcessorFormatter(
    foreign_pre_chain=_shared_processors,
    processors=[
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.JSONRenderer(),
    ],
)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_console_fmt)
# 10 MB x 5 files = ~50 MB cap; ~weeks of data at typical chat volume.
_file_handler = RotatingFileHandler(_LOG_FILE, maxBytes=10_000_000, backupCount=5)
_file_handler.setFormatter(_file_fmt)

_root = logging.getLogger()
_root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
# Idempotent: only add handlers if not already present (avoids dup on reload).
if not any(isinstance(h, RotatingFileHandler) for h in _root.handlers):
    _root.addHandler(_console_handler)
    _root.addHandler(_file_handler)
# Quiet down noisy third-party loggers.
logging.getLogger("slack_bolt").setLevel(logging.INFO)
logging.getLogger("slack_sdk").setLevel(logging.INFO)

log = structlog.get_logger("slack-bridge")

# ── Required env validation ──────────────────────────────────────────────

REQUIRED_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET")
missing = [k for k in REQUIRED_ENV if not os.environ.get(k) or "REPLACE-ME" in os.environ[k]]
if missing:
    log.error("missing required env", missing=missing, env_path=str(ENV_PATH))
    sys.exit(1)

log.info("env loaded", repo_dir=str(REPO_DIR), env_path=str(ENV_PATH) if ENV_PATH.exists() else None,
         log_file=str(_LOG_FILE))

# ── State DB ─────────────────────────────────────────────────────────────

def resolve_state_dir() -> Path:
    if d := os.environ.get("AGENT_ME_STATE_DIR"):
        return Path(d).expanduser()
    if x := os.environ.get("XDG_STATE_HOME"):
        return Path(x).expanduser() / "agent-me"
    return Path.home() / ".local" / "state" / "agent-me"


STATE_DIR = resolve_state_dir()
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = STATE_DIR / "state.db"

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_ts      TEXT PRIMARY KEY,
    channel        TEXT NOT NULL,
    user_id        TEXT,
    auto_approve   INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_ts   TEXT NOT NULL,
    role        TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
    content     TEXT,
    slack_ts    TEXT,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY(thread_ts) REFERENCES threads(thread_ts)
);
CREATE INDEX IF NOT EXISTS messages_thread_idx ON messages(thread_ts);
CREATE TABLE IF NOT EXISTS pending_approvals (
    id                 TEXT PRIMARY KEY,
    thread_ts          TEXT NOT NULL,
    action_type        TEXT,
    payload_json       TEXT,
    status             TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','expired')),
    slack_message_ts   TEXT,
    created_at         INTEGER NOT NULL,
    resolved_at        INTEGER,
    -- Phase 2b additions (2026-05-10): the columns below were added later
    -- via ALTER TABLE for existing DBs (see _migrate_pending_approvals
    -- below). New tables include them at create time.
    tool_use_id        TEXT,
    session_id         TEXT,
    tool_name          TEXT,
    decision_reason    TEXT,
    slack_channel      TEXT,
    auto_approved      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS pending_status_idx ON pending_approvals(status);
CREATE INDEX IF NOT EXISTS pending_tool_use_idx ON pending_approvals(tool_use_id);
CREATE TABLE IF NOT EXISTS claude_sessions (
    thread_ts     TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    started_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL,
    turn_count    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS model_free_threads (
    thread_ts        TEXT PRIMARY KEY,
    subject_pattern  TEXT NOT NULL,
    last_request     TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    FOREIGN KEY(thread_ts) REFERENCES threads(thread_ts)
);
CREATE TABLE IF NOT EXISTS brev_flows (
    thread_ts        TEXT PRIMARY KEY,
    org_id           TEXT NOT NULL,
    status           TEXT NOT NULL CHECK(status IN ('active','filled','submitted','cancelled','failed')),
    last_result      TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    FOREIGN KEY(thread_ts) REFERENCES threads(thread_ts)
);
"""

db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")
db.executescript(DB_SCHEMA)


def _migrate_pending_approvals() -> None:
    """Add Phase 2b columns to `pending_approvals` if the table predates them.

    Idempotent: introspects the existing column set and only adds what's
    missing. Safe to call on every startup. SQLite ALTER TABLE doesn't
    support `IF NOT EXISTS` for columns, so we have to introspect first.
    """
    cur = db.execute("PRAGMA table_info(pending_approvals)")
    existing_cols = {row[1] for row in cur.fetchall()}
    additions: list[tuple[str, str]] = [
        ("tool_use_id", "TEXT"),
        ("session_id", "TEXT"),
        ("tool_name", "TEXT"),
        ("decision_reason", "TEXT"),
        ("slack_channel", "TEXT"),
        ("auto_approved", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col, type_ in additions:
        if col not in existing_cols:
            db.execute(f"ALTER TABLE pending_approvals ADD COLUMN {col} {type_}")
            log.info("schema_migrate_added_column",
                     table="pending_approvals", column=col)
    db.execute(
        "CREATE INDEX IF NOT EXISTS pending_tool_use_idx ON pending_approvals(tool_use_id)"
    )


def _migrate_brev_flows_status_check() -> None:
    """Rebuild `brev_flows` when it predates the `filled` status.

    SQLite cannot relax a CHECK constraint in place. Existing runtime DBs from
    the submit-only Brev flow therefore need a small table rebuild before the
    fill/screenshot state can be stored.
    """
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'brev_flows'"
    ).fetchone()
    create_sql = (row[0] if row else "") or ""
    if "'filled'" in create_sql:
        return

    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN")
        db.execute(
            """
            CREATE TABLE brev_flows_new (
                thread_ts        TEXT PRIMARY KEY,
                org_id           TEXT NOT NULL,
                status           TEXT NOT NULL CHECK(status IN ('active','filled','submitted','cancelled','failed')),
                last_result      TEXT,
                created_at       INTEGER NOT NULL,
                updated_at       INTEGER NOT NULL,
                FOREIGN KEY(thread_ts) REFERENCES threads(thread_ts)
            )
            """
        )
        db.execute(
            """
            INSERT INTO brev_flows_new
                (thread_ts, org_id, status, last_result, created_at, updated_at)
            SELECT thread_ts, org_id, status, last_result, created_at, updated_at
            FROM brev_flows
            """
        )
        db.execute("DROP TABLE brev_flows")
        db.execute("ALTER TABLE brev_flows_new RENAME TO brev_flows")
        db.execute("COMMIT")
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    log.info("schema_migrate_brev_flows_status_check")


_migrate_pending_approvals()
_migrate_brev_flows_status_check()
log.info("state db ready", db_path=str(DB_PATH))

DB_LOCK = asyncio.Lock()


async def upsert_thread(thread_ts: str, channel: str, user_id: str | None) -> None:
    now = int(time.time() * 1000)
    async with DB_LOCK:
        db.execute(
            """INSERT INTO threads (thread_ts, channel, user_id, auto_approve, created_at, last_active_at)
               VALUES (?, ?, ?, 0, ?, ?)
               ON CONFLICT(thread_ts) DO UPDATE SET last_active_at=excluded.last_active_at""",
            (thread_ts, channel, user_id, now, now),
        )


async def insert_message(thread_ts: str, role: str, content: str, slack_ts: str | None) -> None:
    now = int(time.time() * 1000)
    async with DB_LOCK:
        db.execute(
            "INSERT INTO messages (thread_ts, role, content, slack_ts, created_at) VALUES (?,?,?,?,?)",
            (thread_ts, role, content, slack_ts, now),
        )


# ── Agent session map (thread_ts → session_id) ─────────────────────────
#
# Historical note: the SQLite table is still named `claude_sessions` for
# backwards compatibility with deployed DBs and dashboard queries. New rows
# store Codex thread IDs when the Codex backend is active.
#
# Each Slack thread gets its own agent session. First message in a thread
# spawns a fresh session; subsequent messages resume it, so the CLI handles
# context/cache management. Session IDs persist in SQLite, so a bridge restart
# doesn't drop conversational continuity.

async def get_session_id(thread_ts: str) -> str | None:
    async with DB_LOCK:
        row = db.execute(
            "SELECT session_id FROM claude_sessions WHERE thread_ts=?",
            (thread_ts,),
        ).fetchone()
    return row[0] if row else None


async def upsert_session(thread_ts: str, session_id: str) -> None:
    now = int(time.time() * 1000)
    async with DB_LOCK:
        db.execute(
            """INSERT INTO claude_sessions (thread_ts, session_id, started_at, last_used_at, turn_count)
                VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(thread_ts) DO UPDATE SET
                session_id = excluded.session_id,
                last_used_at = excluded.last_used_at,
                turn_count = turn_count + 1""",
            (thread_ts, session_id, now, now),
        )


async def clear_session(thread_ts: str) -> str | None:
    """Forget the stored session for this thread. Returns the old id (or None)
    so the caller can log it. Does NOT touch the messages table — those are
    just an audit trail; the actual conversational state lives in the
    backend's session store."""
    async with DB_LOCK:
        row = db.execute(
            "SELECT session_id FROM claude_sessions WHERE thread_ts=?",
            (thread_ts,),
        ).fetchone()
        old_id = row[0] if row else None
        if old_id:
            db.execute(
                "DELETE FROM claude_sessions WHERE thread_ts=?",
                (thread_ts,),
            )
    return old_id


async def remember_model_free_thread(
    thread_ts: str,
    subject_pattern: str,
    user_request: str,
) -> None:
    now = int(time.time() * 1000)
    async with DB_LOCK:
        db.execute(
            """INSERT INTO model_free_threads
                 (thread_ts, subject_pattern, last_request, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(thread_ts) DO UPDATE SET
                 subject_pattern = excluded.subject_pattern,
                 last_request = excluded.last_request,
                 updated_at = excluded.updated_at""",
            (thread_ts, subject_pattern, user_request, now, now),
        )


async def get_model_free_thread_subject(thread_ts: str) -> str | None:
    async with DB_LOCK:
        row = db.execute(
            "SELECT subject_pattern FROM model_free_threads WHERE thread_ts=?",
            (thread_ts,),
        ).fetchone()
    return row[0] if row else None


async def get_model_free_thread_subject_from_messages(thread_ts: str) -> str | None:
    async with DB_LOCK:
        rows = db.execute(
            """SELECT content FROM messages
               WHERE thread_ts=? AND content IS NOT NULL
               ORDER BY id DESC LIMIT 40""",
            (thread_ts,),
        ).fetchall()
    for (content,) in rows:
        subject = model_free_subject_pattern_in_text(content)
        if subject:
            return subject
    return None


async def remember_brev_flow(thread_ts: str, org_id: str, status: str = "active") -> None:
    now = int(time.time() * 1000)
    async with DB_LOCK:
        db.execute(
            """INSERT INTO brev_flows
                 (thread_ts, org_id, status, last_result, created_at, updated_at)
               VALUES (?, ?, ?, NULL, ?, ?)
               ON CONFLICT(thread_ts) DO UPDATE SET
                 org_id = excluded.org_id,
                 status = excluded.status,
                 last_result = NULL,
                 updated_at = excluded.updated_at""",
            (thread_ts, org_id, status, now, now),
        )


async def get_active_brev_flow(thread_ts: str) -> dict[str, Any] | None:
    async with DB_LOCK:
        row = db.execute(
            """SELECT thread_ts, org_id, status, last_result, created_at, updated_at
               FROM brev_flows
               WHERE thread_ts=? AND status='active'""",
            (thread_ts,),
        ).fetchone()
    if not row:
        return None
    return {
        "thread_ts": row[0],
        "org_id": row[1],
        "status": row[2],
        "last_result": row[3],
        "created_at": row[4],
        "updated_at": row[5],
    }


async def finish_brev_flow(thread_ts: str, status: str, last_result: str | None = None) -> None:
    now = int(time.time() * 1000)
    async with DB_LOCK:
        db.execute(
            """UPDATE brev_flows
               SET status=?, last_result=?, updated_at=?
               WHERE thread_ts=?""",
            (status, last_result, now, thread_ts),
        )


# Routing rules baked into every spawn so the Codex orchestrator knows how to
# use app plugins and MaaS MCP directly. No PA CLI fallback remains in the
# Slack bridge path.
SYSTEM_PROMPT_TEMPLATE = """\
You are agent-me, the user's autonomous personal assistant running on Codex. You are not Claude. Claude Code is only a legacy OAuth bootstrap helper for MaaS MCP auth and is not the chat/brief reasoning backend. The bridge spawning you is responsible for posting your final text to the user's Slack thread — do NOT call a Slack send/post tool for your ordinary reply. Today: {today}. Timezone: Asia/Ho_Chi_Minh.

ROUTING RULES — apply automatically.

1. CODEX APP TOOLS ARE THE DEFAULT READ PATH.
   - Use the available Codex app/MCP tools directly. Do not use shell commands to call PA, Claude, browser automation, or local CLIs for enterprise-source reads.
   - Do not read local skill files during Slack-chat turns. The bridge prompt below gives the tool routing you need; shell approvals are not available in headless exec mode.

2. SOURCE → TOOL MAPPING
   - Microsoft Teams chats/channels → `microsoft teams_list_chats`, `microsoft teams_list_chat_messages`, `microsoft teams_list_channel_messages`, `microsoft teams_search`, then `microsoft teams_fetch` for exact paths.
   - Outlook email → `microsoft outlook email_list_messages`, `microsoft outlook email_search_messages`, then `microsoft outlook email_fetch_message` only when details are needed.
   - Outlook calendar / meetings → use the Outlook Calendar app tools if available.
   - Slack → `slack_slack_search_public_and_private`, `slack_slack_read_channel`, `slack_slack_read_thread`, `slack_slack_search_users`, `slack_slack_read_user_profile`.
   - Google Drive / Docs / Sheets / Slides → `google drive_search`, `google drive_recent_documents`, `google drive_fetch`, and the narrower Google file tools when needed.
   - GitHub → GitHub app tools or `gh` only for explicit repo/PR work that truly requires the local CLI.
   - Jira / GitLab / Confluence / Glean / OneDrive / SharePoint / NVBugs / IPPSEC / Jama / MySQL / Nsight-CUDA / NVKS / PagerDuty → use the registered `maas-*` Codex MCP servers when present.

3. PARALLEL FAN-OUT
   - For requests spanning multiple sources, call the independent app/MCP tools in parallel when the runtime allows it. Keep each query narrow enough to summarize.

4. GRACEFUL FALLBACK
   - On a resumed session, ignore prior-turn claims that tools were disconnected. Try the relevant app/MCP tool fresh for the current turn before claiming inability.
   - If one source is unavailable, say which source failed and continue with the sources that worked.

5. WRITES
   - This generic `codex exec` path is for reads, synthesis, and ordinary replies. Permissioned connector/MCP writes must go through the bridge's Codex app-server auto-review route. If an explicit connector/MCP write request reaches this path, do not perform the write; say the bridge needs to route it through app-server with the exact target/action.
   - Do not call Slack send/post to answer in the current agent-me thread. The bridge posts your final text. Slack write tools are only for a separate requested Slack destination, and those requests should normally be routed before this prompt.
   - Model Free Outlook draft requests are handled by the bridge's dedicated app-server route. This standing rule does not apply to read-only daily/weekly/monthly brief jobs.

6. REPLY — synthesize, do NOT dump
   - When all parallel calls return, SYNTHESIZE results into ONE concise final-text reply. The bridge posts it to Slack automatically — you do NOT call any Slack tool.
   - **Hard size budget: keep the final reply under ~6000 characters total.** Slack rejects long messages even when chunked; staying under 6k means the bridge can post the digest as a single clean message rather than fragmenting it across thread replies. Treat 6000 as a ceiling, not a target.
   - Do NOT paste raw tool output. Extract the items that matter and rewrite them in your own compact format.
   - Format guideline: short header per section (📅 Meetings / 💬 Teams / 🟪 Slack / ✉️ Email). 3-8 bullets each, each bullet on one line: `HH:MM · who · subject · one-line note · link`. Drop noise (auto-bug emails, mailing-list traffic without a direct mention to you or your teams).
"""


def build_system_prompt() -> str:
    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
    return SYSTEM_PROMPT_TEMPLATE.format(today=today)


BREV_FORM_URL = "https://nvidia.tfaforms.net/32"
BREV_NOTIFY_EMAIL = "thaphan@nvidia.com"
BREV_PLAYWRIGHT_PROFILE_DIR = STATE_DIR / "playwright-profile"
BREV_PLAYWRIGHT_OUTPUT_DIR = STATE_DIR / "playwright-output"
BREV_ORG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,80}$")
BREV_FILLED_RE = re.compile(r"(?im)^\s*BREV_FILLED\b.*$")
BREV_SUBMITTED_RE = re.compile(r"(?im)^\s*BREV_SUBMITTED\b.*$")
BREV_AUTH_ALIASES = {"auth", "reauth", "login", "sso", "browser-auth", "browser auth"}
BREV_PLAYWRIGHT_APPROVAL_CONFIGS = (
    'mcp_servers.maas-playwright.tools.browser_navigate.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_snapshot.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_tabs.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_take_screenshot.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_evaluate.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_click.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_type.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_fill_form.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_select_option.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_check.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_uncheck.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_press_key.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_wait_for.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_handle_dialog.approval_mode="approve"',
    'mcp_servers.maas-playwright.tools.browser_resize.approval_mode="approve"',
)
BREV_PLAYWRIGHT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
BREV_PLAYWRIGHT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BREV_SYSTEM_PROMPT_TEMPLATE = """\
You are agent-me running the operator's Brev credits request flow on Codex.
This is an interactive, stateful workflow for Slack thread `{thread_ts}`.

Goal:
- Request Brev credits for org id `{org_id}` using the web form at {form_url}.
- Use the registered `maas-playwright` MCP server for browser automation.
- Do not use shell commands, local browser CLIs, or the Slack bot token.
- Do not send Slack messages yourself.

Current test-stage goal:
- Fill the Brev form with the reference/sample values below.
- Take a screenshot of the filled form/review state.
- Do NOT click the final submit button.
- Do NOT submit the form in this stage, even if the user says they allow all
  browser automation. Submit support will be added in a later prompt revision.

Reference/sample values from `brev/` screenshots:
- Name / Requestor Name: `Thanh Phan`
- Email / Requestor Email: `thaphan@nvidia.com`
- Account listed in NVCRM/SFDC: `No Specific Account`
- Credits requested: `1000`
- Credit use: `Brev Credit Request - Other`
- Other reason for credit use: `Working internal projects`
- Additional details: leave blank unless the live page requires it
- Brev Credit Coupons needed: leave blank unless the live page requires it
- Existing Brev org name: `{org_id}`
- GPU configuration / Launchable / approximate cost: use
  `data-flywheel launchable, approx. cost $12.07/hr` when the live page asks
  for this field.

Hard constraints:
- Do not invent, infer, or auto-approve form answers.
- You may fill `{org_id}` only into fields that clearly ask for org/project/team
  id/name. For all other missing fields, ask the user for the exact value.
- For every form page, inspect the visible fields and options before filling.
- Fill only the reference/sample values above, values the user explicitly
  provides in this Slack thread, values visibly prefilled by the page, or the
  org id where it clearly applies.
- If a page has required fields that are not answered, stop and ask one concise
  question listing the exact visible field labels/options you need next.
- If a select/radio/checkbox choice is required, present the exact visible
  options and ask the user to choose. Do not choose defaults unless the page
  has already selected them.
- If Microsoft/NVIDIA SSO or another login wall appears, stop and say that the
  browser session needs to be authenticated with `brev auth` or the Brev auth
  helper. Do not ask for or collect passwords.
- If a final submit button is visible, do not click it. Instead, take a
  screenshot of the filled state and return it for review.
- If the page blocks you, times out, or requires unavailable auth, report the
  exact blocker and ask for the next user-provided step.
- If browser state is lost between Slack turns, reopen the form and refill
  only values already present in this session before continuing.

Completion contract:
- When the form is filled and a screenshot is captured, make your first
  final-response line exactly:
  BREV_FILLED org_id={org_id} screenshot=<path-or-artifact-name>
- After that marker, add one concise Vietnamese line summarizing what was
  filled and where the screenshot was saved/returned.
- In the current test stage, never include `BREV_SUBMITTED`.
- For future submit-stage support only: when and only when a future prompt
  explicitly enables submit and the form submit succeeds, make your first
  final-response
  line exactly:
  BREV_SUBMITTED org_id={org_id}
- If the form is not filled yet, do not include `BREV_FILLED` or
  `BREV_SUBMITTED`.
"""


def build_brev_system_prompt(org_id: str, thread_ts: str) -> str:
    return BREV_SYSTEM_PROMPT_TEMPLATE.format(
        org_id=org_id,
        thread_ts=thread_ts,
        form_url=BREV_FORM_URL,
    )


BREV_AUTH_SYSTEM_PROMPT = """\
You are agent-me checking the operator's Brev browser SSO state.

Use only the registered `maas-playwright` MCP browser tools. Do not use shell
commands, local browser CLIs, or Slack tools.

Hard constraints:
- Do not ask for, type, collect, or store usernames, passwords, OTPs, MFA
  codes, passkeys, or recovery codes.
- Do not submit any Brev credits request.
- If Microsoft/NVIDIA SSO or another login wall is visible, stop there and
  report that the persistent browser profile needs manual authentication.
- If the Brev form is visible, take a screenshot and report that auth is ready.
"""

# Legacy Claude approval-gate toggle. Codex is now the default chat
# orchestrator, so this path only matters if a future operator explicitly
# revives Claude Code hooks.
APPROVAL_GATE_ON = os.environ.get("APPROVAL_GATE", "0") == "1"

# Default 5 min for routine app/MCP reads. Operator can override via
# AGENT_TIMEOUT_S or CODEX_TIMEOUT_S.
AGENT_TIMEOUT_S = float(
    os.environ.get(
        "AGENT_TIMEOUT_S",
        os.environ.get("CODEX_TIMEOUT_S", 5 * 60),
    )
)
MAX_SLACK_TEXT = 39000
MAX_LOG_TEXT = 4000

CODEX_BIN = resolve_cli_bin("CODEX_BIN", "codex")
MODEL = os.environ.get("CODEX_MODEL", os.environ.get("AGENT_MODEL", "gpt-5.5"))

# Chat-only working directory for headless Codex invocations from Slack.
#
# Why not REPO_DIR: REPO_DIR holds the agent-me project's CLAUDE.md,
# which contains the "auto memory" protocol meant for development
# sessions. When a Slack user asked the bot to remember something, a
# prior Claude backend faithfully
# followed the protocol — read MEMORY.md, wrote a new memory file, and
# updated the index. 10 turns, 78s, $1.09 for one chat message. Bridge
# users want a chat assistant, not a memory-management agent.
#
# A purpose-built empty cwd has no CLAUDE.md, no auto-memory directives,
# and no project-specific tooling instructions. Codex session ids are
# persisted in SQLite and resumed with `codex exec resume`.
#
# MCP tools are user-scope, so they're available from any cwd.
CHAT_CWD = STATE_DIR / "chat-cwd"
CHAT_CWD.mkdir(parents=True, exist_ok=True)

# ── Helpers ──────────────────────────────────────────────────────────────

def clip(s: str | None, n: int = MAX_LOG_TEXT) -> str | None:
    if not s:
        return s
    return s if len(s) <= n else f"{s[:n]}…[+{len(s) - n} chars]"


def truncate_for_slack(text: str | None) -> str:
    if not text:
        return "_(no output)_"
    if len(text) <= MAX_SLACK_TEXT:
        return text
    return f"{text[:MAX_SLACK_TEXT - 120]}\n\n_…[truncated; {len(text) - MAX_SLACK_TEXT} chars cut]_"


# Slack documented chat.update text cap is 40_000, but empirically
# the live API rejected 39k AND 12k Vietnamese-heavy payloads with
# `msg_too_long`. Multi-byte chars + mrkdwn rendering + link unfurls
# count against an internal byte budget we cannot inspect. 2500 chars
# is well under Block Kit's per-text 3000-char ceiling, which is the
# tightest documented Slack limit, and survives every payload we've
# tried.
SLACK_CHUNK_SIZE = 2_500


def chunk_for_slack(text: str) -> list[str]:
    """Split a long reply into Slack-safe chunks, preferring newline breaks."""
    if not text:
        return ["_(no output)_"]
    if len(text) <= SLACK_CHUNK_SIZE:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= SLACK_CHUNK_SIZE:
            chunks.append(remaining)
            break
        # Prefer breaking on a newline near the limit for readability;
        # fall back to a hard cut if no convenient newline exists in the
        # back half of the slice.
        cut = remaining.rfind("\n", SLACK_CHUNK_SIZE // 2, SLACK_CHUNK_SIZE - 100)
        if cut < SLACK_CHUNK_SIZE // 2:
            cut = SLACK_CHUNK_SIZE - 100
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    total = len(chunks)
    return [
        f"{c}\n\n_…(part {i+1}/{total})_" if i < total - 1
        else f"{c}\n\n_…(part {i+1}/{total} — end)_"
        for i, c in enumerate(chunks)
    ]


# Streaming progress: Slack tier-2 chat.update is ~1 req/s before
# throttling kicks in. Two seconds gives headroom across burst events
# (the orchestrator can emit a flurry of tool_use blocks in one
# assistant turn) without making the placeholder feel stale.
PROGRESS_UPDATE_MIN_INTERVAL_S = 2.0


def format_progress(state: dict[str, Any]) -> str:
    """Render the live progress block shown on the Slack placeholder."""
    started = state["tools_started"]
    done = state["tools_done"]
    in_flight = list(state["in_flight"].values())
    completed = state["completed"]
    lines = [f"🔄 *{done}/{started} tool calls done* (live progress)"]
    if in_flight:
        head = ", ".join(f"`{t}`" for t in in_flight[:6])
        if len(in_flight) > 6:
            head += f", +{len(in_flight) - 6} more"
        lines.append(f"▸ running: {head}")
    if completed:
        recent = completed[-6:]
        lines.append("▸ completed: " + ", ".join(f"`{t}`" for t in recent))
    return "\n".join(lines)


async def post_chunked_reply(
    client, *, channel: str, placeholder_ts: str, thread_ts: str, text: str,
) -> None:
    """Replace placeholder with first chunk; post remaining chunks in-thread.

    Slack's `chat.update` rejects messages that `chat.postMessage` accepts
    of identical size — we have empirically seen 2500-char Vietnamese
    payloads bounce with `msg_too_long` on update while the same content
    posts cleanly as a fresh message. So: try chat.update first, and if
    it fails for any reason, demote the placeholder to a short status
    line and post every chunk as a fresh thread message instead.
    """
    chunks = chunk_for_slack(text)
    try:
        await client.chat_update(
            channel=channel, ts=placeholder_ts, text=chunks[0],
        )
        for chunk in chunks[1:]:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk,
            )
        return
    except Exception as exc:
        log.warning("chat_update_fallback", err=str(exc),
                    placeholder_ts=placeholder_ts, chunks=len(chunks))
    # Fallback: short placeholder + every chunk as a fresh in-thread message.
    try:
        await client.chat_update(
            channel=channel, ts=placeholder_ts,
            text=f"✅ done — reply in {len(chunks)} part(s) below"
            if len(chunks) > 1 else "✅ done — reply below",
        )
    except Exception as exc:
        log.warning("placeholder_finalize_failed", err=str(exc))
    for chunk in chunks:
        try:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk,
            )
        except Exception as exc:
            log.error("chunk_post_failed", err=str(exc),
                      chunk_len=len(chunk))


def strip_bot_mention(text: str | None) -> str:
    return re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", text or "").strip()


async def run_command(cmd: list[str], cwd: str, timeout: float = 30.0) -> str:
    """Run an arbitrary command, capture stdout, raise on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{cmd[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} exited {proc.returncode}: {stderr.decode(errors='replace')[:500]}"
        )
    return stdout.decode(errors="replace")


class SessionExpired(RuntimeError):
    """Raised when --resume <id> hits a session that no longer exists.

    Caller should retry without --resume to start a fresh conversation."""


def _codex_args(
    prompt: str,
    resume_session_id: str | None,
    extra_configs: tuple[str, ...] = (),
) -> list[str]:
    args = [CODEX_BIN]
    for cfg in extra_configs:
        args.extend(["-c", cfg])
    if resume_session_id:
        args.extend([
            "exec", "resume",
            "--json",
            "--skip-git-repo-check",
            "-m", MODEL,
            resume_session_id,
            prompt,
        ])
        return args
    args.extend([
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--cd", str(CHAT_CWD),
        "-m", MODEL,
        prompt,
    ])
    return args


def _codex_item_name(item: dict[str, Any]) -> str:
    if item.get("type") == "mcp_tool_call":
        server = item.get("server") or "mcp"
        tool = item.get("tool") or "tool"
        return f"{server}:{tool}"
    if item.get("type") == "command_execution":
        return "shell"
    return str(item.get("type") or "item")


def _codex_app_server_args(prompt: str) -> list[str]:
    return codex_app_server_args(CODEX_BIN, prompt)


def parse_app_server_final_message(output: str) -> str | None:
    return _parse_app_server_final_message(output)


async def spawn_codex_app_server(prompt: str) -> tuple[str, str | None]:
    """Run one app-server turn for permissioned connector/MCP writes.

    `codex exec` can report `user cancelled MCP tool call` for app writes in
    headless mode. The app-server path performs the same connector/MCP call
    through Codex auto-review and can complete approved low-risk writes.
    """
    log.info("codex_app_server_spawn", model=MODEL, prompt_len=len(prompt))
    final = await run_codex_app_server(
        prompt,
        codex_bin=CODEX_BIN,
        cwd=REPO_DIR,
        timeout_s=AGENT_TIMEOUT_S,
    )
    log.info("codex_app_server_done", response=clip(final, 500))
    return final, None


async def spawn_codex(
    prompt: str,
    *,
    resume_session_id: str | None = None,
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    thread_ts: str | None = None,
    system_prompt: str | None = None,
    extra_configs: tuple[str, ...] = (),
) -> tuple[str, str | None]:
    """Spawn `codex exec --json` and stream events.

    Parses each JSONL event as it arrives so the wrapper can track
    MCP/command start/complete and surface live progress through
    `progress_cb` (the bridge passes a callback that writes a throttled
    Slack chat.update on the placeholder message).

    `progress_cb` receives a snapshot dict with:
      - tools_started: int
      - tools_done: int
      - in_flight: dict[item_id -> tool_name] (currently running)
      - completed: list[tool_name] (in completion order)
      - session_id: str | None
      - final_text: str | None (latest agent_message)
      - is_error: bool / error_message: str | None

    Returns (final_response_text, session_id_or_None).

    Raises SessionExpired if --resume hit a missing session — caller can
    retry without --resume to recover.
    """
    prompt_with_system = f"{system_prompt or build_system_prompt()}\n\nUSER REQUEST:\n{prompt}"
    args = _codex_args(prompt_with_system, resume_session_id, extra_configs)
    log.info("codex_spawn", cwd=str(CHAT_CWD), model=MODEL,
             prompt_len=len(prompt), resume=resume_session_id)

    spawn_env = os.environ.copy()
    spawn_env.update(codex_mcp_token_env())
    if thread_ts:
        spawn_env["AGENT_ME_THREAD_TS"] = thread_ts
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(CHAT_CWD), env=spawn_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )

    state: dict[str, Any] = {
        "tools_started": 0,
        "tools_done": 0,
        "in_flight": {},        # tool_use_id -> tool_name
        "completed": [],        # list of completed tool names, in order
        "session_id": None,
        "final_text": None,
        "is_error": False,
        "error_message": None,
    }

    async def _emit():
        if progress_cb is None:
            return
        try:
            await progress_cb(state)
        except Exception as exc:
            log.warning("progress_cb_failed", err=str(exc))

    stderr_chunks: list[bytes] = []

    async def drain_stderr():
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            stderr_chunks.append(chunk)

    stderr_task = asyncio.create_task(drain_stderr())

    try:
        try:
            async with asyncio.timeout(AGENT_TIMEOUT_S):
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        # The CLI may interleave non-JSON warnings on stdout.
                        continue
                    etype = evt.get("type")
                    changed = False
                    if etype == "thread.started":
                        sid = evt.get("thread_id")
                        if sid:
                            state["session_id"] = sid
                        changed = True
                    elif etype == "item.started":
                        item = evt.get("item") or {}
                        iid = item.get("id")
                        if iid and item.get("type") in {"mcp_tool_call", "command_execution"}:
                            state["in_flight"][iid] = _codex_item_name(item)
                            state["tools_started"] += 1
                            changed = True
                    elif etype == "item.completed":
                        item = evt.get("item") or {}
                        iid = item.get("id")
                        itype = item.get("type")
                        if iid in state["in_flight"]:
                            state["completed"].append(state["in_flight"].pop(iid))
                            state["tools_done"] += 1
                            changed = True
                        if itype == "agent_message":
                            state["final_text"] = item.get("text") or ""
                            changed = True
                        elif itype == "error":
                            msg = str(item.get("message") or "")
                            # Codex Cloud requirements currently emit this
                            # warning in every exec; it is not turn-fatal.
                            if "approval_policy" not in msg:
                                state["is_error"] = True
                                state["error_message"] = msg or "unknown Codex error"
                                changed = True
                    elif etype == "turn.completed":
                        changed = True
                    if changed:
                        await _emit()
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"codex timed out after {AGENT_TIMEOUT_S}s") from exc
    finally:
        try:
            await asyncio.wait_for(stderr_task, timeout=2.0)
        except TimeoutError:
            stderr_task.cancel()

    return_code = await proc.wait()
    stderr_text = b"".join(stderr_chunks).decode(errors="replace").strip()

    if return_code != 0:
        err = stderr_text[:500]
        err_l = err.lower()
        if resume_session_id and (
            "session not found" in err_l
            or "thread not found" in err_l
            or "no such session" in err_l
            or "invalid session" in err_l
            or "not found" in err_l
        ):
            raise SessionExpired(
                f"resume failed for {resume_session_id[:8]}…: {err}"
            )
        raise RuntimeError(f"codex exited {return_code}: {err}")

    if state["is_error"]:
        raise RuntimeError(f"codex error: {state['error_message']}")

    final = (state["final_text"] or "").strip()
    log.info("codex_done",
             session_id=state["session_id"],
             tools_started=state["tools_started"],
             tools_done=state["tools_done"])
    return final, state["session_id"]


# ── Bolt app ────────────────────────────────────────────────────────────

app = AsyncApp(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)


# ── Slash command bodies (shared by native slash + text intercept) ──────

HELP_TEXT = "\n".join((
    "*agent-me bot — built-in commands*",
    "",
    "Type any of these as `/cmd`, plain text (`brief`, `mcp`, `reauth`…), or click buttons in posted messages — all three work.",
    "",
    "• `brief` / `/brief` — daily brief (Jira + GitLab + GitHub + NVBugs + Outlook + Calendar)",
    "• `brief week` / `/brief week` — weekly recap (last 7 days)",
    "• `brief month` / `/brief month` — monthly recap (last 30 days)",
    "• `brev <org_id>` / `/brev <org_id>` — fill the Brev credits form in test mode and return a screenshot",
    "• `brev auth` / `/brev auth` — check or refresh the persistent Brev browser SSO profile",
    "• `model free draft` — find latest `Model Free 2.0` email and create a reply-all Outlook draft",
    "• `mcp` / `/mcp` — list MCP server health & auth status",
    "• `reauth` / `/reauth` — trigger Codex MCP re-auth helper",
    "• `reset` / `clear` / `new` — start a fresh Codex session for this thread (drops prior context)",
    "• `whoami` / `/whoami` — show your Slack user id",
    "• `version` / `/version` — bridge + Codex versions and pinned model",
    "• `help` / `/help` — this message",
    "",
    "_Anything else is sent to Codex — context is preserved per Slack thread (reply-in-thread to keep the conversation going; new top-level message = fresh session)._",
    "_The bridge auto-checks MCP auth health every 6h and DMs you when re-auth is needed._",
    "_Daily morning routine fires at 6am Vietnam time when the bridge is running._",
))


def _missing_codex_mcp_token_envs(mcp_list_output: str) -> list[str]:
    """Return Codex MCP server names whose bearer env var has no local token."""
    available = codex_mcp_token_env()
    missing: set[str] = set()
    for line in mcp_list_output.splitlines():
        match = MCP_TOKEN_ENV_RE.search(line)
        if not match:
            continue
        env_name = match.group(0)
        if env_name in available:
            continue
        server = line.split(None, 1)[0].strip()
        if server and server != "Name" and server not in CONNECTOR_COVERED_MAAS:
            missing.add(server)
    return sorted(missing)


async def cmd_mcp() -> str:
    out = await run_command([CODEX_BIN, "mcp", "list"], cwd=str(REPO_DIR), timeout=60.0)
    missing_tokens = _missing_codex_mcp_token_envs(out)
    token_note = ""
    if missing_tokens:
        token_note = (
            "\n\n⚠️ Missing bearer token(s) in the local MaaS credential store: "
            + ", ".join(f"`{name}`" for name in missing_tokens)
            + "\nRun `/reauth`, then `/mcp` again."
        )
    return (
        "`codex mcp list`:\n```\n" + out.strip() + "\n```\n"
        "_Codex app plugins for Teams/Slack/Outlook/GDrive are enabled separately; "
        "this list shows extra Codex MCP servers such as MaaS/NVBugs._"
        + token_note
    )


async def cmd_version() -> str:
    ver = (await run_command([CODEX_BIN, "--version"], cwd=str(REPO_DIR))).strip()
    return (
        f"*Bridge:* python · *Agent:* Codex · *Model:* `{MODEL}`\n"
        f"*Codex CLI:* `{ver}`\n"
        f"*Repo:* `{REPO_DIR}`"
    )


async def cmd_whoami(user_id: str | None) -> str:
    return f"Your Slack user id: `{user_id or '(unknown — DM the bot once first)'}`"


BRIEF_LOG_FILE = _LOG_DIR / "brief.log"
MODEL_FREE_SUBJECT_PATTERN = os.environ.get("MODEL_FREE_SUBJECT_PATTERN", "Model Free 2.0")
MODEL_FREE_DRAFT_BODY = os.environ.get(
    "MODEL_FREE_DRAFT_BODY",
    "Received. Will start testing today\n\nBest regards\nThanh Phan",
)
MODEL_FREE_VERSION_RE = re.compile(
    r"\bmodel[-\s]*free\s+(?P<version>\d+(?:\.\d+)+)\b",
    re.IGNORECASE,
)
MODEL_FREE_EMAIL_TERMS = (
    "email", "mail", "outlook", "subject", "inbox",
)
MODEL_FREE_FORCE_DRAFT_TERMS = (
    "draft", "reply all", "reply-all", "create reply", "create draft",
)
MODEL_FREE_FOLLOWUP_TERMS = (
    "draft", "reply all", "reply-all", "confirm", "execute", "same email",
    "this email", "right email", "again", "test feature",
)
OUTLOOK_WRITE_CONTEXT_TERMS = (
    "email", "mail", "outlook", "reply all", "reply-all",
)
OUTLOOK_WRITE_ACTION_TERMS = (
    "draft", "soan", "reply all", "reply-all", "create reply", "create draft",
    "compose", "phan hoi", "tra loi",
)
PERMISSIONED_CONNECTOR_WRITE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(send|post|dm)\b.*\b(slack|teams|email|mail|outlook|message)\b",
        r"\b(slack|teams)\b.*\b(send|post|dm)\b",
        r"\b(email|mail|outlook)\b.*\b(draft|compose|reply all|reply-all|reply|send)\b",
        r"\b(draft|compose|reply all|reply-all|reply)\b.*\b(email|mail|outlook)\b",
        r"\b(create|open)\b.*\b(jira|gitlab|nvbugs|bug|issue|ticket)\b",
        r"\b(comment|assign|transition|close|resolve|reopen)\b.*\b(jira|gitlab|nvbugs|bug|issue|ticket)\b",
        r"\b(jira|gitlab|nvbugs|bug|issue|ticket)\b.*\b(comment|assign|transition|close|resolve|reopen)\b",
        r"\b(share|upload|create|edit)\b.*\b(google drive|gdrive|drive|docs|sheets|slides|confluence|doc|sheet|slide|page)\b",
        r"\b(schedule|invite|cancel)\b.*\b(calendar|meeting|outlook calendar)\b",
        r"\b(calendar|meeting|outlook calendar)\b.*\b(schedule|invite|cancel)\b",
        r"\b(gui|nhan|post)\b.*\b(slack|teams|email|mail|outlook|tin nhan)\b",
        r"\b(slack|teams|email|mail|outlook|tin nhan)\b.*\b(gui|nhan|post)\b",
        r"\b(soan|tra loi|phan hoi)\b.*\b(email|mail|outlook)\b",
        r"\b(tao|binh luan|gan|dong|mo lai)\b.*\b(jira|gitlab|nvbugs|bug|issue|ticket)\b",
        r"\b(chia se|upload|tai len|tao|chinh sua)\b.*\b(gdrive|drive|docs|sheets|slides|confluence)\b",
        r"\b(dat lich|moi|huy)\b.*\b(calendar|meeting|lich|cuoc hop)\b",
    )
)
READONLY_UPDATE_ME_PATTERNS = (
    "update me",
    "cap nhat cho toi",
    "cap nhat minh",
)
READONLY_CONNECTOR_QUERY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(find|get|fetch|read|list|show|search|check|summarize|test)\b.*\b(open\s+)?(jira|gitlab|nvbugs|bugs?|issues?|tickets?)\b",
        r"\b(open\s+)?(jira|gitlab|nvbugs|bugs?|issues?|tickets?)\b.*\b(find|get|fetch|read|list|show|search|check|summarize|test)\b",
    )
)


def model_free_subject_pattern_from_text(text: str | None) -> str:
    """Prefer the exact Model Free version mentioned by the user."""
    subject = model_free_subject_pattern_in_text(text)
    if subject:
        return subject
    return MODEL_FREE_SUBJECT_PATTERN


def model_free_subject_pattern_in_text(text: str | None) -> str | None:
    """Return an exact Model Free subject pattern if the text contains one."""
    match = MODEL_FREE_VERSION_RE.search(text or "")
    if match:
        return f"Model Free {match.group('version')}"
    return None


def looks_like_model_free_email_request(text: str | None) -> bool:
    """Route Model Free email requests through the deterministic draft helper."""
    lowered = (text or "").lower()
    if not re.search(r"\bmodel[-\s]*free\b", lowered):
        return False
    return (
        any(term in lowered for term in MODEL_FREE_EMAIL_TERMS)
        or model_free_request_forces_draft(lowered)
    )


def model_free_request_forces_draft(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(term in lowered for term in MODEL_FREE_FORCE_DRAFT_TERMS)


def looks_like_model_free_followup_request(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(term in lowered for term in MODEL_FREE_FOLLOWUP_TERMS)


def ascii_search_text(text: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def looks_like_outlook_write_request(text: str | None) -> bool:
    lowered = ascii_search_text(text)
    if not any(term in lowered for term in OUTLOOK_WRITE_ACTION_TERMS):
        return False
    return any(term in lowered for term in OUTLOOK_WRITE_CONTEXT_TERMS)


def looks_like_permissioned_connector_write_request(text: str | None) -> bool:
    """Detect explicit connector/MCP writes that need app-server auto-review."""
    lowered = ascii_search_text(text)
    if not lowered:
        return False
    if any(term in lowered for term in READONLY_UPDATE_ME_PATTERNS):
        return False
    if any(pattern.search(lowered) for pattern in READONLY_CONNECTOR_QUERY_PATTERNS):
        return False
    if looks_like_outlook_write_request(text):
        return True
    return any(pattern.search(lowered) for pattern in PERMISSIONED_CONNECTOR_WRITE_PATTERNS)


def looks_like_nvbugs_read_request(text: str | None) -> bool:
    """Route read-only NVBugs lookups through the source-specific fetcher."""
    lowered = ascii_search_text(text)
    if not lowered:
        return False
    if not re.search(r"\b(nvbugs|bug|bugs)\b", lowered):
        return False
    return any(pattern.search(lowered) for pattern in READONLY_CONNECTOR_QUERY_PATTERNS)


async def cmd_nvbugs_read() -> str:
    """Fetch the operator's open NVBugs with the hardened source prompt."""
    from agent_me.scripts import daily_brief

    spec = next(s for s in daily_brief.SOURCES if s.id == "nvbugs")
    data = await spec.fetcher(spec, 1)
    items = daily_brief.parse_nvbugs(data, spec)
    if not items:
        return "NVBugs: không có open bug nào match `QA Eng = Thanh Phan` hoặc `ARB = Thanh Phan`."

    lines = [
        "*NVBugs open bugs* — `QA Eng = Thanh Phan` hoặc `ARB = Thanh Phan`"
    ]
    for item in items:
        title = item.title or "(no title)"
        reason = item.reason or "matched"
        status = item.status or "Open"
        group = item.group or "uncategorized"
        url = item.url or f"https://nvbugspro.nvidia.com/bug/{item.item_id}"
        lines.append(
            f"- <{url}|{item.item_id}> — {title} — `{reason}` — {status} — {group}"
        )
    return "\n".join(lines)


async def cmd_brief(
    args_text: str = "",
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
) -> str:
    """Spawn `uv run agent-me-brief --period <X>` detached.

    The brief script posts a placeholder DM immediately and updates it as
    it progresses (Step 1/3 → 2/3 → 3/3 → final blocks). If the script
    itself crashes, stdout/stderr are appended to brief.log so `tail -f`
    on the host shows what happened.
    """
    arg = (args_text or "").strip().lower()
    period_map = {
        "": "day", "day": "day", "daily": "day", "today": "day",
        "w": "week", "week": "week", "weekly": "week", "7d": "week",
        "m": "month", "month": "month", "monthly": "month", "30d": "month",
    }
    period = period_map.get(arg)
    if period is None:
        return f"Unknown period `{arg}`. Try `/brief`, `/brief week`, or `/brief month`."

    # Open the brief log in append mode and pass its fd to the subprocess.
    # Parent closes its fd after spawn; the child keeps its dup'd copy.
    BRIEF_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(BRIEF_LOG_FILE, "ab")  # noqa: SIM115 - child process keeps this fd
    log_fp.write(
        f"\n=== {datetime.now().isoformat()} brief --period {period} (pid {os.getpid()} parent) ===\n".encode()
    )
    log_fp.flush()
    try:
        cmd = [_UV_BIN, "run", "agent-me-brief", "--period", period]
        if channel:
            cmd.extend(["--channel", channel])
        if thread_ts:
            cmd.extend(["--thread-ts", thread_ts])
        cmd.extend(["--mirror-email", "thaphan@nvidia.com"])
        await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(REPO_DIR),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log_fp, stderr=log_fp,
            start_new_session=True,
        )
    finally:
        log_fp.close()

    label = {"day": "Daily", "week": "Weekly", "month": "Monthly"}[period]
    return (
        f"📅 *{label} brief generation started.* "
        "Each platform will post as its own message in this thread, and the same brief "
        "will be mirrored to `thaphan@nvidia.com`. Total time ~30-90s.\n"
        f"_If something goes wrong, tail `{BRIEF_LOG_FILE}` for crash details "
        "or run `/mcp` + `/reauth` for stale tokens._"
    )


async def cmd_model_free_draft(
    thread_ts: str | None = None,
    *,
    subject_pattern: str | None = None,
    user_request: str | None = None,
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> str:
    subject_pattern = subject_pattern or MODEL_FREE_SUBJECT_PATTERN
    prompt = f"""The user explicitly authorized an Outlook reply-all draft for a Model Free email.

Use only the Codex Outlook Email connector tools. Do not use shell commands.
Do not send the email; create exactly one new reply-all draft.
Do not ask for confirmation. Do not skip because a previous user-authored
reply or draft already exists; this is a feature test and the user wants a
fresh draft.

Find the latest received email message where:
- subject contains the exact normalized pattern `{subject_pattern}` case-insensitively
- the message was sent to or cc'd to the signed-in user
- the latest matching message is not authored by the signed-in user

Subject matching rule:
- Treat `Model Free 2.0.4` and `model-free 2.0.4` as equivalent.
- The version must match exactly.
- Reject subjects such as `ga-model-free-nim 2.0.4` when `{subject_pattern}`
  was requested, because `nim` appears between `free` and the version.
- Prefer the newest inbound non-self message by received time among exact
  matches.

Search recent mail first, then widen if necessary. Fetch the exact target
message before drafting. Create the reply-all draft tied to that exact message
with this exact plain-text body:

{MODEL_FREE_DRAFT_BODY}

Current user request: {user_request or "/model-free-draft"}

Return a concise Vietnamese status:
- if created: draft created, subject, sender, received time, and source link if available
- if no match: say no matching `{subject_pattern}` email was found
- if the connector fails: include the exact failure in one short line
"""
    if progress_cb:
        await progress_cb({
            "tools_started": 1,
            "tools_done": 0,
            "in_flight": {"app-server": "codex-app-server:outlook"},
            "completed": [],
            "session_id": None,
            "final_text": None,
            "is_error": False,
            "error_message": None,
        })
    answer, _sid = await spawn_codex_app_server(prompt)
    return answer.strip() or "_(no output)_"


def is_brev_auth_args(args_text: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", (args_text or "").strip().lower())
    return normalized in BREV_AUTH_ALIASES


def is_brev_auth_text(text: str | None) -> bool:
    m = re.match(r"^\s*/?brev(?:\s+(.+))?\s*$", text or "", re.IGNORECASE)
    return bool(m and is_brev_auth_args(m.group(1)))


def parse_brev_org_id(args_text: str | None) -> tuple[str | None, str | None]:
    raw = (args_text or "").strip()
    if not raw:
        return None, (
            "Usage: `/brev <org_id>` hoặc `brev <org_id>`, ví dụ "
            "`brev vrdc-maxine`. Dùng `brev auth` để check Brev browser SSO."
        )
    if raw.lower() in {"cancel", "stop", "huy", "hủy", "huỷ"}:
        return "cancel", None
    parts = raw.split()
    if len(parts) != 1:
        return None, "Chỉ truyền một `org_id` thôi, ví dụ `brev vrdc-maxine`."
    org_id = parts[0]
    if not BREV_ORG_ID_RE.fullmatch(org_id):
        return None, (
            "`org_id` chỉ nên gồm chữ/số/dấu `-`, `_`, `.`, "
            "và bắt đầu bằng chữ hoặc số."
        )
    return org_id, None


def parse_brev_plain_command(text: str | None) -> str | None:
    m = re.match(r"^\s*brev(?:\s+(.+))?\s*$", text or "", re.IGNORECASE)
    if not m:
        return None
    return (m.group(1) or "").strip()


def is_brev_cancel_text(text: str | None) -> bool:
    value = (text or "").strip().lower()
    return value in {
        "cancel",
        "stop",
        "huy",
        "hủy",
        "huỷ",
        "brev cancel",
        "/brev cancel",
    }


def brev_submission_marker_seen(text: str) -> bool:
    return BREV_SUBMITTED_RE.search(text or "") is not None


def brev_filled_marker_seen(text: str) -> bool:
    return BREV_FILLED_RE.search(text or "") is not None


def strip_brev_submission_marker(text: str) -> str:
    lines = [
        line for line in (text or "").splitlines()
        if not BREV_SUBMITTED_RE.match(line) and not BREV_FILLED_RE.match(line)
    ]
    return "\n".join(lines).strip() or "Brev flow completed."


async def send_brev_slack_notification(org_id: str) -> str:
    message = f"@brev-credits requested credits for {org_id}"
    prompt = f"""The user explicitly authorized this Slack MCP notification after a successful Brev credits form submission.

Destination: Slack DM to `{BREV_NOTIFY_EMAIL}`.
Message text, exactly:
{message}

Use the registered `maas-slack` MCP server if its send-message tool is
available in this turn. If Slack is exposed only through the Codex Slack app
connector, use that connector. Do not use the agent-me Slack bot token, do not
post in the current agent-me thread, and do not use shell commands.

Send as the signed-in Slack user. Do not add "sent by ChatGPT", "via Codex",
"via agent-me", signatures, metadata, blocks, attachments, prefixes, suffixes,
or any extra text. Do not rewrite the bare `@brev-credits` text.

Return a concise Vietnamese status after the send, including the recipient and
permalink if the connector returns one. If the connector fails, include the
exact failure in one short line.
"""
    answer, _sid = await spawn_codex_app_server(prompt)
    return answer.strip() or "_(no output)_"


async def finalize_brev_outcome(thread_ts: str, org_id: str, answer: str) -> str:
    if brev_filled_marker_seen(answer):
        visible_answer = strip_brev_submission_marker(answer)
        await finish_brev_flow(thread_ts, "filled", visible_answer)
        return visible_answer

    if not brev_submission_marker_seen(answer):
        return answer.strip() or "_(no output)_"

    visible_answer = strip_brev_submission_marker(answer)
    await finish_brev_flow(thread_ts, "submitted", visible_answer)
    try:
        notify_status = await send_brev_slack_notification(org_id)
    except Exception as exc:
        return (
            f"{visible_answer}\n\n"
            "⚠️ Form đã submit, nhưng Slack MCP notification bị lỗi: "
            f"`{str(exc)[:600]}`"
        )
    return f"{visible_answer}\n\n✅ Slack MCP notification: {notify_status}"


async def cmd_brev_start(
    args_text: str = "",
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
    user_id: str | None = None,
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> str:
    org_id, err = parse_brev_org_id(args_text)
    if err:
        return err
    if org_id == "cancel":
        if thread_ts:
            await finish_brev_flow(thread_ts, "cancelled", "cancelled by user")
            await clear_session(thread_ts)
        return "Đã hủy Brev flow trong thread này."
    if not channel or not thread_ts:
        return (
            "Brev flow cần một Slack thread thật để hỏi/đáp từng bước. "
            "Hãy chạy `brev <org_id>` trong DM với bot, hoặc dùng native `/brev <org_id>`."
        )

    await upsert_thread(thread_ts, channel, user_id)
    await clear_session(thread_ts)
    await remember_brev_flow(thread_ts, org_id)

    prompt = f"""Start the Brev credits request flow.

Org id: `{org_id}`
Form URL: {BREV_FORM_URL}

Navigate to the form with `maas-playwright`, inspect the first visible page,
fill the visible fields using the reference/sample values in the system prompt,
advance through the form until either all known fields are filled or a missing
required value/authentication blocks progress, then take a screenshot of the
filled state. Do not click the final submit button.
"""
    answer, new_sid = await spawn_codex(
        prompt,
        progress_cb=progress_cb,
        thread_ts=thread_ts,
        system_prompt=build_brev_system_prompt(org_id, thread_ts),
        extra_configs=BREV_PLAYWRIGHT_APPROVAL_CONFIGS,
    )
    if new_sid:
        await upsert_session(thread_ts, new_sid)
    result = await finalize_brev_outcome(thread_ts, org_id, answer)
    if brev_submission_marker_seen(answer) or brev_filled_marker_seen(answer):
        return result
    return f"{result}\n\n_Reply trong thread này với value tiếp theo; gõ `cancel` để hủy._"


async def cmd_brev_continue(
    thread_ts: str,
    user_response: str,
    *,
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> str:
    flow = await get_active_brev_flow(thread_ts)
    if not flow:
        return "Không có Brev flow active trong thread này. Chạy `brev <org_id>` để bắt đầu."

    if is_brev_cancel_text(user_response):
        await finish_brev_flow(thread_ts, "cancelled", "cancelled by user")
        await clear_session(thread_ts)
        return "Đã hủy Brev flow trong thread này."

    org_id = str(flow["org_id"])
    existing_sid = await get_session_id(thread_ts)
    if not existing_sid:
        await finish_brev_flow(thread_ts, "failed", "missing Codex session")
        return (
            "Brev flow bị mất Codex session nên không thể tiếp tục an toàn. "
            f"Chạy lại `brev {org_id}` để bắt đầu từ đầu."
        )

    prompt = f"""The user replied with the next Brev form input or confirmation:

{user_response}

Continue the same Brev credits request for org id `{org_id}`. Use only the
existing user-provided values from this session plus this latest reply. If the
reply answers a visible field, fill it and move to the next page only when all
required fields on the current page are answered. If more information is
needed, ask one concise Vietnamese question with exact visible field labels and
options. If all known fields are filled, take a screenshot and follow the
`BREV_FILLED` completion contract. Do not submit the form in this stage.
"""
    try:
        answer, new_sid = await spawn_codex(
            prompt,
            resume_session_id=existing_sid,
            progress_cb=progress_cb,
            thread_ts=thread_ts,
            system_prompt=build_brev_system_prompt(org_id, thread_ts),
            extra_configs=BREV_PLAYWRIGHT_APPROVAL_CONFIGS,
        )
    except SessionExpired:
        await clear_session(thread_ts)
        await finish_brev_flow(thread_ts, "failed", "expired Codex session")
        return (
            "Brev flow bị mất Codex session nên không thể tiếp tục an toàn. "
            f"Chạy lại `brev {org_id}` để bắt đầu từ đầu."
        )
    if new_sid:
        await upsert_session(thread_ts, new_sid)
    return await finalize_brev_outcome(thread_ts, org_id, answer)


async def cmd_permissioned_app_server_write(user_request: str) -> str:
    prompt = f"""The Slack user explicitly requested a permissioned connector/MCP write action.

Use the relevant Codex app connector or registered MCP tools directly. Do not
use shell commands, PA, Claude CLI, browser automation, or local files.

Rules:
- This current Slack request is the user's authorization. Do not ask for
  confirmation solely because this is a connector/MCP write.
- Perform only the requested external connector/MCP action.
- For email/Outlook: draft-first unless the user explicitly asks to send.
- For Slack/Teams: never post to the current agent-me thread as the answer;
  only send/post to the separate recipient/channel requested by the user.
- For Google Drive, Jira, GitLab, Confluence, NVBugs, or Calendar: modify only
  the requested target object.
- If the request is missing a required recipient, thread, object, or target,
  ask one concise clarification instead of inventing it.
- Return a concise Vietnamese status with the target and link when available.
- If the connector fails, include the exact failure in one short line.

Current user request: {user_request}
"""
    answer, _sid = await spawn_codex_app_server(prompt)
    return answer.strip() or "_(no output)_"


async def cmd_outlook_app_server_write(user_request: str) -> str:
    return await cmd_permissioned_app_server_write(user_request)


async def cmd_reset(thread_ts: str | None) -> str:
    """Drop the Codex session for this thread so the next message
    starts a fresh conversation. The audit `messages` table is left
    intact — only the session pointer is cleared."""
    if not thread_ts:
        return (
            "⚠️ I don't know which thread you mean — DM me from inside a "
            "thread or use this from a real conversation."
        )
    old = await clear_session(thread_ts)
    if not old:
        return (
            "Info: This thread has no active Codex session yet — your next "
            "message starts a new one automatically."
        )
    return (
        f"🧹 *Cleared session* `{old[:8]}…` for this thread.\n"
        "Your next message starts a fresh conversation (no prior context)."
    )


async def cmd_reauth() -> str:
    """Trigger the Codex MCP reauth helper as a detached background process."""
    reauth_log = STATE_DIR / "codex-reauth.log"
    with reauth_log.open("ab") as out:
        await asyncio.create_subprocess_exec(
            _UV_BIN, "run", "agent-me-codex-reauth",
            cwd=str(REPO_DIR),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
    return (
        "🚀 *Codex MCP re-auth helper started* on the bridge host.\n\n"
        "It will refresh the MaaS token store used by Codex bearer-token MCPs "
        "and print/open auth URLs where possible. Output is written to "
        f"`{reauth_log}`. When done, run `/mcp` here to verify."
    )


def _brev_auth_blocks(text: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
        {"type": "actions", "elements": [
            {"type": "button",
             "text": {"type": "plain_text", "text": "🔐 Check Brev auth"},
             "action_id": "menu_brev_auth", "style": "primary"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "🔄 Check MCP status"},
             "action_id": "menu_mcp_status"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "❓ Help"},
             "action_id": "menu_help"},
        ]},
    ]


async def cmd_brev_auth(
    *,
    thread_ts: str | None = None,
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[str, list[dict]]:
    """Check the persistent Brev browser profile without collecting secrets."""
    BREV_PLAYWRIGHT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    BREV_PLAYWRIGHT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    prompt = f"""Check Brev browser authentication state.

Form URL: {BREV_FORM_URL}
Persistent browser profile on this host:
{BREV_PLAYWRIGHT_PROFILE_DIR}
Playwright output directory:
{BREV_PLAYWRIGHT_OUTPUT_DIR}

Navigate to the form with `maas-playwright`, wait for the first stable page,
and inspect whether the Brev form is visible or the browser is blocked by
Microsoft/NVIDIA SSO. Do not enter credentials, MFA, passwords, or submit any
form.

If the Brev form is visible, take a screenshot named
`brev-auth-ok-{thread_ts or "manual"}.png`.
If a login wall is visible, take a screenshot named
`brev-auth-needed-{thread_ts or "manual"}.png`.

Return a concise Vietnamese status. Mention that Brev web SSO has no localhost
callback issue here; the session is stored in the host profile above.
"""
    answer, _sid = await spawn_codex(
        prompt,
        progress_cb=progress_cb,
        thread_ts=thread_ts,
        system_prompt=BREV_AUTH_SYSTEM_PROMPT,
        extra_configs=BREV_PLAYWRIGHT_APPROVAL_CONFIGS,
    )
    body = (
        (answer.strip() or "_(no output)_")
        + "\n\n"
        "Profile host đang dùng: "
        f"`{BREV_PLAYWRIGHT_PROFILE_DIR}`\n"
        "Nếu vẫn thấy SSO, hãy login bằng browser profile này trên host rồi bấm "
        "*Check Brev auth* lại. Không paste password/OTP vào Slack."
    )
    return body, _brev_auth_blocks(body)


SlashResult = tuple[str, list[dict] | None]


def _help_blocks() -> list[dict]:
    """Render `/help` as a section + actions block so the user can click
    instead of typing the command name. Action_ids reuse the menu_*
    handlers wired for the morning routine."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": HELP_TEXT}},
        {"type": "actions", "elements": [
            {"type": "button",
             "text": {"type": "plain_text", "text": "📅 Daily brief"},
             "action_id": "menu_brief_day", "style": "primary"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "📊 Weekly recap"},
             "action_id": "menu_brief_week"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "📆 Monthly"},
             "action_id": "menu_brief_month"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "🔄 Check MCP status"},
             "action_id": "menu_mcp_status"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "🔧 Reauth MCPs"},
             "action_id": "brief_reauth"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "🔐 Brev auth"},
             "action_id": "menu_brev_auth"},
        ]},
    ]


async def handle_slash(
    cmd: str,
    user_id: str | None,
    args_text: str = "",
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> str | SlashResult:
    if cmd == "/mcp":
        return await cmd_mcp()
    if cmd == "/version":
        return await cmd_version()
    if cmd == "/whoami":
        return await cmd_whoami(user_id)
    if cmd == "/reauth":
        return await cmd_reauth()
    if cmd == "/brief":
        return await cmd_brief(args_text, channel=channel, thread_ts=thread_ts)
    if cmd == "/brev":
        if is_brev_auth_args(args_text):
            return await cmd_brev_auth(thread_ts=thread_ts, progress_cb=progress_cb)
        return await cmd_brev_start(
            args_text,
            channel=channel,
            thread_ts=thread_ts,
            user_id=user_id,
            progress_cb=progress_cb,
        )
    if cmd == "/model-free-draft":
        return await cmd_model_free_draft(
            thread_ts=thread_ts,
            subject_pattern=model_free_subject_pattern_from_text(args_text),
            user_request=args_text or "/model-free-draft",
        )
    if cmd == "/reset":
        return await cmd_reset(thread_ts)
    if cmd == "/help":
        return HELP_TEXT, _help_blocks()
    return f"Unknown command `{cmd}`. Try `/help`."


def _split_result(result: str | SlashResult) -> tuple[str, list[dict] | None]:
    if isinstance(result, tuple):
        return result[0], result[1]
    return result, None


# ── Event handlers ──────────────────────────────────────────────────────

async def recover_model_free_subject_from_slack_thread(
    client, channel: str, thread_ts: str,
) -> str | None:
    """Recover the requested Model Free subject for threads created pre-migration."""
    try:
        res = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=40,
        )
    except Exception as exc:
        log.warning("model_free_slack_history_recovery_failed",
                    err=str(exc), thread_ts=thread_ts)
        return None

    messages = res.get("messages") or []
    for msg in reversed(messages):
        subject = model_free_subject_pattern_in_text(msg.get("text"))
        if subject:
            return subject
    return None


async def post_thinking(client, channel: str, thread_ts: str) -> str:
    res = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="🔄 thinking…")
    return res["ts"]


async def update_progress(client, channel: str, ts: str, text: str) -> None:
    await client.chat_update(channel=channel, ts=ts, text=truncate_for_slack(text))


def make_slack_progress_callback(
    client, channel: str, placeholder_ts: str,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Create a throttled Codex progress callback for one Slack placeholder."""
    progress_state: dict[str, float] = {"last": 0.0}
    progress_lock = asyncio.Lock()

    async def progress_cb(state: dict[str, Any]) -> None:
        now = time.monotonic()
        is_final = state.get("final_text") is not None or state.get("is_error")
        async with progress_lock:
            if not is_final and now - progress_state["last"] < PROGRESS_UPDATE_MIN_INTERVAL_S:
                return
            progress_state["last"] = now
            if is_final:
                return  # final reply replaces the placeholder.
            text = format_progress(state)
        try:
            await client.chat_update(
                channel=channel, ts=placeholder_ts, text=text,
            )
        except Exception as exc:
            log.warning("progress_update_failed", err=str(exc))

    return progress_cb


# Track auto-discovered operator user_id when SLACK_ALLOWED_USER_ID is unset.
_operator_user_id: str | None = os.environ.get("SLACK_ALLOWED_USER_ID") or None


# Plain-text command shortcuts: typing "brief", "brief week", "mcp",
# "reauth", "help", etc. in a DM is treated as the corresponding slash
# command. Match is exact (after lower() + strip()) so it doesn't
# accidentally fire on sentences like "help me debug this".
PLAIN_COMMANDS: dict[str, tuple[str, str]] = {
    # brief variants
    "brief":          ("/brief", ""),
    "brief day":      ("/brief", "day"),
    "brief daily":    ("/brief", "daily"),
    "daily":          ("/brief", ""),
    "today":          ("/brief", ""),
    "brief week":     ("/brief", "week"),
    "brief weekly":   ("/brief", "week"),
    "weekly":         ("/brief", "week"),
    "this week":      ("/brief", "week"),
    "brief month":    ("/brief", "month"),
    "brief monthly":  ("/brief", "month"),
    "monthly":        ("/brief", "month"),
    "this month":     ("/brief", "month"),
    # outlook automation shortcuts
    "model free draft":       ("/model-free-draft", ""),
    "draft model free":       ("/model-free-draft", ""),
    "model free 2.0 draft":   ("/model-free-draft", ""),
    "draft model free 2.0":   ("/model-free-draft", ""),
    # other commands
    "mcp":            ("/mcp", ""),
    "status":         ("/mcp", ""),
    "reauth":         ("/reauth", ""),
    "auth":           ("/reauth", ""),
    "help":           ("/help", ""),
    "?":              ("/help", ""),
    "commands":       ("/help", ""),
    "version":        ("/version", ""),
    "ver":            ("/version", ""),
    "whoami":         ("/whoami", ""),
    "who am i":       ("/whoami", ""),
    "id":             ("/whoami", ""),
    # session reset
    "reset":          ("/reset", ""),
    "clear":          ("/reset", ""),
    "new":            ("/reset", ""),
    "new chat":       ("/reset", ""),
    "forget":         ("/reset", ""),
}


async def handle_user_query(*, client, channel: str, thread_ts: str,
                            user_id: str | None, text: str | None,
                            event_ts: str | None) -> None:
    cleaned = strip_bot_mention(text)
    if not cleaned:
        log.debug("empty text after mention-strip", thread_ts=thread_ts)
        return

    log.info("message_received", thread_ts=thread_ts, channel=channel, user=user_id,
             prompt=clip(cleaned))

    async def _dispatch(cmd: str, args_text: str, label: str) -> None:
        placeholder_ts = await post_thinking(client, channel, thread_ts)
        progress_cb = make_slack_progress_callback(client, channel, placeholder_ts)
        try:
            result = await handle_slash(cmd, user_id, args_text,
                                         channel=channel, thread_ts=thread_ts,
                                         progress_cb=progress_cb)
            text, blocks = _split_result(result)
            if blocks:
                # chat.update accepts blocks; include text as fallback for
                # notifications and a11y.
                await client.chat_update(
                    channel=channel, ts=placeholder_ts,
                    text=text or "(see blocks)", blocks=blocks,
                )
            else:
                await update_progress(client, channel, placeholder_ts, text)
            log.info(f"{label}_handled", cmd=cmd, thread_ts=thread_ts, args=args_text)
        except Exception as exc:
            log.error(f"{label}_failed", cmd=cmd, err=str(exc))
            await update_progress(client, channel, placeholder_ts,
                                  f"⚠️ `{cmd}` failed: `{exc}`")

    # Allow Brev browser auth checks even inside an active Brev form thread.
    plain_key = cleaned.strip().lower()
    if is_brev_auth_text(cleaned):
        await _dispatch("/brev", "auth", "plain")
        return

    active_brev = await get_active_brev_flow(thread_ts)
    if active_brev:
        await upsert_thread(thread_ts, channel, user_id)
        await insert_message(thread_ts, "user", cleaned, event_ts)
        placeholder_ts = await post_thinking(client, channel, thread_ts)
        progress_cb = make_slack_progress_callback(client, channel, placeholder_ts)
        try:
            result = await cmd_brev_continue(
                thread_ts,
                cleaned,
                progress_cb=progress_cb,
            )
            await update_progress(client, channel, placeholder_ts, result)
            await insert_message(thread_ts, "assistant", result, placeholder_ts)
            log.info(
                "brev_flow_continued",
                thread_ts=thread_ts,
                org_id=active_brev.get("org_id"),
            )
        except Exception as exc:
            log.error("brev_flow_failed", err=str(exc), thread_ts=thread_ts)
            await update_progress(
                client, channel, placeholder_ts,
                f"⚠️ Brev flow failed: `{str(exc)[:600]}`",
            )
        return

    # Plain-text command intercept (exact match): "brief", "brief week", "mcp", etc.
    brev_args = parse_brev_plain_command(cleaned)
    if brev_args is not None:
        await _dispatch("/brev", brev_args, "plain")
        return
    if plain_key in PLAIN_COMMANDS:
        cmd, args_text = PLAIN_COMMANDS[plain_key]
        await _dispatch(cmd, args_text, "plain")
        return

    # Slash-prefix intercept: route /mcp etc. without spawning the agent.
    m = re.match(r"^(/[a-z][a-z0-9_-]*)\b\s*(.*)$", cleaned, re.IGNORECASE)
    if m:
        cmd = m.group(1)
        args_text = m.group(2)
        await _dispatch(cmd, args_text, "slash")
        return

    model_free_subject = None
    if looks_like_model_free_email_request(cleaned):
        model_free_subject = model_free_subject_pattern_from_text(cleaned)
    elif looks_like_model_free_followup_request(cleaned):
        model_free_subject = await get_model_free_thread_subject(thread_ts)
        if not model_free_subject:
            model_free_subject = await get_model_free_thread_subject_from_messages(thread_ts)
        if not model_free_subject:
            model_free_subject = await recover_model_free_subject_from_slack_thread(
                client, channel, thread_ts,
            )

    if model_free_subject:
        await upsert_thread(thread_ts, channel, user_id)
        await insert_message(thread_ts, "user", cleaned, event_ts)
        await remember_model_free_thread(thread_ts, model_free_subject, cleaned)
        placeholder_ts = await post_thinking(client, channel, thread_ts)
        progress_cb = make_slack_progress_callback(client, channel, placeholder_ts)
        try:
            result = await cmd_model_free_draft(
                thread_ts=thread_ts,
                subject_pattern=model_free_subject,
                user_request=cleaned,
                progress_cb=progress_cb,
            )
            await update_progress(client, channel, placeholder_ts, result)
            await insert_message(thread_ts, "assistant", result, placeholder_ts)
            log.info(
                "model_free_email_handled",
                thread_ts=thread_ts,
                subject_pattern=model_free_subject,
            )
        except Exception as exc:
            log.error("model_free_email_failed", err=str(exc), thread_ts=thread_ts)
            await update_progress(
                client, channel, placeholder_ts,
                f"⚠️ Model Free draft check failed: `{str(exc)[:600]}`",
            )
        return

    if looks_like_nvbugs_read_request(cleaned):
        await upsert_thread(thread_ts, channel, user_id)
        await insert_message(thread_ts, "user", cleaned, event_ts)
        placeholder_ts = await post_thinking(client, channel, thread_ts)
        try:
            result = await cmd_nvbugs_read()
            await update_progress(client, channel, placeholder_ts, result)
            await insert_message(thread_ts, "assistant", result, placeholder_ts)
            log.info("nvbugs_read_handled", thread_ts=thread_ts)
        except Exception as exc:
            log.error("nvbugs_read_failed", err=str(exc), thread_ts=thread_ts)
            await update_progress(
                client, channel, placeholder_ts,
                f"⚠️ NVBugs fetch failed: `{str(exc)[:600]}`",
            )
        return

    if looks_like_permissioned_connector_write_request(cleaned):
        await upsert_thread(thread_ts, channel, user_id)
        await insert_message(thread_ts, "user", cleaned, event_ts)
        placeholder_ts = await post_thinking(client, channel, thread_ts)
        try:
            await update_progress(
                client,
                channel,
                placeholder_ts,
                "🔄 routing connector/MCP write through Codex app-server auto-review…",
            )
            result = await cmd_permissioned_app_server_write(cleaned)
            await update_progress(client, channel, placeholder_ts, result)
            await insert_message(thread_ts, "assistant", result, placeholder_ts)
            log.info("permissioned_app_server_write_handled", thread_ts=thread_ts)
        except Exception as exc:
            log.error("permissioned_app_server_write_failed",
                      err=str(exc), thread_ts=thread_ts)
            await update_progress(
                client, channel, placeholder_ts,
                f"⚠️ Connector/MCP write failed: `{str(exc)[:600]}`",
            )
        return

    await upsert_thread(thread_ts, channel, user_id)
    await insert_message(thread_ts, "user", cleaned, event_ts)

    try:
        placeholder_ts = await post_thinking(client, channel, thread_ts)
    except Exception as exc:
        log.error("post_thinking_failed", err=str(exc), thread_ts=thread_ts)
        return

    # Anchor reset: resumed sessions can over-weight prior tool failures.
    # The bridge starts a fresh Codex exec process on every turn and app/MCP
    # availability may have changed, so force a current-turn retry before the
    # agent claims a source is unavailable.
    cleaned_with_reset = (
        "[bridge note — TOOL STATE FOR THIS TURN: Codex app plugins "
        "(Teams, Slack, Outlook, Google Drive, GitHub) and any registered "
        "Codex MCP servers are initialized for this turn. Runtime identity is "
        "Codex, not Claude. Disregard any "
        "earlier-turn belief that tools are disconnected or unavailable; "
        "try the appropriate app/MCP tool for this question first. Do not "
        "use PA CLI or shell for enterprise-source reads. Permissioned "
        "connector/MCP writes are handled by the app-server auto-review route, "
        "not this read/chat path.]\n\n"
        f"{cleaned}"
    )
    start = time.time()
    progress_cb = make_slack_progress_callback(client, channel, placeholder_ts)

    try:
        # Look up the Codex session for this Slack thread. First message
        # means no session yet; Codex creates one and we save the ID.
        # Subsequent messages resume the same session, which is how
        # context, prompt-cache hits, and tool-use history are preserved.
        existing_sid = await get_session_id(thread_ts)
        try:
            answer, new_sid = await spawn_codex(
                cleaned_with_reset, resume_session_id=existing_sid,
                progress_cb=progress_cb, thread_ts=thread_ts,
            )
        except SessionExpired as exc:
            # The on-disk session went away (Codex was restarted, project
            # path changed, file got cleaned, etc.). Retry without --resume
            # so the user sees the bridge respond gracefully instead of an
            # error message — they lose continuity, that's it.
            log.warning("session_expired_starting_fresh",
                        thread_ts=thread_ts, expired=existing_sid, err=str(exc))
            await clear_session(thread_ts)
            answer, new_sid = await spawn_codex(
                cleaned_with_reset, progress_cb=progress_cb,
                thread_ts=thread_ts,
            )

        if new_sid:
            await upsert_session(thread_ts, new_sid)

        final = answer if answer.strip() else "_(no output)_"
        await post_chunked_reply(
            client, channel=channel, placeholder_ts=placeholder_ts,
            thread_ts=thread_ts, text=final,
        )
        await insert_message(thread_ts, "assistant", final, placeholder_ts)
        log.info("query_handled", thread_ts=thread_ts,
                 ms=int((time.time() - start) * 1000),
                 model=MODEL, session_id=new_sid,
                 resumed=existing_sid is not None,
                 prompt=clip(cleaned), response=clip(final))
    except Exception as exc:
        log.error("query_failed", thread_ts=thread_ts, err=str(exc),
                  ms=int((time.time() - start) * 1000), prompt=clip(cleaned))
        try:
            await update_progress(client, channel, placeholder_ts,
                                  f"⚠️ Error: `{str(exc)[:600]}`")
        except Exception as exc2:
            log.error("error_update_failed", err=str(exc2))


@app.event("message")
async def on_message(event, client):
    global _operator_user_id
    if event.get("subtype"):
        return
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id"):
        return

    user = event.get("user")
    allowed = os.environ.get("SLACK_ALLOWED_USER_ID")
    if allowed and user != allowed:
        log.warning("message_rejected_user", from_=user)
        return
    if not allowed and user and not _operator_user_id:
        _operator_user_id = user
        log.info("auto_discovered_operator", user=user)

    thread_ts = event.get("thread_ts") or event.get("ts")
    await handle_user_query(
        client=client, channel=event["channel"], thread_ts=thread_ts,
        user_id=user, text=event.get("text"), event_ts=event.get("ts"),
    )


@app.event("app_mention")
async def on_app_mention(event, client):
    thread_ts = event.get("thread_ts") or event.get("ts")
    await handle_user_query(
        client=client, channel=event["channel"], thread_ts=thread_ts,
        user_id=event.get("user"), text=event.get("text"), event_ts=event.get("ts"),
    )


# ── Native slash commands ───────────────────────────────────────────────

async def _native_slash(ack, respond, command, cmd_name: str):
    await ack()
    args_text = (command.get("text") or "").strip()
    log.info("native_slash", cmd=cmd_name, user=command.get("user_id"),
             channel=command.get("channel_id"), args=args_text)
    try:
        result = await handle_slash(
            cmd_name,
            command.get("user_id"),
            args_text,
            channel=command.get("channel_id"),
        )
        text, blocks = _split_result(result)
        payload: dict = {"response_type": "in_channel", "text": text or "(see blocks)"}
        if blocks:
            payload["blocks"] = blocks
        await respond(**payload)
    except Exception as exc:
        log.error("native_slash_failed", cmd=cmd_name, err=str(exc))
        await respond(response_type="ephemeral", text=f"⚠️ `{cmd_name}` failed: `{exc}`")


@app.command("/mcp")
async def slash_mcp(ack, respond, command):
    await _native_slash(ack, respond, command, "/mcp")


@app.command("/version")
async def slash_version(ack, respond, command):
    await _native_slash(ack, respond, command, "/version")


@app.command("/whoami")
async def slash_whoami(ack, respond, command):
    await _native_slash(ack, respond, command, "/whoami")


@app.command("/help")
async def slash_help(ack, respond, command):
    await _native_slash(ack, respond, command, "/help")


@app.command("/reauth")
async def slash_reauth(ack, respond, command):
    await _native_slash(ack, respond, command, "/reauth")


@app.command("/brief")
async def slash_brief(ack, respond, command):
    await _native_slash(ack, respond, command, "/brief")


@app.command("/brev")
async def slash_brev(ack, respond, command, client):
    await ack()
    args_text = (command.get("text") or "").strip()
    channel = command.get("channel_id")
    user_id = command.get("user_id")
    log.info("native_slash", cmd="/brev", user=user_id,
             channel=channel, args=args_text)
    if not channel:
        await respond(response_type="ephemeral", text="⚠️ `/brev` missing Slack channel.")
        return
    try:
        intro = (
            "🔐 Checking Brev browser auth profile…"
            if is_brev_auth_args(args_text)
            else "🔄 Starting Brev credits fill/screenshot flow… reply in this thread with each requested value."
        )
        root = await client.chat_postMessage(
            channel=channel,
            text=intro,
        )
        thread_ts = root["ts"]
        progress_cb = make_slack_progress_callback(client, channel, thread_ts)
        if is_brev_auth_args(args_text):
            result = await cmd_brev_auth(thread_ts=thread_ts, progress_cb=progress_cb)
            text, blocks = _split_result(result)
            await client.chat_update(
                channel=channel, ts=thread_ts,
                text=text or "(see blocks)", blocks=blocks or [],
            )
        else:
            result = await cmd_brev_start(
                args_text,
                channel=channel,
                thread_ts=thread_ts,
                user_id=user_id,
                progress_cb=progress_cb,
            )
            await update_progress(client, channel, thread_ts, result)
    except Exception as exc:
        log.error("native_slash_failed", cmd="/brev", err=str(exc))
        await respond(response_type="ephemeral", text=f"⚠️ `/brev` failed: `{exc}`")


@app.command("/model-free-draft")
async def slash_model_free_draft(ack, respond, command):
    await _native_slash(ack, respond, command, "/model-free-draft")


# ── Block Kit button handlers ──────────────────────────────────────────
#
# Slash commands don't always feel native in Slack DMs — interactive
# buttons inside posted messages are a guaranteed-to-work alternative.
# Daily-brief Block Kit ends with [Refresh] [Weekly] [Reauth] buttons;
# the morning warmup message has a [Reauth now] primary button.

async def _post_in_channel(client, body: dict, text: str):
    channel = body.get("channel", {}).get("id") if isinstance(body.get("channel"), dict) else None
    if not channel:
        # Fall back to operator DM if button context lacks channel id.
        channel = await ensure_dm_channel(client)
    if channel:
        await client.chat_postMessage(channel=channel, text=text)


def _button_thread_context(body: dict) -> tuple[str | None, str | None]:
    channel = body.get("channel", {}).get("id") if isinstance(body.get("channel"), dict) else None
    msg = body.get("message") or {}
    return channel, msg.get("thread_ts") or msg.get("ts")


@app.action("brief_refresh")
async def on_brief_refresh(ack, body, client):
    await ack()
    period = (body.get("actions") or [{}])[0].get("value") or "day"
    log.info("button_brief_refresh", period=period, user=body.get("user", {}).get("id"))
    channel, thread_ts = _button_thread_context(body)
    await cmd_brief(period if period != "day" else "", channel=channel, thread_ts=thread_ts)
    await _post_in_channel(client, body, f"📅 Refreshing brief (`{period}`) — back in ~60s.")


@app.action("brief_reauth")
async def on_brief_reauth(ack, body, client):
    await ack()
    log.info("button_brief_reauth", user=body.get("user", {}).get("id"))
    await cmd_reauth()
    await _post_in_channel(client, body,
                           "🔧 Reauth helper started — browser tabs opening on bridge host.")


async def _reply_in_thread(client, body, text: str, blocks: list[dict] | None = None):
    """Reply inside the thread of the message whose button was clicked."""
    channel = (body.get("channel") or {}).get("id")
    msg = body.get("message") or {}
    thread_ts = msg.get("thread_ts") or msg.get("ts")
    if not channel:
        channel = await ensure_dm_channel(client)
    if not channel:
        log.warning("reply_in_thread_no_channel")
        return
    kwargs = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    if blocks:
        kwargs["blocks"] = blocks
    await client.chat_postMessage(**kwargs)


@app.action("morning_reauth")
async def on_morning_reauth(ack, body, client):
    await ack()
    log.info("button_morning_reauth", user=body.get("user", {}).get("id"))
    await cmd_reauth()
    intro = (
        "🔧 *Reauth helper started* — browser tabs opening on bridge host.\n"
        "Sign in to each tab (~30 seconds), then pick an action:"
    )
    await _reply_in_thread(client, body, "Reauth started — pick next action",
                           blocks=_morning_menu_blocks(intro))


@app.action("morning_brief_now")
async def on_morning_brief_now(ack, body, client):
    await ack()
    log.info("button_morning_brief_now", user=body.get("user", {}).get("id"))
    channel, thread_ts = _button_thread_context(body)
    await cmd_brief("", channel=channel, thread_ts=thread_ts)
    await _reply_in_thread(client, body,
                           "📅 Generating today's brief — back in ~60s in this thread.")


# ── Action-menu buttons (posted in morning thread or after reauth) ────

@app.action("menu_brief_day")
async def on_menu_brief_day(ack, body, client):
    await ack()
    log.info("button_menu_brief_day", user=body.get("user", {}).get("id"))
    channel, thread_ts = _button_thread_context(body)
    await cmd_brief("", channel=channel, thread_ts=thread_ts)
    await _reply_in_thread(client, body, "📅 Daily brief generating — back in ~60s.")


@app.action("menu_brief_week")
async def on_menu_brief_week(ack, body, client):
    await ack()
    log.info("button_menu_brief_week", user=body.get("user", {}).get("id"))
    channel, thread_ts = _button_thread_context(body)
    await cmd_brief("week", channel=channel, thread_ts=thread_ts)
    await _reply_in_thread(client, body, "📊 Weekly recap generating — back in ~60s.")


@app.action("menu_brief_month")
async def on_menu_brief_month(ack, body, client):
    await ack()
    log.info("button_menu_brief_month", user=body.get("user", {}).get("id"))
    channel, thread_ts = _button_thread_context(body)
    await cmd_brief("month", channel=channel, thread_ts=thread_ts)
    await _reply_in_thread(client, body, "📆 Monthly recap generating — back in ~60s.")


@app.action("menu_mcp_status")
async def on_menu_mcp_status(ack, body, client):
    await ack()
    log.info("button_menu_mcp_status", user=body.get("user", {}).get("id"))
    try:
        body_text = await cmd_mcp()
    except Exception as exc:
        body_text = f"⚠️ `mcp` failed: `{exc}`"
    await _reply_in_thread(client, body, body_text)


@app.action("menu_brev_auth")
async def on_menu_brev_auth(ack, body, client):
    await ack()
    log.info("button_menu_brev_auth", user=body.get("user", {}).get("id"))
    _channel, thread_ts = _button_thread_context(body)
    try:
        result = await cmd_brev_auth(thread_ts=thread_ts)
        text, blocks = _split_result(result)
    except Exception as exc:
        text, blocks = f"⚠️ Brev auth check failed: `{str(exc)[:600]}`", None
    await _reply_in_thread(client, body, text, blocks=blocks)


@app.action("menu_help")
async def on_menu_help(ack, body, client):
    await ack()
    log.info("button_menu_help", user=body.get("user", {}).get("id"))
    await _reply_in_thread(client, body, HELP_TEXT)


# ── Periodic MCP-auth health check ──────────────────────────────────────

MCP_CHECK_INTERVAL_S = float(os.environ.get("MCP_CHECK_INTERVAL_S", 6 * 60 * 60))
MIN_NOTIFY_GAP_S = float(os.environ.get("MIN_NOTIFY_GAP_S", 4 * 60 * 60))

_dm_channel_id: str | None = None
_last_notify_ts: float = 0.0
_last_need_auth_set: str = ""


async def ensure_dm_channel(client) -> str | None:
    global _dm_channel_id
    if _dm_channel_id:
        return _dm_channel_id
    uid = os.environ.get("SLACK_ALLOWED_USER_ID") or _operator_user_id
    if not uid:
        return None
    try:
        res = await client.conversations_open(users=uid)
        _dm_channel_id = res["channel"]["id"]
        log.info("dm_channel_opened", dm_channel_id=_dm_channel_id, uid=uid)
    except Exception as exc:
        log.warning("dm_channel_failed", err=str(exc))
    return _dm_channel_id


async def check_mcp_auth(client) -> None:
    global _last_notify_ts, _last_need_auth_set
    try:
        out = await run_command([CODEX_BIN, "mcp", "list"], cwd=str(REPO_DIR), timeout=60.0)
    except Exception as exc:
        log.warning("mcp_list_failed", err=str(exc))
        return
    need_auth = sorted(
        line.split(":")[0].strip()
        for line in out.splitlines()
        if "Needs authentication" in line
    )
    missing_tokens = _missing_codex_mcp_token_envs(out)
    need_auth = sorted(set(need_auth) | set(missing_tokens))
    log.info(
        "mcp_health_check",
        need_auth_count=len(need_auth),
        servers=need_auth,
        missing_bearer_tokens=missing_tokens,
    )
    if not need_auth:
        _last_need_auth_set = ""
        return
    set_key = ",".join(need_auth)
    now = time.time()
    if set_key == _last_need_auth_set and now - _last_notify_ts < MIN_NOTIFY_GAP_S:
        log.debug("notify_skipped_recent")
        return
    dm = await ensure_dm_channel(client)
    if not dm:
        log.warning("no_dm_channel — set SLACK_ALLOWED_USER_ID or DM the bot once")
        return
    text = "\n".join((
        f"🔔 *{len(need_auth)} MCP server(s) need re-auth:* "
        + ", ".join(f"`{s}`" for s in need_auth),
        "",
        "Run `/reauth` here, or on the bridge host:",
        "```",
        "uv run agent-me-codex-reauth",
        "```",
        "Bridge picks up new tokens on the next call — no restart needed.",
    ))
    try:
        await client.chat_postMessage(channel=dm, text=text)
        _last_notify_ts = now
        _last_need_auth_set = set_key
        log.info("mcp_auth_notified", count=len(need_auth))
    except Exception as exc:
        log.error("mcp_auth_notify_failed", err=str(exc))


# ── Scheduled morning routine (6am Vietnam time by default) ─────────────

MORNING_TZ = ZoneInfo(os.environ.get("BRIEF_TIMEZONE", "Asia/Ho_Chi_Minh"))
MORNING_HOUR = int(os.environ.get("BRIEF_HOUR", 6))
MORNING_MINUTE = int(os.environ.get("BRIEF_MINUTE", 0))


def _seconds_until_next_morning() -> float:
    now = datetime.now(MORNING_TZ)
    target = now.replace(hour=MORNING_HOUR, minute=MORNING_MINUTE, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


def _morning_menu_blocks(intro_text: str) -> list[dict]:
    """Standard "what next?" menu — posted in-thread after reauth or as
    the daily starter when MCPs are healthy. All buttons reply in the
    same thread so the morning's conversation stays organized."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": intro_text}},
        {"type": "actions", "elements": [
            {"type": "button",
             "text": {"type": "plain_text", "text": "📅 Daily brief"},
             "action_id": "menu_brief_day", "style": "primary"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "📊 Weekly recap"},
             "action_id": "menu_brief_week"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "📆 Monthly"},
             "action_id": "menu_brief_month"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "🔄 Verify MCPs"},
             "action_id": "menu_mcp_status"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "🔐 Brev auth"},
             "action_id": "menu_brev_auth"},
            {"type": "button",
             "text": {"type": "plain_text", "text": "❓ Help"},
             "action_id": "menu_help"},
        ]},
    ]


async def run_morning_routine(client) -> None:
    """Daily morning conversation in DM.

    Flow:
      1. Post a fresh date-headed STARTER message (becomes thread root).
      2. Run `codex mcp list` to check MCP auth state.
      3. Reply in thread:
         - If anything stale → reauth prompt with [🔧 Reauth now] +
           [📅 Brief anyway] buttons.
         - If healthy → action menu (Daily / Weekly / Monthly / MCP / Help).
      4. After user clicks Reauth, the action handler replies in the
         same thread with the action menu so they can pick what's next.
    """
    log.info("morning_routine_running")
    dm = await ensure_dm_channel(client)
    if not dm:
        log.warning("morning_no_dm_channel — set SLACK_ALLOWED_USER_ID")
        return

    # Step 1: Starter message with date header — this becomes the day's thread root.
    today = datetime.now(MORNING_TZ).strftime("%A · %Y-%m-%d")
    starter = await client.chat_postMessage(
        channel=dm,
        text=f"☀️ Good morning — {today}",
        blocks=[
            {"type": "header",
             "text": {"type": "plain_text", "text": f"☀️ Good morning — {today}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn",
                 "text": "_New session for today. Running MCP health check first…_"},
            ]},
        ],
    )
    starter_ts = starter["ts"]
    log.info("morning_starter_posted", ts=starter_ts)

    # Step 2: MCP probe.
    try:
        out = await run_command([CODEX_BIN, "mcp", "list"], cwd=str(REPO_DIR), timeout=60.0)
        need_auth = sorted(
            line.split(":")[0].strip()
            for line in out.splitlines()
            if "Needs authentication" in line
        )
    except Exception as exc:
        log.error("morning_mcp_check_failed", err=str(exc))
        need_auth = ["(probe failed — see bridge.log)"]

    # Step 3: Reply in thread with status + actions.
    if need_auth:
        text = (
            f"*🔧 {len(need_auth)} MCP server(s) need re-auth* before today's data is fresh:\n"
            + ", ".join(f"`{s}`" for s in need_auth)
            + "\n\nClick *Reauth now* and sign in to each browser tab. After that, you'll see action options."
        )
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [
                {"type": "button",
                 "text": {"type": "plain_text", "text": "🔧 Reauth now"},
                 "action_id": "morning_reauth", "style": "primary"},
                {"type": "button",
                 "text": {"type": "plain_text", "text": "📅 Brief anyway"},
                 "action_id": "morning_brief_now"},
            ]},
        ]
        await client.chat_postMessage(
            channel=dm, thread_ts=starter_ts,
            text=f"{len(need_auth)} MCP(s) need re-auth",
            blocks=blocks,
        )
        log.info("morning_warned", need_auth_count=len(need_auth), servers=need_auth)
        return

    # All MCPs healthy → action menu.
    intro = (
        "✅ *Codex MCPs connected.*\n\n"
        "What would you like to do?"
    )
    await client.chat_postMessage(
        channel=dm, thread_ts=starter_ts,
        text="All MCPs healthy — pick an action",
        blocks=_morning_menu_blocks(intro),
    )
    log.info("morning_menu_posted")


async def morning_loop(client) -> None:
    while True:
        sleep_s = _seconds_until_next_morning()
        next_run = (datetime.now(MORNING_TZ) + timedelta(seconds=sleep_s)).isoformat()
        log.info("morning_sleep", sleep_seconds=int(sleep_s), next_run_local=next_run,
                 tz=str(MORNING_TZ), hour=MORNING_HOUR, minute=MORNING_MINUTE)
        try:
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            return
        try:
            await run_morning_routine(client)
        except Exception as exc:
            log.error("morning_routine_failed", err=str(exc))


# ── Phase 2b approval gate ──────────────────────────────────────────────
#
# Claude Code's PreToolUse hook runs `slack-approval.sh` (bootstrapped
# at startup into CHAT_CWD/.claude/) for any write tool call. The hook
# writes a request JSON to `${STATE_DIR}/approvals/requests/` and polls
# `${STATE_DIR}/approvals/decisions/` for the bridge's reply.
#
# This loop scans the requests/ dir, posts an Approve/Reject DM to the
# operator, and inserts a `pending_approvals` row. The Slack action
# handlers below write the decision file when the user clicks.
#
# Why polling not inotify: cross-platform, no extra deps, ~1s latency
# is invisible against a human round-trip. If we ever need sub-second
# latency the loop signature is ready for an asyncio.Queue swap-in.


async def _post_approval_request(client, req: approvals.ApprovalRequest) -> None:
    """Insert a DB row + post the Approve/Reject message to operator DM."""
    # Fast path: per-thread auto-approve. We learn the thread_ts in two
    # ways: (a) the hook stamps `agent_me_thread_ts` on the request when
    # the bridge passes `AGENT_ME_THREAD_TS` via env (works on every
    # turn, including the first); (b) `claude_sessions` maps session_id
    # → thread_ts (only available from the second turn onward, because
    # the first turn's session row is written after the spawn returns).
    thread_ts: str | None = req.thread_ts
    if thread_ts is None and req.session_id:
        async with DB_LOCK:
            row = db.execute(
                "SELECT thread_ts FROM claude_sessions WHERE session_id = ?",
                (req.session_id,),
            ).fetchone()
        if row:
            thread_ts = row[0]
    if thread_ts and approvals.thread_auto_approve(db, thread_ts):
        approvals.write_decision(
            state_dir=STATE_DIR,
            tool_use_id=req.tool_use_id,
            decision="allow",
            reason="auto-approved (per-thread toggle)",
        )
        approval_id = uuid.uuid4().hex[:12]
        async with DB_LOCK:
            approvals.insert_pending(
                db,
                approval_id=approval_id,
                thread_ts=thread_ts,
                tool_use_id=req.tool_use_id,
                tool_name=req.tool_name,
                tool_input_json=json.dumps(req.tool_input,
                                           ensure_ascii=False),
                session_id=req.session_id,
                slack_channel=None,
                slack_message_ts=None,
            )
            approvals.resolve(
                db, approval_id=approval_id, status="approved",
                decision_reason="auto-approved (per-thread toggle)",
                auto=True,
            )
        approvals.archive_request(STATE_DIR, req.tool_use_id, "approved")
        log.info("approval_auto_allowed",
                 tool=req.tool_name, tool_use_id=req.tool_use_id,
                 thread_ts=thread_ts)
        return

    # Slow path: ask the human.
    dm = await ensure_dm_channel(client)
    if not dm:
        log.warning("approval_no_dm — denying by default",
                    tool_use_id=req.tool_use_id)
        approvals.write_decision(
            state_dir=STATE_DIR,
            tool_use_id=req.tool_use_id,
            decision="deny",
            reason="bridge: no DM channel configured",
        )
        approvals.archive_request(STATE_DIR, req.tool_use_id, "rejected")
        return

    approval_id = uuid.uuid4().hex[:12]
    fallback_text, blocks = approvals.format_request_for_slack(req)
    posted_ts: str | None = None
    try:
        res = await client.chat_postMessage(
            channel=dm,
            text=f"⚠️ Tool wants approval: {fallback_text}",
            blocks=blocks,
        )
        posted_ts = res.get("ts") if isinstance(res, dict) else res["ts"]
    except Exception as exc:
        log.error("approval_post_failed", tool_use_id=req.tool_use_id, err=str(exc))
        approvals.write_decision(
            state_dir=STATE_DIR,
            tool_use_id=req.tool_use_id,
            decision="deny",
            reason=f"bridge: failed to post Slack message ({exc!s:.80})",
        )
        approvals.archive_request(STATE_DIR, req.tool_use_id, "rejected")
        return

    async with DB_LOCK:
        approvals.insert_pending(
            db,
            approval_id=approval_id,
            thread_ts=thread_ts or "",
            tool_use_id=req.tool_use_id,
            tool_name=req.tool_name,
            tool_input_json=json.dumps(req.tool_input, ensure_ascii=False),
            session_id=req.session_id,
            slack_channel=dm,
            slack_message_ts=posted_ts,
        )
    log.info("approval_posted",
             tool=req.tool_name,
             tool_use_id=req.tool_use_id,
             approval_id=approval_id,
             slack_ts=posted_ts)


async def _resolve_approval_from_button(
    *,
    client,
    body: dict,
    decision: str,
    reason: str,
    auto: bool = False,
) -> None:
    """Common path for Approve / Reject button handlers.

    Looks the row up by tool_use_id (carried in button `value`), writes
    the decision file, marks the DB row, edits the original Slack
    message to disable the buttons + show outcome.
    """
    actions = body.get("actions") or []
    tool_use_id = (actions[0].get("value") if actions else "") or ""
    if not tool_use_id:
        log.warning("approval_button_missing_tool_use_id", body_keys=list(body.keys()))
        return

    async with DB_LOCK:
        row = approvals.get_by_tool_use_id(db, tool_use_id)

    if not row:
        log.warning("approval_button_no_row", tool_use_id=tool_use_id)
        return
    if row["status"] != "pending":
        log.info("approval_button_already_resolved",
                 tool_use_id=tool_use_id, status=row["status"])
        return

    # Write decision FIRST. The hook is polling and may pick it up
    # before we finish updating Slack — that's fine, we want the tool
    # call unblocked ASAP.
    status_word = "approved" if decision == "allow" else "rejected"
    approvals.write_decision(
        state_dir=STATE_DIR, tool_use_id=tool_use_id,
        decision=decision, reason=reason,
    )
    approvals.archive_request(STATE_DIR, tool_use_id, status_word)

    async with DB_LOCK:
        approvals.resolve(
            db, approval_id=row["id"], status=status_word,
            decision_reason=reason, auto=auto,
        )

    user_id = (body.get("user") or {}).get("id")
    log.info("approval_resolved",
             approval_id=row["id"],
             tool_use_id=tool_use_id,
             status=status_word,
             auto=auto,
             user=user_id)

    # Update the original Slack message: strip buttons, show outcome.
    channel = (body.get("channel") or {}).get("id") or row.get("slack_channel")
    msg_ts = (body.get("message") or {}).get("ts") or row.get("slack_message_ts")
    if not channel or not msg_ts:
        return
    icon = {"approved": "✅", "rejected": "❌"}[status_word]
    auto_note = " · auto-thread" if auto else ""
    decided_text = (
        f"{icon} *{status_word.title()}* — {row.get('tool_name', '?')}"
        f" · `{tool_use_id[:12]}`{auto_note}"
    )
    try:
        await client.chat_update(
            channel=channel, ts=msg_ts,
            text=decided_text,
            blocks=[
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": decided_text}},
                {"type": "context",
                 "elements": [{"type": "mrkdwn",
                               "text": f"_resolved by <@{user_id or '?'}>_"}]},
            ],
        )
    except Exception as exc:
        log.warning("approval_chat_update_failed",
                    tool_use_id=tool_use_id, err=str(exc))


@app.action("approval_approve")
async def on_approval_approve(ack, body, client):
    await ack()
    await _resolve_approval_from_button(
        client=client, body=body,
        decision="allow",
        reason="approved via Slack",
    )


@app.action("approval_reject")
async def on_approval_reject(ack, body, client):
    await ack()
    await _resolve_approval_from_button(
        client=client, body=body,
        decision="deny",
        reason="rejected via Slack",
    )


@app.action("approval_auto_thread")
async def on_approval_auto_thread(ack, body, client):
    """Approve THIS request and turn on auto-approve for the rest of the thread."""
    await ack()
    actions = body.get("actions") or []
    tool_use_id = (actions[0].get("value") if actions else "") or ""
    async with DB_LOCK:
        row = approvals.get_by_tool_use_id(db, tool_use_id)
    if row and row.get("thread_ts"):
        async with DB_LOCK:
            approvals.set_thread_auto_approve(db, row["thread_ts"], True)
        log.info("approval_auto_thread_on", thread_ts=row["thread_ts"])
    await _resolve_approval_from_button(
        client=client, body=body,
        decision="allow",
        reason="approved + thread set auto-approve",
        auto=True,
    )


# ── Boot ────────────────────────────────────────────────────────────────

async def main_async() -> int:
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    log.info("bridge_starting", agent="codex",
             approval_gate=APPROVAL_GATE_ON,
             model=MODEL, mcp_check_interval_s=MCP_CHECK_INTERVAL_S)

    # Legacy Claude approval bootstrap. Codex is the default backend, so this
    # only runs if an operator explicitly enables the old hook path.
    if APPROVAL_GATE_ON:
        try:
            approvals.bootstrap_hooks(chat_cwd=CHAT_CWD, state_dir=STATE_DIR)
        except Exception as exc:
            log.error("approval_bootstrap_failed", err=str(exc))

    async def health_loop():
        while True:
            try:
                await check_mcp_auth(app.client)
            except Exception as exc:
                log.warning("health_loop_iter_failed", err=str(exc))
            await asyncio.sleep(MCP_CHECK_INTERVAL_S)

    async def approval_dispatch(req: approvals.ApprovalRequest) -> None:
        await _post_approval_request(app.client, req)

    health_task = asyncio.create_task(health_loop())
    morning_task = asyncio.create_task(morning_loop(app.client))
    approval_task: asyncio.Task[None] | None = None
    if APPROVAL_GATE_ON:
        approval_task = asyncio.create_task(
            approvals.approval_loop(
                db=db, state_dir=STATE_DIR,
                on_request=approval_dispatch,
                poll_interval_s=1.0,
            )
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _sig(signame: str):
        log.info("shutdown_signal_received", signal=signame)
        stop.set()
        # Hard-exit fallback if graceful shutdown hangs (e.g. socket close stuck).
        loop.call_later(8.0, lambda: (log.warning("force_exit_after_timeout"), os._exit(1)))

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _sig, sig.name)

    try:
        await handler.start_async()
        log.info("bridge_running")
        await stop.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("shutdown_via_exception")
    finally:
        log.info("bridge_stopping")
        health_task.cancel()
        morning_task.cancel()
        bg_tasks: list[asyncio.Task[Any]] = [health_task, morning_task]
        if approval_task is not None:
            approval_task.cancel()
            bg_tasks.append(approval_task)
        for t in bg_tasks:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=2.0)
        try:
            await asyncio.wait_for(handler.close_async(), timeout=4.0)
        except (TimeoutError, Exception) as exc:
            log.warning("handler_close_timed_out_or_failed", err=str(exc))
        with contextlib.suppress(Exception):
            db.close()
        log.info("bridge_stopped")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
