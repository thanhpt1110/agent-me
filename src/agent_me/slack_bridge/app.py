"""agent-me Slack bridge — Python port (replaces services/slack-bridge/).

Run with:
    uv run agent-me-bridge

What this owns:
- Loading .env from ${AGENT_ME_REPO_DIR}/configs/.env
- AsyncApp + Socket Mode connection to Slack
- SQLite state DB (threads, messages, pending_approvals — schema in db/)
- Event handlers: DM messages and channel @mentions
- Native slash commands: /mcp /version /whoami /help /reauth
- Text-prefix slash commands (same set, intercepted from message body)
- Spawning headless `claude` per query with read-only tool restrictions
- Hybrid streaming UX (placeholder → final via chat.update)
- 6h periodic MCP-auth health probe + DM notification
- Graceful shutdown on SIGINT/SIGTERM

Phase 2b (deferred):
- Real PreToolUse approval hook with Slack button gating
- Token-by-token chat.update progress (currently single-shot at end)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from agent_me.slack_bridge import approvals

# ── Repo dir resolution ──────────────────────────────────────────────────

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
    processors=_shared_processors + [
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
# 10 MB × 5 files = ~50 MB cap; ~weeks of data at typical chat volume.
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


_migrate_pending_approvals()
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


# ── Claude Code session map (thread_ts → session_id) ───────────────────
#
# Each Slack thread gets its own Claude Code session. First message in a
# thread spawns a fresh session; subsequent messages resume it via
# `claude -p --resume <session_id>`, so claude itself handles all
# context/cache management. Session IDs persist in SQLite, so a bridge
# restart doesn't drop conversational continuity.

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
    just an audit trail; the actual conversational state lives on disk in
    claude's own session store."""
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


# ── Tool restrictions: Phase 2a vs Phase 2b ─────────────────────────────
#
# Phase 2a (default until APPROVAL_GATE=1): blanket disallow of all
# write / side-effect tools. Safe but the bot can't actually do anything
# beyond reading.
#
# Phase 2b (APPROVAL_GATE=1): write tools are *allowed* but each call
# triggers a PreToolUse hook that posts to Slack and waits for
# approval before letting Claude execute. The hook matcher lives in
# approvals.HOOK_MATCHER; Claude Code's CLI flags here just need to
# stop pre-blocking those tools so the hook gets a chance to fire.

PHASE_2A_ALLOWED_TOOLS = " ".join((
    "Read", "Grep", "Glob", "WebFetch", "WebSearch",
    "mcp__maas-confluence__*",
    "mcp__maas-gdrive__*",
    "mcp__maas-gitlab__*",
    "mcp__maas-glean__*",
    "mcp__maas-ippsec__*",
    "mcp__maas-jama__*",
    "mcp__maas-jira__*",
    "mcp__maas-mysql__*",
    "mcp__maas-nsight-cuda__*",
    "mcp__maas-nvbugs__*",
    "mcp__maas-onedrive__*",
    "mcp__maas-sharepoint__*",
))

PHASE_2A_DISALLOWED_TOOLS = " ".join((
    "Bash", "Write", "Edit", "NotebookEdit",
    "mcp__maas-jira__jira_create_issue",
    "mcp__maas-jira__jira_clone_issue",
    "mcp__maas-jira__jira_update_issue",
    "mcp__maas-jira__jira_transition_issue",
    "mcp__maas-nvbugs__nvbugs_update_bug_v2",
    "mcp__maas-nvbugs__nvbugs_update_bug",
    "mcp__maas-ippsec__register_repo",
    "mcp__maas-mysql__execute_sql",
    "mcp__maas-gitlab__gitlab_coderabbit_ai_prompt",
    "mcp__maas-gitlab__gitlab_greptile_ai_suggestions",
))

# Phase 2b allow-list = read tools + every MCP wildcard, including the
# write actions that 2a blocked. The PreToolUse hook (approvals.py)
# matches the same write tools and gates them via Slack buttons.
PHASE_2B_ALLOWED_TOOLS = " ".join((
    "Read", "Grep", "Glob", "WebFetch", "WebSearch",
    "Write", "Edit", "NotebookEdit", "Bash",
    "mcp__maas-confluence__*",
    "mcp__maas-gdrive__*",
    "mcp__maas-gitlab__*",
    "mcp__maas-glean__*",
    "mcp__maas-ippsec__*",
    "mcp__maas-jama__*",
    "mcp__maas-jira__*",
    "mcp__maas-mysql__*",
    "mcp__maas-nsight-cuda__*",
    "mcp__maas-nvbugs__*",
    "mcp__maas-onedrive__*",
    "mcp__maas-sharepoint__*",
))

# Tools never allowed in chat regardless of approval (truly dangerous /
# operationally unsuitable for chat surface). Empty for v1 — the hook
# is the gate. Move things here only after a real incident.
PHASE_2B_DISALLOWED_TOOLS = ""

# Toggle. Default 0 (Phase 2a behaviour, no behavioural change for
# already-deployed bridges). Setting `APPROVAL_GATE=1` in configs/.env
# turns on the hook + Slack flow.
APPROVAL_GATE_ON = os.environ.get("APPROVAL_GATE", "0") == "1"

# Default 5 min for Phase 2a (no human in loop). With Phase 2b approval
# gate on, bump it to 12 min so a hook waiting on a Slack button click
# doesn't kill the subprocess. Operator can override via env.
CLAUDE_TIMEOUT_S = float(
    os.environ.get(
        "CLAUDE_TIMEOUT_S",
        12 * 60 if os.environ.get("APPROVAL_GATE", "0") == "1" else 5 * 60,
    )
)
MAX_SLACK_TEXT = 39000
MAX_LOG_TEXT = 4000

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")

# Chat-only working directory for `claude -p` invocations from Slack.
#
# Why not REPO_DIR: REPO_DIR holds the agent-me project's CLAUDE.md,
# which contains the "auto memory" protocol meant for development
# sessions. When a Slack user said "ghi nhớ" (remember), claude faithfully
# followed the protocol — read MEMORY.md, wrote a new memory file, and
# updated the index. 10 turns, 78s, $1.09 for one chat message. Bridge
# users want a chat assistant, not a memory-management agent.
#
# A purpose-built empty cwd has no CLAUDE.md, no auto-memory directives,
# and no project-specific tooling instructions. Claude responds
# conversationally. Sessions persist in ~/.claude/projects/<sanitized
# CHAT_CWD>/ so --resume across this dir works exactly the same as it
# would from REPO_DIR — different on-disk location, same behavior.
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


def strip_bot_mention(text: str | None) -> str:
    return re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", text or "").strip()


async def run_command(cmd: list[str], cwd: str, timeout: float = 30.0) -> str:
    """Run an arbitrary command, capture stdout, raise on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{cmd[0]} timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} exited {proc.returncode}: {stderr.decode(errors='replace')[:500]}"
        )
    return stdout.decode(errors="replace")


class SessionExpired(RuntimeError):
    """Raised when --resume <id> hits a session that no longer exists.

    Caller should retry without --resume to start a fresh conversation."""


async def spawn_claude(
    prompt: str,
    *,
    resume_session_id: str | None = None,
) -> tuple[str, str | None]:
    """Spawn `claude -p` with read-only tool restrictions.

    Uses --output-format json so the wrapper can capture session_id (and
    usage stats for logging). Pass `resume_session_id` to continue an
    existing thread — claude handles all context and cache transparently.

    Returns (response_text, session_id_or_None).

    Raises SessionExpired if --resume hit a missing session — caller can
    retry without --resume to recover.
    """
    if APPROVAL_GATE_ON:
        allowed_tools = PHASE_2B_ALLOWED_TOOLS
        disallowed_tools = PHASE_2B_DISALLOWED_TOOLS
    else:
        allowed_tools = PHASE_2A_ALLOWED_TOOLS
        disallowed_tools = PHASE_2A_DISALLOWED_TOOLS

    args = [
        "claude", "-p", prompt,
        "--model", MODEL,
        "--output-format", "json",
        # NO --dangerously-skip-permissions: that flag bypasses --disallowedTools,
        # which made claude follow the project's "auto memory" protocol (loaded
        # from REPO_DIR/CLAUDE.md back when the bridge ran with cwd=REPO_DIR) and
        # write .md files via the Write tool. With this flag removed plus the
        # cwd change below, --disallowedTools (Write/Edit/Bash) is enforced and
        # CLAUDE.md isn't loaded at all — chat is just chat.
        "--permission-mode", "dontAsk",
        "--allowedTools", allowed_tools,
    ]
    if disallowed_tools:
        args += ["--disallowedTools", disallowed_tools]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    log.info("claude_spawn", cwd=str(CHAT_CWD), model=MODEL,
             prompt_len=len(prompt), resume=resume_session_id)
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(CHAT_CWD),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"claude timed out after {CLAUDE_TIMEOUT_S}s")
    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()[:500]
        # Cover the actual phrases the CLI uses when --resume <id> can't
        # find the session. Confirmed empirically:
        #   - "No conversation found with session ID: …"   (claude 2.1.x)
        # Plus defensive matches for closely related variants we might see
        # in future versions.
        err_l = err.lower()
        if resume_session_id and (
            "no conversation found" in err_l
            or "conversation not found" in err_l
            or "session not found" in err_l
            or "session does not exist" in err_l
            or "no such session" in err_l
            or "invalid session" in err_l
            or "session expired" in err_l
        ):
            raise SessionExpired(
                f"resume failed for {resume_session_id[:8]}…: {err}"
            )
        raise RuntimeError(f"claude exited {proc.returncode}: {err}")

    raw = stdout.decode(errors="replace").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("claude_json_parse_failed", err=str(exc), preview=raw[:300])
        raise RuntimeError(f"claude returned non-JSON: {raw[:200]}") from exc

    if payload.get("is_error"):
        api_err = payload.get("api_error_status") or payload.get("result") or "unknown"
        raise RuntimeError(f"claude API error: {api_err}")

    text = str(payload.get("result", "")).strip()
    sid = payload.get("session_id")
    usage = payload.get("usage") or {}
    log.info("claude_done",
             session_id=sid,
             num_turns=payload.get("num_turns"),
             duration_ms=payload.get("duration_ms"),
             input_tokens=usage.get("input_tokens"),
             output_tokens=usage.get("output_tokens"),
             cache_read_tokens=usage.get("cache_read_input_tokens"),
             cost_usd=payload.get("total_cost_usd"))
    return text, sid


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
    "• `brief` / `/brief` — daily brief (Jira + GitLab + GitHub + NVBugs + Confluence)",
    "• `brief week` / `/brief week` — weekly recap (last 7 days)",
    "• `brief month` / `/brief month` — monthly recap (last 30 days)",
    "• `mcp` / `/mcp` — list MCP server health & auth status",
    "• `reauth` / `/reauth` — trigger MCP re-auth helper (auto-opens auth URLs on bridge host)",
    "• `reset` / `clear` / `new` — start a fresh Claude session for this thread (drops prior context)",
    "• `whoami` / `/whoami` — show your Slack user id",
    "• `version` / `/version` — bridge + claude versions and pinned model",
    "• `help` / `/help` — this message",
    "",
    "_Anything else is sent to Claude — context is preserved per Slack thread (reply-in-thread to keep the conversation going; new top-level message = fresh session)._",
    "_The bridge auto-checks MCP auth health every 6h and DMs you when re-auth is needed._",
    "_Daily morning routine fires at 6am Vietnam time when the bridge is running._",
))


async def cmd_mcp() -> str:
    out = await run_command(["claude", "mcp", "list"], cwd=str(REPO_DIR), timeout=60.0)
    return (
        "`claude mcp list`:\n```\n" + out.strip() + "\n```\n"
        "_Run `/reauth` (or `uv run agent-me-reauth` on the host) for any servers that need authentication._"
    )


async def cmd_version() -> str:
    ver = (await run_command(["claude", "--version"], cwd=str(REPO_DIR))).strip()
    return (
        f"*Bridge:* python · *Phase:* 2a · *Model:* `{MODEL}`\n"
        f"*Claude CLI:* `{ver}`\n"
        f"*Repo:* `{REPO_DIR}`"
    )


async def cmd_whoami(user_id: str | None) -> str:
    return f"Your Slack user id: `{user_id or '(unknown — DM the bot once first)'}`"


BRIEF_LOG_FILE = _LOG_DIR / "brief.log"


async def cmd_brief(args_text: str = "") -> str:
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
    log_fp = open(BRIEF_LOG_FILE, "ab")
    log_fp.write(
        f"\n=== {datetime.now().isoformat()} brief --period {period} (pid {os.getpid()} parent) ===\n".encode()
    )
    log_fp.flush()
    try:
        await asyncio.create_subprocess_exec(
            "uv", "run", "agent-me-brief", "--period", period,
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
        "Watch for a `🔄 generating…` message in your DM (it auto-updates with progress, "
        "then becomes the final brief). Total time ~30-90s.\n"
        f"_If something goes wrong, tail `{BRIEF_LOG_FILE}` for crash details "
        "or run `/mcp` + `/reauth` for stale tokens._"
    )


async def cmd_reset(thread_ts: str | None) -> str:
    """Drop the Claude Code session for this thread so the next message
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
            "ℹ️ This thread has no active Claude session yet — your next "
            "message starts a new one automatically."
        )
    return (
        f"🧹 *Cleared session* `{old[:8]}…` for this thread.\n"
        "Your next message starts a fresh conversation (no prior context)."
    )


async def cmd_reauth() -> str:
    """Trigger the reauth helper as a detached background process.

    The helper will open auth URLs in the bridge host's default browser.
    User signs in to NVIDIA SSO in each tab on the host.
    """
    # Spawn detached so we don't block. stdout/stderr go to /dev/null;
    # the user sees results by checking `claude mcp list` after signing in.
    await asyncio.create_subprocess_exec(
        "uv", "run", "agent-me-reauth",
        cwd=str(REPO_DIR),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    return (
        "🚀 *Re-auth helper started* on the bridge host.\n\n"
        "Browser tabs will open shortly — sign in to NVIDIA SSO in each.\n"
        "When done, run `/mcp` here to verify everything is `✓ Connected`."
    )


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
        ]},
    ]


async def handle_slash(cmd: str, user_id: str | None, args_text: str = "",
                       *, thread_ts: str | None = None) -> str | SlashResult:
    if cmd == "/mcp":
        return await cmd_mcp()
    if cmd == "/version":
        return await cmd_version()
    if cmd == "/whoami":
        return await cmd_whoami(user_id)
    if cmd == "/reauth":
        return await cmd_reauth()
    if cmd == "/brief":
        return await cmd_brief(args_text)
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

async def post_thinking(client, channel: str, thread_ts: str) -> str:
    res = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="🔄 thinking…")
    return res["ts"]


async def update_progress(client, channel: str, ts: str, text: str) -> None:
    await client.chat_update(channel=channel, ts=ts, text=truncate_for_slack(text))


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
        try:
            result = await handle_slash(cmd, user_id, args_text,
                                         thread_ts=thread_ts)
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

    # Plain-text command intercept (exact match): "brief", "brief week", "mcp", etc.
    plain_key = cleaned.strip().lower()
    if plain_key in PLAIN_COMMANDS:
        cmd, args_text = PLAIN_COMMANDS[plain_key]
        await _dispatch(cmd, args_text, "plain")
        return

    # Slash-prefix intercept: route /mcp etc. without spawning claude.
    m = re.match(r"^(/[a-z][a-z0-9_-]*)\b\s*(.*)$", cleaned, re.IGNORECASE)
    if m:
        cmd = m.group(1)
        args_text = m.group(2)
        await _dispatch(cmd, args_text, "slash")
        return

    await upsert_thread(thread_ts, channel, user_id)
    await insert_message(thread_ts, "user", cleaned, event_ts)

    try:
        placeholder_ts = await post_thinking(client, channel, thread_ts)
    except Exception as exc:
        log.error("post_thinking_failed", err=str(exc), thread_ts=thread_ts)
        return

    start = time.time()
    try:
        # Look up the Claude Code session for this Slack thread. First
        # message ⇒ no session yet ⇒ claude creates one and we save the
        # ID. Subsequent messages resume the same session, which is how
        # context, prompt-cache hits, and tool-use history are preserved.
        existing_sid = await get_session_id(thread_ts)
        try:
            answer, new_sid = await spawn_claude(
                cleaned, resume_session_id=existing_sid,
            )
        except SessionExpired as exc:
            # The on-disk session went away (claude was restarted, project
            # path changed, file got cleaned, etc.). Retry without --resume
            # so the user sees the bridge respond gracefully instead of an
            # error message — they lose continuity, that's it.
            log.warning("session_expired_starting_fresh",
                        thread_ts=thread_ts, expired=existing_sid, err=str(exc))
            await clear_session(thread_ts)
            answer, new_sid = await spawn_claude(cleaned)

        if new_sid:
            await upsert_session(thread_ts, new_sid)

        final = answer if answer.strip() else "_(no output)_"
        await update_progress(client, channel, placeholder_ts, final)
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
        result = await handle_slash(cmd_name, command.get("user_id"), args_text)
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


@app.action("brief_refresh")
async def on_brief_refresh(ack, body, client):
    await ack()
    period = (body.get("actions") or [{}])[0].get("value") or "day"
    log.info("button_brief_refresh", period=period, user=body.get("user", {}).get("id"))
    await cmd_brief(period if period != "day" else "")
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
    await cmd_brief("")
    await _reply_in_thread(client, body,
                           "📅 Generating today's brief — back in ~60s in this DM (top level).")


# ── Action-menu buttons (posted in morning thread or after reauth) ────

@app.action("menu_brief_day")
async def on_menu_brief_day(ack, body, client):
    await ack()
    log.info("button_menu_brief_day", user=body.get("user", {}).get("id"))
    await cmd_brief("")
    await _reply_in_thread(client, body, "📅 Daily brief generating — back in ~60s.")


@app.action("menu_brief_week")
async def on_menu_brief_week(ack, body, client):
    await ack()
    log.info("button_menu_brief_week", user=body.get("user", {}).get("id"))
    await cmd_brief("week")
    await _reply_in_thread(client, body, "📊 Weekly recap generating — back in ~60s.")


@app.action("menu_brief_month")
async def on_menu_brief_month(ack, body, client):
    await ack()
    log.info("button_menu_brief_month", user=body.get("user", {}).get("id"))
    await cmd_brief("month")
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
        out = await run_command(["claude", "mcp", "list"], cwd=str(REPO_DIR), timeout=60.0)
    except Exception as exc:
        log.warning("mcp_list_failed", err=str(exc))
        return
    need_auth = sorted(
        line.split(":")[0].strip()
        for line in out.splitlines()
        if "Needs authentication" in line
    )
    log.info("mcp_health_check", need_auth_count=len(need_auth), servers=need_auth)
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
        "uv run agent-me-reauth",
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
             "text": {"type": "plain_text", "text": "❓ Help"},
             "action_id": "menu_help"},
        ]},
    ]


async def run_morning_routine(client) -> None:
    """Daily morning conversation in DM.

    Flow:
      1. Post a fresh date-headed STARTER message (becomes thread root).
      2. Run `claude mcp list` to check MCP auth state.
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
        out = await run_command(["claude", "mcp", "list"], cwd=str(REPO_DIR), timeout=60.0)
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
        "✅ *All MCPs connected.*\n\n"
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
    # Fast path: per-thread auto-approve. We don't know the thread_ts
    # from the hook input directly (Claude has no idea about Slack),
    # but we keyed each spawn_claude on a thread. The mapping lives in
    # claude_sessions: session_id → thread_ts. If session_id is set on
    # the request and the thread has auto_approve = 1, just allow.
    thread_ts: str | None = None
    if req.session_id:
        async with DB_LOCK:
            row = db.execute(
                "SELECT thread_ts FROM claude_sessions WHERE session_id = ?",
                (req.session_id,),
            ).fetchone()
        if row:
            thread_ts = row[0]
            if approvals.thread_auto_approve(db, thread_ts):
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
    log.info("bridge_starting", phase=("2b" if APPROVAL_GATE_ON else "2a"),
             approval_gate=APPROVAL_GATE_ON,
             model=MODEL, mcp_check_interval_s=MCP_CHECK_INTERVAL_S)

    # Phase 2b bootstrap: write the PreToolUse hook + .claude/settings.json
    # into CHAT_CWD so claude -p picks them up. Idempotent — overwrites on
    # every startup so a fresh state dir on a new host works without
    # manual intervention.
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
        try:
            loop.add_signal_handler(sig, _sig, sig.name)
        except (NotImplementedError, RuntimeError):
            pass

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
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass
        try:
            await asyncio.wait_for(handler.close_async(), timeout=4.0)
        except (TimeoutError, Exception) as exc:
            log.warning("handler_close_timed_out_or_failed", err=str(exc))
        try:
            db.close()
        except Exception:
            pass
        log.info("bridge_stopped")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
