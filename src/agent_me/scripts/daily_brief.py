"""agent-me brief — fan-out Codex turns per source, post root + threaded replies.

Run with:
    uv run agent-me-brief                # daily (today's outlook)
    uv run agent-me-brief --period week  # weekly recap
    uv run agent-me-brief --period month # monthly recap
    uv run agent-me-brief --dry-run      # print per-source JSON, don't post
    uv run agent-me-brief --channel C123 # post to a specific channel id

Architecture (2026-05-10 refactor):

    Python orchestrator
      ├── post root header DM ("📅 Daily Brief — running…")
      └── asyncio.gather (7 subagents in parallel):
            ├── jira       → direct MCP JSON-RPC (maas-jira)
            ├── gitlab     → direct MCP JSON-RPC (maas-gitlab)
            ├── nvbugs     → direct MCP JSON-RPC (maas-nvbugs)
            ├── slack      → codex exec (Slack app tools)
            ├── outlook    → codex exec (Outlook Email app tools)
            ├── calendar   → codex exec (Outlook Calendar app tools)
            └── github     → `gh` CLI directly
          Each subagent posts ONE threaded reply when done.
      └── final priority synthesis posted as last threaded reply.
      └── root header updated with item-count summary + actions buttons.
      └── optional Slack-connector mirror via Codex app-server auto-review.

Why fan-out: previously a single 1700-token prompt asked claude to call
all MCPs serially in one turn. Wall-clock ~60-230s, single ~3-4kB JSON
blob hard to fit in one Slack message. Fan-out gives:
  - parallelism (max ~one subagent's runtime, not sum)
  - per-source context window (no MCP tool catalogs blowing each other's budget)
  - per-source Slack message (no >2900-char overflow / split-section hacks)
  - per-source error isolation (slack reauth fail doesn't kill jira fetch)

Why on-demand (not cron): MAAS-MCP tokens expire ~24h and need a manual
browser-based SSO refresh, so a true 8am cron would silently fail on
stale-token mornings. Pattern: bridge DMs you when MCPs go stale, you
`/reauth`, then `/brief` (or `/brief weekly`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog
from dotenv import load_dotenv
from slack_sdk import WebClient

from agent_me.codex_app_server import run_codex_app_server
from agent_me.mcp_tokens import codex_mcp_token_env

# ── Setup ────────────────────────────────────────────────────────────────

REPO_DIR = Path(os.environ.get("AGENT_ME_REPO_DIR") or Path(__file__).resolve().parents[3])
ENV = REPO_DIR / "configs" / ".env"
if ENV.exists():
    load_dotenv(ENV)

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
    ],
    cache_logger_on_first_use=True,
)
log = structlog.get_logger("daily-brief")

USER = os.environ.get("AGENT_ME_USER", "thaphan")  # NVIDIA shortname
USER_FULL_NAME = os.environ.get("AGENT_ME_FULL_NAME", "Thanh Phan")
DEFAULT_MIRROR_EMAIL = os.environ.get("BRIEF_MIRROR_EMAIL", "thaphan@nvidia.com")
AGENT_TIMEOUT_S = float(os.environ.get("AGENT_TIMEOUT_S", os.environ.get("CODEX_TIMEOUT_S", 240.0)))
MODEL = os.environ.get("CODEX_MODEL", os.environ.get("AGENT_MODEL", "gpt-5.5"))
PRIORITY_WINDOW_DAYS = 7
ITEMS_PER_GROUP = 5
SLACK_SECTION_MAX_CHARS = 2900   # Slack hard limit is 3000; keep buffer.
BRIEF_TIMEZONE = os.environ.get("AGENT_ME_TIMEZONE", "Asia/Ho_Chi_Minh")
try:
    LOCAL_TZ = ZoneInfo(BRIEF_TIMEZONE)
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC")
    BRIEF_TIMEZONE = "UTC"

PERIOD_PRESETS = {
    "day":   {"days": 1,  "label": "Daily",   "title": "Daily Brief"},
    "week":  {"days": 7,  "label": "Weekly",  "title": "Weekly Brief"},
    "month": {"days": 30, "label": "Monthly", "title": "Monthly Brief"},
}


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


CODEX_BIN = resolve_cli_bin("CODEX_BIN", "codex")
NVBUGS_BUG_URL_BASE = "https://nvbugspro.nvidia.com/bug"
JIRA_MCP_URL = os.environ.get(
    "AGENT_ME_JIRA_MCP_URL",
    "https://nvaihub.nvidia.com/maas/jira/mcp/",
)
GITLAB_MCP_URL = os.environ.get(
    "AGENT_ME_GITLAB_MCP_URL",
    "https://nvaihub.nvidia.com/maas/gitlab/mcp/",
)
NVBUGS_MCP_URL = os.environ.get(
    "AGENT_ME_NVBUGS_MCP_URL",
    "https://nvaihub.nvidia.com/maas/nvbugs/mcp/",
)
READONLY_MAAS_APPROVAL_CONFIGS = (
    'mcp_servers.maas-jira.tools.jira_search.approval_mode="approve"',
    'mcp_servers.maas-gitlab.tools.gitlab_list_merge_requests.approval_mode="approve"',
    'mcp_servers.maas-gitlab.tools.gitlab_list_issues.approval_mode="approve"',
    'mcp_servers.maas-confluence.tools.confluence_search.approval_mode="approve"',
    'mcp_servers.maas-nvbugs.tools.nvbugs_check_connection_v2.approval_mode="approve"',
    'mcp_servers.maas-nvbugs.tools.nvbugs_search_v2.approval_mode="approve"',
    'mcp_servers.maas-nvbugs.tools.nvbugs_get_bug_details_v2.approval_mode="approve"',
)


# ── Data model ──────────────────────────────────────────────────────────

@dataclass
class BriefItem:
    source: str        # "jira" | "gitlab" | "github" | "nvbugs" | "slack" | "outlook" | "calendar"
    icon: str
    item_id: str
    title: str
    url: str
    group: str = "uncategorized"
    reason: str | None = None
    status: str | None = None
    priority: str | None = None
    deadline: str | None = None        # ISO date
    last_activity: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class SlackDestination:
    label: str
    channel: str
    root_ts: str
    thread_ts: str | None = None

    @property
    def reply_thread_ts(self) -> str:
        return self.thread_ts or self.root_ts


@dataclass
class ConnectorMirrorResult:
    ok: bool
    error: str | None = None
    raw: str = ""


# ── Date / format helpers (kept from previous version) ─────────────────

def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_today() -> date:
    return local_now().date()


def period_window(period_days: int) -> dict[str, str]:
    start = local_today()
    end_exclusive = start + timedelta(days=max(period_days, 1))
    end_display = end_exclusive - timedelta(days=1)
    start_dt = datetime.combine(start, dt_time.min, LOCAL_TZ)
    end_dt = datetime.combine(end_exclusive, dt_time.min, LOCAL_TZ)
    return {
        "timezone": BRIEF_TIMEZONE,
        "start_date": start.isoformat(),
        "end_date": end_display.isoformat(),
        "start_at": start_dt.isoformat(timespec="seconds"),
        "end_at": end_dt.isoformat(timespec="seconds"),
    }


def parse_date(s: str | None) -> date | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.replace("Z", ""), fmt.replace("Z", "")).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def fmt_due(deadline: str | None) -> str:
    d = parse_date(deadline)
    if not d:
        return ""
    today = local_today()
    delta = (d - today).days
    if delta < 0:
        return f"⚠️ overdue {abs(delta)}d ({d.isoformat()})"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta <= 7:
        return f"{d.strftime('%a %m/%d')}"
    return d.isoformat()


def fmt_age(s: str | None) -> str:
    d = parse_date(s)
    if not d:
        return ""
    delta = (local_today() - d).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta < 7:
        return f"{delta}d ago"
    if delta < 30:
        return f"{delta // 7}w ago"
    if delta < 365:
        return f"{delta // 30}mo ago"
    return d.isoformat()


def parse_datetime(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo:
        return dt.astimezone(LOCAL_TZ)
    return dt.replace(tzinfo=LOCAL_TZ)


def fmt_event_time(start: str | None, end: str | None) -> str:
    start_dt = parse_datetime(start)
    end_dt = parse_datetime(end)
    if not start_dt:
        return ""
    if end_dt and start_dt.date() == end_dt.date():
        return f"{start_dt.strftime('%a %m/%d %H:%M')}-{end_dt.strftime('%H:%M')}"
    if end_dt:
        return f"{start_dt.strftime('%a %m/%d %H:%M')} -> {end_dt.strftime('%a %m/%d %H:%M')}"
    return start_dt.strftime("%a %m/%d %H:%M")


def md_link(text: str, url: str) -> str:
    if url:
        safe_text = text.replace("|", "·").replace(">", "-")[:120]
        return f"<{url}|{safe_text}>"
    return text[:120]


def _g(d: dict, key: str, default: str = "uncategorized") -> str:
    v = d.get(key)
    return str(v) if v else default


# ── Per-source parsers ────────────────────────────────────────────────

def parse_jira(data: dict, _spec: SourceSpec) -> list[BriefItem]:
    out: list[BriefItem] = []
    for j in data.get("items", []) or []:
        key = str(j.get("key", "?"))
        derived = key.split("-")[0] if "-" in key else "uncategorized"
        out.append(BriefItem(
            source="jira", icon="📋",
            item_id=key,
            title=str(j.get("summary", ""))[:200],
            url=str(j.get("url", "")),
            group=_g(j, "group", derived),
            reason=j.get("reason"),
            status=j.get("status"),
            priority=j.get("priority"),
            deadline=j.get("duedate"),
            last_activity=j.get("updated"),
        ))
    return out


def _jira_rows(payload: dict) -> list[dict]:
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    rows = data.get("issues") or data.get("results") or data.get("items") or []
    return [row for row in rows if isinstance(row, dict)]


def _jira_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "displayName", "value", "key"):
            if value.get(key):
                return str(value[key])
    if value:
        return str(value)
    return ""


def _normalize_jira(issue: dict, reason: str) -> dict:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    key = str(issue.get("key") or fields.get("key") or issue.get("id") or "")
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    group = str(
        project.get("key") or (key.split("-")[0] if "-" in key else "uncategorized")
    )
    return {
        "key": key,
        "summary": fields.get("summary") or issue.get("summary") or "",
        "status": _jira_name(fields.get("status") or issue.get("status")),
        "priority": _jira_name(fields.get("priority") or issue.get("priority")),
        "duedate": fields.get("duedate") or issue.get("duedate"),
        "url": issue.get("url") or (
            f"https://jirasw.nvidia.com/browse/{key}" if key else ""
        ),
        "updated": (
            fields.get("updatedDate")
            or fields.get("updated")
            or issue.get("updatedDate")
            or issue.get("updated")
        ),
        "group": group,
        "reason": reason,
    }


def parse_gitlab(data: dict, _spec: SourceSpec) -> list[BriefItem]:
    out: list[BriefItem] = []
    for m in data.get("mrs", []) or []:
        out.append(BriefItem(
            source="gitlab", icon="🦊",
            item_id=f"!{m.get('iid', '?')}",
            title=str(m.get("title", ""))[:200],
            url=str(m.get("web_url", "")),
            group=_g(m, "group", str(m.get("project_path") or "uncategorized")),
            reason=m.get("reason"),
            status=m.get("state"),
            deadline=m.get("milestone_due_date"),
            last_activity=m.get("updated_at"),
            extras={"kind": "MR"},
        ))
    for i in data.get("issues", []) or []:
        out.append(BriefItem(
            source="gitlab", icon="🦊",
            item_id=f"#{i.get('iid', '?')}",
            title=str(i.get("title", ""))[:200],
            url=str(i.get("web_url", "")),
            group=_g(i, "group", str(i.get("project_path") or "uncategorized")),
            reason=i.get("reason"),
            status=i.get("state"),
            deadline=i.get("due_date"),
            last_activity=i.get("updated_at"),
            extras={"kind": "issue"},
        ))
    return out


def _gitlab_mrs(payload: dict) -> list[dict]:
    rows = payload.get("merge_requests") or payload.get("mrs") or payload.get("items") or []
    return [row for row in rows if isinstance(row, dict)]


def _normalize_gitlab_mr(row: dict, reason: str) -> dict:
    project = row.get("project") if isinstance(row.get("project"), dict) else {}
    project_path = (
        row.get("project_path")
        or row.get("path_with_namespace")
        or project.get("path_with_namespace")
        or project.get("name_with_namespace")
        or project.get("name")
        or "uncategorized"
    )
    updated = (
        row.get("merged_at")
        or row.get("mergedAt")
        or row.get("updated_at")
        or row.get("updated")
        or row.get("updatedAt")
    )
    return {
        "iid": row.get("iid") or row.get("id") or "?",
        "title": row.get("title") or "",
        "state": row.get("state") or "opened",
        "web_url": row.get("web_url") or row.get("url") or "",
        "milestone_due_date": row.get("milestone_due_date") or row.get("due_date"),
        "updated_at": updated,
        "group": str(project_path),
        "reason": reason,
    }


def parse_nvbugs(data: dict, _spec: SourceSpec) -> list[BriefItem]:
    out: list[BriefItem] = []
    for nb in data.get("items", []) or []:
        raw_id = nb.get("id") or nb.get("BugId") or nb.get("bugid") or "?"
        bug_id = str(raw_id).removeprefix("Bug ")
        url = str(nb.get("url", ""))
        if bug_id and bug_id != "?" and not url:
            url = f"{NVBUGS_BUG_URL_BASE}/{bug_id}"
        out.append(BriefItem(
            source="nvbugs", icon="🐛",
            item_id=bug_id,
            title=str(nb.get("title") or nb.get("Synopsis") or nb.get("synopsis") or "")[:200],
            url=url,
            group=_g(nb, "group", _g(nb, "Module", "uncategorized")),
            reason=nb.get("reason"),
            status=nb.get("status") or nb.get("BugAction") or nb.get("Disposition"),
            priority=nb.get("priority") or nb.get("Priority"),
            deadline=nb.get("due"),
            last_activity=nb.get("updated") or nb.get("RequestDate"),
        ))
    return out


def _nvbugs_rows(payload: dict) -> list[dict]:
    data = payload.get("data") or {}
    rows = ((data.get("ReturnValue") or {}).get("data") or [])
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3 or not isinstance(row[2], dict):
            continue
        out.append(row[2])
    return out


def _normalize_nvbug(row: dict, reason: str) -> dict:
    bug_id = str(row.get("BugId") or row.get("bugid") or "")
    return {
        "id": bug_id,
        "title": row.get("Synopsis") or row.get("synopsis") or "",
        "priority": row.get("Priority"),
        "status": row.get("BugAction") or row.get("Disposition") or "Open",
        "due": None,
        "updated": row.get("RequestDate"),
        "url": f"{NVBUGS_BUG_URL_BASE}/{bug_id}" if bug_id else "",
        "group": row.get("Module") or "uncategorized",
        "reason": reason,
    }


def parse_slack(data: dict, _spec: SourceSpec) -> list[BriefItem]:
    out: list[BriefItem] = []
    for s in data.get("items", []) or []:
        chan = str(s.get("channel", "?"))
        sender = s.get("user") or s.get("from") or ""
        snippet = str(s.get("snippet", ""))[:160]
        out.append(BriefItem(
            source="slack", icon="💬",
            item_id=f"@{sender}" if sender else "",
            title=f"[{chan}] {snippet}" if snippet else f"[{chan}]",
            url=str(s.get("permalink", "") or s.get("url", "")),
            group=_g(s, "group", chan or "uncategorized"),
            reason=s.get("reason"),
            last_activity=s.get("timestamp") or s.get("ts"),
        ))
    return out


def parse_outlook(data: dict, _spec: SourceSpec) -> list[BriefItem]:
    out: list[BriefItem] = []
    for e in data.get("items", []) or []:
        sender = str(e.get("from", ""))
        out.append(BriefItem(
            source="outlook", icon="📧",
            item_id="",
            title=str(e.get("subject", ""))[:200],
            url=str(e.get("url", "") or e.get("web_link", "")),
            group=_g(e, "group", sender.split("@")[-1] if "@" in sender else "uncategorized"),
            reason=e.get("reason"),
            last_activity=e.get("received"),
            extras={"from": sender, "snippet": str(e.get("snippet", ""))[:160]},
        ))
    return out


def parse_calendar(data: dict, _spec: SourceSpec) -> list[BriefItem]:
    out: list[BriefItem] = []
    for ev in data.get("items", []) or []:
        start = str(ev.get("start", "") or "")
        group = str(ev.get("group", "") or (parse_date(start) or "unscheduled"))
        out.append(BriefItem(
            source="calendar", icon="📅",
            item_id="",
            title=str(ev.get("subject", "") or ev.get("title", ""))[:200],
            url=str(ev.get("url", "") or ev.get("web_link", "")),
            group=group,
            reason=ev.get("reason"),
            status=ev.get("show_as") or ev.get("response_status"),
            extras={
                "start": start,
                "end": str(ev.get("end", "") or ""),
                "organizer": str(ev.get("organizer", "") or ""),
                "location": str(ev.get("location", "") or ""),
                "body_summary": str(ev.get("body_summary", "") or ev.get("summary", ""))[:180],
                "is_online": ev.get("is_online"),
            },
        ))
    return out


# ── Source specs ──────────────────────────────────────────────────────

@dataclass
class SourceSpec:
    id: str             # "jira"
    label: str          # "Jira"
    icon: str           # "📋"
    fetcher: Callable[[SourceSpec, int], Awaitable[dict]]
    parser: Callable[[dict, SourceSpec], list[BriefItem]]
    allowed_tools: str = ""   # legacy hint retained for source logging
    prompt_template: str = ""  # f-string with {user} and {period_days}


JIRA_PROMPT = """Return ONLY a JSON object: {{"items": [...]}}. No prose, no markdown fences.

User shortname: `{user}`. Find every Jira issue where this user is involved
AND statusCategory != Done.

Use only the registered `maas-jira` Jira search capability. Do not call Jira
write tools. Do not broaden into semantic/text search unless a project key is
already known from the structured searches below.

Run these searches separately, merge, and dedupe by key:
  1. assignee = currentUser() AND statusCategory != Done
  2. assignee = "{user}" AND statusCategory != Done
  3. reporter = currentUser() AND statusCategory != Done
  4. reporter = "{user}" AND statusCategory != Done
  5. watcher = currentUser() AND statusCategory != Done

Do NOT run a broad `text ~ "{user}"` query without a project. Jira rejects
text searches unless a project is specified. If you find project keys from
the searches above, you may optionally run:
  project in (<found project keys>) AND text ~ "{user}" AND statusCategory != Done

Order each query by updated DESC or duedate ASC NULLS LAST, maxResults/top_k 50.

Item schema (every field):
  {{"key": "NGC-123", "summary": "...", "status": "...",
    "priority": "...", "duedate": null, "url": "...",
    "updated": "...", "group": "<project_key>",
    "reason": "assignee|reporter|watcher|mentioned"}}

`group` = the project key (the part of the issue key before the dash,
e.g. "NGC" from "NGC-789"). `reason` = best-effort guess based on which
JQL clause likely matched.

If the tool errors, return {{"error": "<exact tool error>", "items": []}}.
Do not report errors as "nothing pending". JSON ONLY, no commentary."""


GITLAB_PROMPT = """Return ONLY a JSON object: {{"mrs": [...], "issues": [...]}}. No prose, no fences.

User: `{user}` (NVIDIA). Find GitLab MRs and code reviews in these groups:
1. open MRs I authored that are awaiting review
2. open MRs where I'm assigned as reviewer
3. MRs involving me that merged in the last 3 days

Use only the registered `maas-gitlab` read tools. Do not call GitLab write
tools. The current MaaS GitLab MR tool accepts `scope="me"` for user-related
merge requests.

Use `gitlab_list_merge_requests` with `scope="me"`:
  - `state="opened"`, `role="author"` for authored awaiting review
  - `state="opened"`, `role="reviewer"` for review requests
  - `state="merged"`, `role="author"|"reviewer"|"assignee"` for recently merged

2. Issues are optional. Only call mcp__maas-gitlab__gitlab_list_issues if
   you already have a concrete `project_id` accepted by the tool schema. If
   the tool requires `project_id` and no concrete project is known, return
   `"issues": []` without treating that as a source error.

For each MR:
  {{"iid": ..., "title": "...", "state": "opened",
    "web_url": "...", "milestone_due_date": null, "updated_at": "...",
    "group": "<project_path>",
    "reason": "authored_waiting_review|review_requested|recently_merged"}}

For each issue:
  {{"iid": ..., "title": "...", "state": "opened", "web_url": "...",
    "due_date": null, "updated_at": "...", "group": "<project_path>",
    "reason": "assignee|author"}}

If a tool errors, include {{"error": "<exact tool error>", "mrs": [], "issues": []}}.
Do not report errors as "nothing pending". JSON ONLY, no commentary."""


NVBUGS_PROMPT = """Return ONLY {{"items": [...]}}. No prose, no fences.

User full name: `{full_name}`.
Use the registered Codex MCP server `maas-nvbugs`; call only
`nvbugs_search_v2` unless a returned row is missing its bug id or title.

Find open NVBugs matching exactly either condition:
  1. QA Eng / QA Engineer is `{full_name}`
  2. ARB / Action Required By is `{full_name}`

Use exactly these two structured searches with max_results=50:
  - query: `Show open bugs where QAEngineerFullName = "{full_name}"`
    search_type: `structured`
  - query: `Show open bugs where ActionReqByFullName = "{full_name}"`
    search_type: `structured`

NVBugs v2 structured search field names are intentional here:
`QAEngineerFullName` for QA Eng and `ActionReqByFullName` for ARB.
Do NOT broaden the search to requester, assignee, reporter, Cc, comments,
keywords, semantic search, similarity search, or ARB text mentions.

Merge the two result sets and dedupe by bug id. Treat a bug as open when the
NVBugs result is returned by the "open bugs" structured query or its BugAction
contains `Open`. Do not call details/comments/attachments for every bug; the
search rows normally include BugId, Synopsis, Module, Priority, BugAction,
Disposition, RequestDate, Engineer, and Requester.

Each item:
  {{"id": "...", "title": "...", "priority": "P0|P1|P2|P3",
    "status": "Open", "due": null, "updated": "...",
    "url": "https://nvbugspro.nvidia.com/bug/<id>",
    "group": "<module|component|product>",
    "reason": "qa_eng|arb"}}

Every item MUST include a clickable NVBugs URL. If the tool returns only
an id, construct `https://nvbugspro.nvidia.com/bug/<id>`.

If the tool errors (auth, approval, timeout, or otherwise), return
{{"error": "<exact tool error>", "items": []}}. Do not report errors as
"nothing pending". JSON ONLY."""


SLACK_PROMPT = """Return ONLY {{"items": [...]}}. No prose, no fences.

You are READ-ONLY. Do NOT call any tool that posts, sends, replies,
deletes, or modifies messages. Use Codex Slack app read tools directly:
`slack_slack_search_public_and_private`, `slack_slack_read_channel`, and
`slack_slack_read_thread`.

User: `{user}` (NVIDIA). Find recent Slack messages in the last
{period_days} day(s) where someone needs my attention:
  - I'm @-mentioned (channel mentions)
  - DMs sent directly to me where the latest message is from someone else
  - Thread replies on threads I authored

Skip: bot/notification messages, channel-join/leave, my own messages.

Each item:
  {{"channel": "<#channel name or DM:user>", "user": "<sender display name>",
    "snippet": "<first 200 chars of message text>",
    "timestamp": "<ISO 8601 or epoch>",
    "permalink": "<slack permalink if available>",
    "group": "<channel name>", "reason": "mentioned|dm|thread_reply"}}

Top 25. If errors, return {{"error": "<exact tool error>", "items": []}}.
Do not report errors as "nothing pending". JSON ONLY."""


OUTLOOK_PROMPT = """Return ONLY {{"items": [...]}}. No prose, no fences.

You are READ-ONLY. Do NOT call any tool that sends, replies, drafts,
deletes, or modifies emails or calendar events.

Use only Codex Outlook Email read tools:
  - `microsoft outlook email_list_messages`
  - `microsoft outlook email_fetch_message` only if the list result lacks a
    usable snippet/body preview

Do NOT use Outlook search tools for this source. Do NOT pass any `filter`,
`$filter`, OData expression, OR clause, receivedDateTime predicate,
toRecipients predicate, ccRecipients predicate, or isRead predicate. The first
data call MUST be a plain recent message list with `top=40`,
`order_by="receivedDateTime desc"`, and the filter argument omitted. If the
tool returns `ErrorInvalidUrlQueryFilter`, that attempt is invalid; retry with
the same plain list call and no filter instead of returning the error.

User: `{user}@nvidia.com`. Fetch recent inbox/mailbox messages first, then
filter locally for the brief. Start with a plain recent message list sorted by
received time desc (top 40).

Primary target: messages received in the last {period_days} day(s) where I'm in
to/cc OR sender expects a reply/action. Include read messages too if they are
recent and actionable. For a daily brief, if the strict 1-day window has fewer
than 3 useful items, expand to the most recent 3 days and return the latest
direct/actionable messages so the source proves the connector is fetching data.
Never return an empty list until you have successfully checked a recent message
list.

Each item:
  {{"subject": "...", "from": "<email or display name>",
    "received": "<ISO 8601 timestamp>",
    "snippet": "<first 200 chars of body>",
    "url": "<deeplink or web_link if available>",
    "group": "<sender domain (e.g. nvidia.com) or 'external'>",
    "reason": "to|cc|reply_expected|action_required|recent_direct"}}

Skip: noreply@*, do-not-reply@*, list@*, automated build/CI notifications,
calendar invitations (those go in a separate calendar fetch later).
Top 20. If errors, return {{"error": "<exact tool error>", "items": []}}.
Do not report errors as "nothing pending". JSON ONLY."""


CALENDAR_PROMPT = """Return ONLY {{"items": [...]}}. No prose, no fences.

You are READ-ONLY. Do NOT call any tool that creates, updates, replies to,
deletes, or modifies calendar events. Use Codex Outlook Calendar app read
tools directly, preferably `microsoft outlook calendar_list_events`.

User: `{user}@nvidia.com`. Timezone: `{timezone}`.
Window: `{start_at}` inclusive to `{end_at}` exclusive
({start_date} through {end_date}, {period_days} day(s)).

Find meetings/events on the default personal Outlook calendar in this window.
For daily scope this means today's meetings; for weekly/monthly scope this
means the next 7/30 calendar days. Exclude cancelled events and declined
events. Skip all-day holidays or OOO blocks unless the title makes them
actionable.

Sort by start ascending. Top 60.

Each item:
  {{"subject": "...", "start": "<ISO 8601 with timezone>",
    "end": "<ISO 8601 with timezone>", "organizer": "<name or email>",
    "location": "<room/Teams/online/empty>", "is_online": true,
    "body_summary": "<one short summary of agenda/body if visible, else empty>",
    "url": "<deeplink or web_link if available>",
    "group": "YYYY-MM-DD", "show_as": "busy|tentative|free|...",
    "response_status": "accepted|tentative|organizer|required|optional|unknown",
    "reason": "organizer|required|optional|tentative|accepted"}}

Keep `body_summary` under 140 characters and do not invent details when the
event body is empty or hidden by permissions.

If errors, return {{"error": "<exact tool error>", "items": []}}.
Do not report errors as "nothing pending". JSON ONLY."""


# ── Fetchers (one per source kind) ────────────────────────────────────

BRIEF_SYSTEM_PROMPT = """\
Return only the JSON object requested by the source prompt. Use Codex app tools
and registered MaaS MCP servers directly. Do not run shell commands, do not
read local skill files, and do not call PA or Claude CLI for enterprise-source
reads. If a source tool fails, return an explicit error object; do not turn
tool failures into empty task lists.
"""


async def _run_codex(prompt: str, timeout_s: float) -> str:
    args = [
        CODEX_BIN,
    ]
    for cfg in READONLY_MAAS_APPROVAL_CONFIGS:
        args.extend(["-c", cfg])
    args.extend([
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox", "read-only",
        "--cd", str(REPO_DIR),
        "-m", MODEL,
        f"{BRIEF_SYSTEM_PROMPT}\n\n{prompt}",
    ])
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(REPO_DIR),
        env={**os.environ, **codex_mcp_token_env()},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"codex timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"codex exited {proc.returncode}: {stderr.decode(errors='replace')[:400]}"
        )
    final_text = ""
    nonfatal_errors: list[str] = []
    for line in stdout.decode(errors="replace").splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "item.completed":
            continue
        item = evt.get("item") or {}
        if item.get("type") == "agent_message":
            final_text = item.get("text") or final_text
        elif item.get("type") == "error":
            msg = str(item.get("message") or "")
            if "approval_policy" not in msg:
                nonfatal_errors.append(msg[:200])
    if final_text:
        return final_text.strip()
    if nonfatal_errors:
        raise RuntimeError("; ".join(nonfatal_errors))
    return ""


async def _run_codex_app_server(prompt: str, timeout_s: float) -> str:
    """Run a permissioned connector/MCP write through Codex app-server."""
    return await run_codex_app_server(
        prompt,
        codex_bin=CODEX_BIN,
        cwd=REPO_DIR,
        timeout_s=timeout_s,
    )


def _strip_to_json(text: str) -> dict:
    """Strip ``` fences and any leading prose, then json.loads. Raises on failure."""
    t = re.sub(r"^```(?:json)?\s*", "", text)
    t = re.sub(r"\s*```\s*$", "", t)
    if not t.startswith("{"):
        i = t.find("{")
        if i >= 0:
            t = t[i:]
    return json.loads(t)


async def _codex_fetch_prompt(spec: SourceSpec, prompt: str) -> dict:
    started = time.time()
    log.info("subagent_start", source=spec.id, backend="codex", hint=spec.allowed_tools)
    raw = await _run_codex(prompt, AGENT_TIMEOUT_S)
    elapsed = int(time.time() - started)
    try:
        data = _strip_to_json(raw)
    except json.JSONDecodeError as exc:
        log.error("subagent_parse_failed", source=spec.id,
                  err=str(exc), preview=raw[:200])
        raise RuntimeError(f"non-JSON response: {raw[:200]}") from exc
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:500])
    log.info("subagent_done", source=spec.id, seconds=elapsed,
             item_count=len(data.get("items", []) or []) +
                       len(data.get("mrs", []) or []) +
                       len(data.get("issues", []) or []))
    return data


def _render_source_prompt(spec: SourceSpec, period_days: int) -> str:
    return spec.prompt_template.format(
        user=USER,
        full_name=USER_FULL_NAME,
        period_days=period_days,
        **period_window(period_days),
    )


async def codex_fetcher(spec: SourceSpec, period_days: int) -> dict:
    """Generic fetcher for a Codex/app-backed SourceSpec."""
    return await _codex_fetch_prompt(spec, _render_source_prompt(spec, period_days))


async def outlook_fetcher(spec: SourceSpec, period_days: int) -> dict:
    """Fetch Outlook with a list-only retry for invalid OData filter drift."""
    prompt = _render_source_prompt(spec, period_days)
    try:
        return await _codex_fetch_prompt(spec, prompt)
    except RuntimeError as exc:
        msg = str(exc)
        retryable = (
            "ErrorInvalidUrlQueryFilter",
            "INVALID_ARGUMENT",
            "Transport send error",
            "Unexpected content type",
            "upstream connect error",
            "connection timeout",
        )
        if not any(term in msg for term in retryable):
            raise
        log.warning("outlook_retry_list_only", err=msg[:300])
        retry_prompt = (
            "The previous Outlook attempt failed because it used an invalid "
            "filter/OData query. Retry from scratch. Use only "
            "`microsoft outlook email_list_messages` with `top=40`, "
            '`order_by="receivedDateTime desc"`, and NO filter argument. '
            "Do not call search tools. Do not return the previous error.\n\n"
            + prompt
        )
        return await _codex_fetch_prompt(spec, retry_prompt)


async def _jira_search(
    client: httpx.AsyncClient,
    token: str,
    jql: str,
    reason: str,
) -> tuple[str, dict]:
    res = await client.post(
        JIRA_MCP_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": reason,
            "method": "tools/call",
            "params": {
                "name": "jira_search",
                "arguments": {
                    "jql": jql,
                    "top_k": 50,
                    "fields": [
                        "summary",
                        "status",
                        "priority",
                        "duedate",
                        "updated",
                        "updatedDate",
                        "project",
                    ],
                    "expand_comments": False,
                    "expand_changelog": False,
                    "full_text": False,
                },
            },
        },
        timeout=90.0,
    )
    if res.status_code == 401:
        raise RuntimeError(
            "Jira MCP auth token expired or invalid; run "
            "`./scripts/mac-reauth-and-sync.sh 1xA100-40` from the Mac, "
            "then retry."
        )
    res.raise_for_status()
    rpc = res.json()
    if "error" in rpc:
        raise RuntimeError(str(rpc["error"])[:500])
    content = ((rpc.get("result") or {}).get("content") or [{}])[0]
    payload = json.loads(content.get("text") or "{}")
    if not payload.get("success"):
        raise RuntimeError(str(payload.get("error") or payload)[:500])
    return reason, payload


async def jira_fetcher(_spec: SourceSpec, _period_days: int) -> dict:
    """Fetch Jira directly via MCP JSON-RPC to avoid Codex toolset drift."""
    started = time.time()
    token = (
        codex_mcp_token_env().get("AGENT_ME_MCP_TOKEN_MAAS_JIRA")
        or os.environ.get("AGENT_ME_MCP_TOKEN_MAAS_JIRA")
    )
    if not token:
        raise RuntimeError("AGENT_ME_MCP_TOKEN_MAAS_JIRA is not available")

    log.info("subagent_start", source="jira", backend="mcp-jsonrpc",
             hint="maas-jira")
    searches = [
        (
            "assignee",
            "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC",
        ),
        (
            "assignee",
            f'assignee = "{USER}" AND statusCategory != Done ORDER BY updated DESC',
        ),
        (
            "reporter",
            "reporter = currentUser() AND statusCategory != Done ORDER BY updated DESC",
        ),
        (
            "reporter",
            f'reporter = "{USER}" AND statusCategory != Done ORDER BY updated DESC',
        ),
        ("watcher", 'watcher = currentUser() AND statusCategory != Done ORDER BY updated DESC'),
    ]
    items_by_key: dict[str, dict] = {}
    errors: list[str] = []
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(_jira_search(client, token, jql, reason) for reason, jql in searches),
            return_exceptions=True,
        )
    for result in results:
        if isinstance(result, Exception):
            errors.append(str(result)[:200])
            continue
        reason, payload = result
        for row in _jira_rows(payload):
            item = _normalize_jira(row, reason)
            key = str(item.get("key") or "")
            if not key:
                continue
            if key in items_by_key:
                existing = str(items_by_key[key].get("reason") or "")
                if reason not in existing.split("|"):
                    items_by_key[key]["reason"] = f"{existing}|{reason}" if existing else reason
                continue
            items_by_key[key] = item

    if errors:
        log.warning("jira_search_partial_errors", count=len(errors),
                    first=errors[0])
    if errors and not items_by_key:
        raise RuntimeError("; ".join(errors)[:500])

    items = list(items_by_key.values())
    items.sort(key=lambda item: str(item.get("updated") or ""), reverse=True)
    elapsed = int(time.time() - started)
    log.info("subagent_done", source="jira", seconds=elapsed,
             item_count=len(items))
    return {"items": items}


async def _gitlab_list_merge_requests(
    client: httpx.AsyncClient,
    token: str,
    *,
    reason: str,
    role: str,
    state: str,
    updated_after: str | None = None,
) -> tuple[str, dict]:
    arguments: dict[str, str] = {
        "scope": "me",
        "state": state,
        "role": role,
    }
    if updated_after:
        arguments["updated_after"] = updated_after
    res = await client.post(
        GITLAB_MCP_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": role,
            "method": "tools/call",
            "params": {
                "name": "gitlab_list_merge_requests",
                "arguments": arguments,
            },
        },
        timeout=90.0,
    )
    if res.status_code == 401:
        raise RuntimeError(
            "GitLab MCP auth token expired or invalid; run "
            "`./scripts/mac-reauth-and-sync.sh 1xA100-40` from the Mac, "
            "then retry."
        )
    res.raise_for_status()
    rpc = res.json()
    if "error" in rpc:
        raise RuntimeError(str(rpc["error"])[:500])
    result = rpc.get("result") or {}
    if result.get("isError"):
        text = ((result.get("content") or [{}])[0]).get("text") or result
        raise RuntimeError(str(text)[:500])
    content = (result.get("content") or [{}])[0]
    payload = json.loads(content.get("text") or "{}")
    if payload.get("error"):
        raise RuntimeError(str(payload["error"])[:500])
    return reason, payload


async def gitlab_fetcher(_spec: SourceSpec, _period_days: int) -> dict:
    """Fetch GitLab MRs directly via MCP JSON-RPC using the server's real schema."""
    started = time.time()
    token = (
        codex_mcp_token_env().get("AGENT_ME_MCP_TOKEN_MAAS_GITLAB")
        or os.environ.get("AGENT_ME_MCP_TOKEN_MAAS_GITLAB")
    )
    if not token:
        raise RuntimeError("AGENT_ME_MCP_TOKEN_MAAS_GITLAB is not available")

    log.info("subagent_start", source="gitlab", backend="mcp-jsonrpc",
             hint="maas-gitlab")
    mrs_by_key: dict[tuple[str, str], dict] = {}
    errors: list[str] = []
    cutoff = local_now() - timedelta(days=3)
    updated_after = cutoff.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
    searches = [
        ("authored_waiting_review", "author", "opened", None),
        ("review_requested", "reviewer", "opened", None),
        ("recently_merged", "author", "merged", updated_after),
        ("recently_merged", "reviewer", "merged", updated_after),
        ("recently_merged", "assignee", "merged", updated_after),
    ]
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(
                _gitlab_list_merge_requests(
                    client,
                    token,
                    reason=reason,
                    role=role,
                    state=state,
                    updated_after=updated_after_value,
                )
                for reason, role, state, updated_after_value in searches
            ),
            return_exceptions=True,
        )
    for result in results:
        if isinstance(result, Exception):
            errors.append(str(result)[:200])
            continue
        reason, payload = result
        for row in _gitlab_mrs(payload):
            item = _normalize_gitlab_mr(row, reason)
            if reason == "recently_merged":
                merged_dt = parse_datetime(str(item.get("updated_at") or ""))
                if not merged_dt or merged_dt < cutoff:
                    continue
            key = (str(item.get("group") or ""), str(item.get("iid") or ""))
            if key in mrs_by_key:
                existing = str(mrs_by_key[key].get("reason") or "")
                if reason not in existing.split("|"):
                    mrs_by_key[key]["reason"] = (
                        f"{existing}|{reason}" if existing else reason
                    )
                continue
            mrs_by_key[key] = item

    if errors:
        log.warning("gitlab_mr_partial_errors", count=len(errors),
                    first=errors[0])
    if errors and not mrs_by_key:
        raise RuntimeError("; ".join(errors)[:500])

    mrs = list(mrs_by_key.values())
    mrs.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    elapsed = int(time.time() - started)
    log.info("subagent_done", source="gitlab", seconds=elapsed,
             item_count=len(mrs))
    return {"mrs": mrs, "issues": []}


async def _nvbugs_search_v2(client: httpx.AsyncClient, token: str, query: str) -> dict:
    res = await client.post(
        NVBUGS_MCP_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": query,
            "method": "tools/call",
            "params": {
                "name": "nvbugs_search_v2",
                "arguments": {
                    "query": query,
                    "search_type": "structured",
                    "max_results": 50,
                },
            },
        },
        timeout=90.0,
    )
    if res.status_code == 401:
        raise RuntimeError(
            "NVBugs MCP auth token expired or invalid; run "
            "`./scripts/mac-reauth-and-sync.sh 1xA100-40` from the Mac, "
            "then retry."
        )
    res.raise_for_status()
    rpc = res.json()
    if "error" in rpc:
        raise RuntimeError(str(rpc["error"])[:500])
    content = ((rpc.get("result") or {}).get("content") or [{}])[0]
    payload = json.loads(content.get("text") or "{}")
    if not payload.get("success"):
        raise RuntimeError(str(payload.get("error") or payload)[:500])
    return payload


async def nvbugs_fetcher(_spec: SourceSpec, _period_days: int) -> dict:
    """Fetch NVBugs directly via MCP JSON-RPC to avoid tool discovery drift."""
    started = time.time()
    token = (
        codex_mcp_token_env().get("AGENT_ME_MCP_TOKEN_MAAS_NVBUGS")
        or os.environ.get("AGENT_ME_MCP_TOKEN_MAAS_NVBUGS")
    )
    if not token:
        raise RuntimeError("AGENT_ME_MCP_TOKEN_MAAS_NVBUGS is not available")

    log.info("subagent_start", source="nvbugs", backend="mcp-jsonrpc",
             hint="maas-nvbugs")
    queries = [
        ("qa_eng", f'Show open bugs where QAEngineerFullName = "{USER_FULL_NAME}"'),
        ("arb", f'Show open bugs where ActionReqByFullName = "{USER_FULL_NAME}"'),
    ]
    items_by_id: dict[str, dict] = {}
    async with httpx.AsyncClient() as client:
        for reason, query in queries:
            payload = await _nvbugs_search_v2(client, token, query)
            for row in _nvbugs_rows(payload):
                item = _normalize_nvbug(row, reason)
                bug_id = str(item.get("id") or "")
                if bug_id and bug_id not in items_by_id:
                    items_by_id[bug_id] = item

    items = list(items_by_id.values())
    items.sort(key=lambda item: str(item.get("updated") or ""), reverse=True)
    elapsed = int(time.time() - started)
    log.info("subagent_done", source="nvbugs", seconds=elapsed,
             item_count=len(items))
    return {"items": items}


async def github_fetcher(_spec: SourceSpec, _period_days: int) -> dict:
    """GitHub via gh CLI. Returns same-shape dict for the parser."""
    started = time.time()
    log.info("subagent_start", source="github", method="gh CLI")

    async def gh(*args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            log.warning("gh_subcmd_failed", args=list(args),
                        err=err.decode(errors="replace")[:200])
            return "[]"
        return out.decode(errors="replace") or "[]"

    fields = "number,title,url,repository,updatedAt,state"
    issues, prs_authored, prs_reviewing = await asyncio.gather(
        gh("search", "issues", "--assignee", "@me", "--state", "open",
           "--limit", "20", "--json", fields),
        gh("search", "prs", "--author", "@me", "--state", "open",
           "--limit", "20", "--json", fields),
        gh("search", "prs", "--review-requested", "@me", "--state", "open",
           "--limit", "20", "--json", fields),
    )

    def safe(s: str) -> list[dict]:
        try:
            return json.loads(s)
        except Exception:
            return []

    elapsed = int(time.time() - started)
    payload = {
        "issues": safe(issues),
        "prs_authored": safe(prs_authored),
        "prs_reviewing": safe(prs_reviewing),
    }
    log.info("subagent_done", source="github", seconds=elapsed,
             item_count=sum(len(v) for v in payload.values()))
    return payload


def parse_github(data: dict, _spec: SourceSpec) -> list[BriefItem]:
    def repo_name(d: dict) -> str:
        repo = d.get("repository") or {}
        if isinstance(repo, dict):
            return str(repo.get("nameWithOwner", "")) or str(repo.get("name", ""))
        return str(repo)

    items: list[BriefItem] = []
    for ghi in data.get("issues", []) or []:
        items.append(BriefItem(
            source="github", icon="🐱",
            item_id=f"#{ghi.get('number', '?')}",
            title=str(ghi.get("title", ""))[:200],
            url=str(ghi.get("url", "")),
            group=repo_name(ghi) or "uncategorized",
            reason="assignee",
            status=ghi.get("state"),
            last_activity=ghi.get("updatedAt"),
            extras={"kind": "issue"},
        ))
    for prs_key, reason in (("prs_authored", "author"), ("prs_reviewing", "reviewer")):
        for pr in data.get(prs_key, []) or []:
            items.append(BriefItem(
                source="github", icon="🐱",
                item_id=f"#{pr.get('number', '?')}",
                title=str(pr.get("title", ""))[:200],
                url=str(pr.get("url", "")),
                group=repo_name(pr) or "uncategorized",
                reason=reason,
                status=pr.get("state"),
                last_activity=pr.get("updatedAt"),
                extras={"kind": "PR"},
            ))
    return items


SOURCES: list[SourceSpec] = [
    SourceSpec(id="jira", label="Jira", icon="📋",
               fetcher=jira_fetcher, parser=parse_jira,
               allowed_tools="maas-jira",
               prompt_template=JIRA_PROMPT),
    SourceSpec(id="gitlab", label="GitLab", icon="🦊",
               fetcher=gitlab_fetcher, parser=parse_gitlab,
               allowed_tools="maas-gitlab",
               prompt_template=GITLAB_PROMPT),
    SourceSpec(id="nvbugs", label="NVBugs", icon="🐛",
               fetcher=nvbugs_fetcher, parser=parse_nvbugs,
               allowed_tools="maas-nvbugs",
               prompt_template=NVBUGS_PROMPT),
    SourceSpec(id="slack", label="Slack", icon="💬",
               fetcher=codex_fetcher, parser=parse_slack,
               allowed_tools="codex-slack-app",
               prompt_template=SLACK_PROMPT),
    SourceSpec(id="outlook", label="Outlook", icon="📧",
               fetcher=outlook_fetcher, parser=parse_outlook,
               allowed_tools="codex-outlook-email-app",
               prompt_template=OUTLOOK_PROMPT),
    SourceSpec(id="calendar", label="Outlook Calendar", icon="📅",
               fetcher=codex_fetcher, parser=parse_calendar,
               allowed_tools="codex-outlook-calendar-app",
               prompt_template=CALENDAR_PROMPT),
    SourceSpec(id="github", label="GitHub", icon="🐱",
               fetcher=github_fetcher, parser=parse_github),
]


@dataclass
class SubagentResult:
    spec: SourceSpec
    items: list[BriefItem]
    error: str | None
    seconds: int


# ── Block-Kit builders ────────────────────────────────────────────────

def _format_item_line(i: BriefItem) -> str:
    label = md_link(f"{i.item_id} {i.title}".strip(), i.url) if i.item_id else md_link(i.title, i.url)
    bits: list[str] = []
    if event_time := fmt_event_time(i.extras.get("start"), i.extras.get("end")):
        bits.append(event_time)
    if i.priority:
        bits.append(f"*[{i.priority}]*")
    if i.status:
        bits.append(f"_{i.status}_")
    if i.deadline:
        d = fmt_due(i.deadline)
        if d:
            bits.append(f"due {d}")
    if i.last_activity:
        a = fmt_age(i.last_activity)
        if a:
            bits.append(f"upd {a}")
    if i.reason:
        bits.append(i.reason)
    if organizer := i.extras.get("organizer"):
        bits.append(str(organizer)[:60])
    if location := i.extras.get("location"):
        bits.append(str(location)[:80])
    if summary := i.extras.get("body_summary"):
        bits.append(str(summary)[:140])
    if v := i.extras.get("kind"):
        bits.append(str(v))
    line = f"  • {label}"
    if bits:
        line += " · " + " · ".join(bits)
    return line


def _group_items(items: list[BriefItem]) -> list[dict]:
    by_group: dict[str, list[BriefItem]] = {}
    for i in items:
        by_group.setdefault(i.group or "uncategorized", []).append(i)
    today = local_today()
    soon = today + timedelta(days=PRIORITY_WINDOW_DAYS)
    out: list[dict] = []
    for name, gitems in by_group.items():
        def sort_key(x: BriefItem):
            if start_dt := parse_datetime(x.extras.get("start")):
                return (start_dt.date(), int(start_dt.timestamp()))
            d = parse_date(x.deadline)
            return (d or date.max, -(parse_date(x.last_activity) or date.min).toordinal())
        gitems_sorted = sorted(gitems, key=sort_key)
        due_soon = sum(1 for x in gitems if (d := parse_date(x.deadline)) and d <= soon)
        out.append({"name": name, "count": len(gitems),
                    "due_soon": due_soon, "items": gitems_sorted})
    out.sort(key=lambda g: (-g["due_soon"], -g["count"], g["name"].lower()))
    return out


def build_source_blocks(result: SubagentResult) -> list[dict]:
    """One subagent's threaded reply: header + groups, splitting if long."""
    spec = result.spec
    if result.error:
        return [{
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"{spec.icon} *{spec.label}* — ❌ fetch failed in {result.seconds}s\n"
                             f"```{result.error[:500]}```"},
        }]

    n = len(result.items)
    if n == 0:
        return [{
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"{spec.icon} *{spec.label}* — ✓ nothing pending ({result.seconds}s)"},
        }]

    groups = _group_items(result.items)
    summary = (f"{spec.icon} *{spec.label}* — {n} item(s) "
               f"across {len(groups)} group(s) · {result.seconds}s")
    blocks: list[dict] = []
    rows: list[str] = [summary, ""]

    def flush() -> None:
        text = "\n".join(rows).rstrip()
        if text.strip():
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    for g in groups:
        counts = f"{g['count']}"
        if g["due_soon"]:
            counts += f" · *{g['due_soon']} due ≤{PRIORITY_WINDOW_DAYS}d*"
        block = [f"📁 *{g['name']}*  ({counts})"]
        for i in g["items"][:ITEMS_PER_GROUP]:
            block.append(_format_item_line(i))
        if g["count"] > ITEMS_PER_GROUP:
            block.append(f"  _+ {g['count'] - ITEMS_PER_GROUP} more_")
        block.append("")

        proposed = "\n".join(rows + block)
        if len(proposed) > SLACK_SECTION_MAX_CHARS and len(rows) > 2:
            flush()
            rows = [f"{spec.icon} *{spec.label}* (cont.)", "", *block]
        else:
            rows.extend(block)

    flush()
    return blocks


def build_priority_blocks(all_items: list[BriefItem]) -> list[dict]:
    today = local_today()
    soon = today + timedelta(days=PRIORITY_WINDOW_DAYS)
    priority_items = sorted(
        [i for i in all_items if (d := parse_date(i.deadline)) and d <= soon],
        key=lambda x: parse_date(x.deadline) or date.max,
    )
    if not priority_items:
        return [{
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"⏰ *Priorities (next {PRIORITY_WINDOW_DAYS}d)* — _none with deadlines._"},
        }]
    rows = [f"⏰ *Priorities (next {PRIORITY_WINDOW_DAYS}d)*", ""]
    for i in priority_items[:12]:
        due = fmt_due(i.deadline) or "-"
        label = md_link(f"{i.item_id} {i.title}".strip(), i.url) if i.item_id else md_link(i.title, i.url)
        prio = f" *[{i.priority}]*" if i.priority else ""
        status = f" · _{i.status}_" if i.status else ""
        grp = f" · `{i.group}`" if i.group and i.group != "uncategorized" else ""
        rows.append(f"• `{due}` — {i.icon} {label}{prio}{grp}{status}")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(rows)}}]


def _plain_item_line(i: BriefItem) -> str:
    label = md_link(f"{i.item_id} {i.title}".strip(), i.url) if i.item_id else md_link(i.title, i.url)
    bits: list[str] = []
    if event_time := fmt_event_time(i.extras.get("start"), i.extras.get("end")):
        bits.append(event_time)
    if i.priority:
        bits.append(f"[{i.priority}]")
    if i.status:
        bits.append(i.status)
    if i.deadline and (d := fmt_due(i.deadline)):
        bits.append(f"due {d}")
    if i.last_activity and (a := fmt_age(i.last_activity)):
        bits.append(f"upd {a}")
    if i.reason:
        bits.append(i.reason)
    if organizer := i.extras.get("organizer"):
        bits.append(str(organizer)[:60])
    if location := i.extras.get("location"):
        bits.append(str(location)[:80])
    if summary := i.extras.get("body_summary"):
        bits.append(str(summary)[:140])
    suffix = f" · {' · '.join(bits)}" if bits else ""
    return f"• {label}{suffix}"


def build_connector_mirror_text(period: str, results: list[SubagentResult],
                                total_seconds: int) -> str:
    preset = PERIOD_PRESETS[period]
    total = sum(len(r.items) for r in results)
    rows = [
        f"📅 *{preset['title']} — {local_today().isoformat()}*",
        f"_agent-me mirror · {total} item(s) · {total_seconds}s_",
        "",
    ]
    for result in results:
        spec = result.spec
        if result.error:
            rows.append(f"{spec.icon} *{spec.label}* — ❌ fetch failed: `{result.error[:180]}`")
            rows.append("")
            continue
        if not result.items:
            rows.append(f"{spec.icon} *{spec.label}* — ✓ nothing pending")
            rows.append("")
            continue
        rows.append(f"{spec.icon} *{spec.label}* — {len(result.items)} item(s)")
        for item in result.items[:ITEMS_PER_GROUP]:
            rows.append(_plain_item_line(item))
        if len(result.items) > ITEMS_PER_GROUP:
            rows.append(f"_+ {len(result.items) - ITEMS_PER_GROUP} more in the source thread_")
        rows.append("")

    text = "\n".join(rows).strip()
    if len(text) <= 4800:
        return text
    return text[:4700].rstrip() + "\n\n_…truncated for Slack connector mirror; see source thread for full split._"


async def send_connector_slack_mirror(email: str, message: str) -> ConnectorMirrorResult:
    prompt = f"""Return ONLY a JSON object. No prose, no markdown fences.

The user explicitly requested this Slack send. Use the Codex Slack connector
app tools only. Do not use shell commands. Do not use SLACK_BOT_TOKEN. This
turn is running through Codex app-server auto-review because it is a
permissioned connector write.

Destination: Slack DM for exact email `{email}`.

Steps:
1. Search Slack users for the exact email `{email}` using the Slack connector user search tool.
2. Pick the exact email match and get its Slack user_id.
3. Send the message below immediately as a DM using the Slack connector send-message tool.
   Do not create a draft. Do not ask for confirmation.
4. Return exactly:
   {{"ok": true, "user_id": "...", "link": "..."}}
   or, if the exact email cannot be resolved or send fails:
   {{"ok": false, "error": "..."}}

Message JSON string:
{json.dumps(message, ensure_ascii=False)}
"""
    try:
        raw = await _run_codex_app_server(prompt, AGENT_TIMEOUT_S)
    except Exception as exc:
        return ConnectorMirrorResult(ok=False, error=str(exc))
    try:
        data = _strip_to_json(raw)
    except Exception:
        return ConnectorMirrorResult(ok=False, error="connector mirror returned non-JSON", raw=raw)
    return ConnectorMirrorResult(
        ok=bool(data.get("ok")),
        error=None if data.get("ok") else str(data.get("error") or "unknown connector mirror error"),
        raw=raw,
    )


def build_root_blocks(period: str, results: list[SubagentResult],
                      total_seconds: int) -> list[dict]:
    preset = PERIOD_PRESETS[period]
    today = local_today()
    total = sum(len(r.items) for r in results)
    by_source = " · ".join(
        f"*{len(r.items)}* {r.spec.id}" for r in sorted(results, key=lambda r: -len(r.items)) if r.items
    ) or "no items"
    err_count = sum(1 for r in results if r.error)
    err_line = f" · ⚠️ {err_count} fetch errors" if err_count else ""

    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f"📅 {preset['title']} — {today.strftime('%a %Y-%m-%d')}"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": (f"_{local_now().strftime('%-I:%M %p')} · "
                                f"{total} total · {by_source} · {total_seconds}s wall-clock"
                                f"{err_line}_")}]},
        {"type": "actions",
         "elements": [
             {"type": "button",
              "text": {"type": "plain_text", "text": "🔄 Refresh"},
              "action_id": "menu_brief_day", "value": "day"},
             {"type": "button",
              "text": {"type": "plain_text", "text": "📅 Weekly"},
              "action_id": "menu_brief_week", "value": "week"},
             {"type": "button",
              "text": {"type": "plain_text", "text": "📆 Monthly"},
              "action_id": "menu_brief_month", "value": "month"},
             {"type": "button",
              "text": {"type": "plain_text", "text": "🔧 Reauth"},
              "action_id": "brief_reauth"},
         ]},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": ("_Each source posted as a threaded reply ↓_  ·  "
                                "`/brief week|month` for other periods")}]},
    ]
    return blocks


def post_root_message(
    client: WebClient,
    *,
    channel: str,
    text: str,
    label: str,
    thread_ts: str | None = None,
) -> SlackDestination:
    kwargs: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    res = client.chat_postMessage(**kwargs)
    return SlackDestination(
        label=label,
        channel=channel,
        thread_ts=thread_ts,
        root_ts=res["ts"],
    )


# ── Orchestration ─────────────────────────────────────────────────────

async def run_subagent(spec: SourceSpec, period_days: int) -> SubagentResult:
    started = time.time()
    try:
        data = await spec.fetcher(spec, period_days)
        items = spec.parser(data, spec)
        return SubagentResult(spec=spec, items=items, error=None,
                              seconds=int(time.time() - started))
    except Exception as exc:
        log.error("subagent_failed", source=spec.id, err=str(exc))
        return SubagentResult(spec=spec, items=[], error=str(exc),
                              seconds=int(time.time() - started))


async def main_async(
    period: str = "day",
    dry_run: bool = False,
    channel_override: str | None = None,
    thread_ts_override: str | None = None,
    mirror_email: str | None = None,
) -> int:
    preset = PERIOD_PRESETS.get(period)
    if not preset:
        log.error("unknown period", period=period, valid=list(PERIOD_PRESETS))
        return 2
    started_at = time.time()
    log.info("brief_starting", repo_dir=str(REPO_DIR), period=period,
             days=preset["days"], n_subagents=len(SOURCES))

    # ── Resolve Slack target + post root header ─────────────────────────
    destinations: list[SlackDestination] = []
    client: WebClient | None = None
    target: str | None = None
    if not dry_run:
        bot_token = os.environ.get("SLACK_BOT_TOKEN")
        target = channel_override or os.environ.get("SLACK_ALLOWED_USER_ID")
        if not bot_token or "REPLACE-ME" in bot_token:
            log.error("missing SLACK_BOT_TOKEN")
            return 2
        if not target:
            log.error("no target — set SLACK_ALLOWED_USER_ID or use --channel")
            return 2
        client = WebClient(token=bot_token)
        if target.startswith("U"):
            try:
                opened = client.conversations_open(users=target)
                root_channel = opened["channel"]["id"]
            except Exception as exc:
                log.error("conversations_open_failed", err=str(exc))
                return 3
        else:
            root_channel = target

        root_text = (
            f"📅 *{preset['title']} — {local_today().isoformat()}*\n"
            f"🔄 _Fanning out {len(SOURCES)} subagents in parallel "
            "(jira / gitlab / nvbugs / slack / outlook / calendar / github)…_\n"
            "_Each platform will post as its own message._"
        )
        try:
            primary = post_root_message(
                client,
                channel=root_channel,
                thread_ts=thread_ts_override,
                text=root_text,
                label="primary",
            )
            destinations.append(primary)
            log.info("root_header_posted", channel=root_channel, ts=primary.root_ts,
                     thread_ts=thread_ts_override, label="primary")
        except Exception as exc:
            log.error("root_post_failed", err=str(exc))
            return 3

        if mirror_email or DEFAULT_MIRROR_EMAIL:
            log.info("connector_mirror_enabled", email=mirror_email or DEFAULT_MIRROR_EMAIL)
        else:
            log.info("connector_mirror_disabled")

    # ── Fan out subagents in parallel ───────────────────────────────────
    # Each post is serialized through this lock so we don't hit Slack with
    # 7 simultaneous chat.postMessage calls and trip the per-channel rate
    # limit. Fetches still run in parallel; only the post step waits.
    post_lock = asyncio.Lock()

    async def run_and_reply(spec: SourceSpec) -> SubagentResult:
        result = await run_subagent(spec, preset["days"])
        if dry_run or not (client and destinations):
            return result
        blocks = build_source_blocks(result)
        async with post_lock:
            for dest in destinations:
                try:
                    client.chat_postMessage(
                        channel=dest.channel,
                        thread_ts=dest.reply_thread_ts,
                        text=(f"{spec.icon} {spec.label} — "
                              f"{'❌ failed' if result.error else f'{len(result.items)} item(s)'}"),
                        blocks=blocks,
                    )
                except Exception as exc:
                    log.warning("threaded_reply_failed", label=dest.label,
                                source=spec.id, err=str(exc)[:300])
        return result

    results: list[SubagentResult] = await asyncio.gather(
        *[run_and_reply(s) for s in SOURCES]
    )

    total_seconds = int(time.time() - started_at)
    total_items = sum(len(r.items) for r in results)
    err_count = sum(1 for r in results if r.error)
    log.info("fan_out_done", total_items=total_items, err_count=err_count,
             total_seconds=total_seconds,
             per_source={r.spec.id: (len(r.items), r.seconds) for r in results})

    # ── Synthesise priority list across all sources, post as last reply ─
    all_items = [i for r in results for i in r.items]
    priority_blocks = build_priority_blocks(all_items)
    if not dry_run and client and destinations:
        for dest in destinations:
            try:
                client.chat_postMessage(
                    channel=dest.channel, thread_ts=dest.reply_thread_ts,
                    text="⏰ Priorities", blocks=priority_blocks,
                )
            except Exception as exc:
                log.warning("priority_reply_failed", label=dest.label, err=str(exc)[:300])

    # ── Update root header with final summary + buttons ─────────────────
    final_root = build_root_blocks(period, results, total_seconds)
    if not dry_run and client and destinations:
        fallback = (f"{preset['title']} — {local_today().isoformat()} "
                    f"({total_items} items, {total_seconds}s)")
        for dest in destinations:
            try:
                client.chat_update(
                    channel=dest.channel, ts=dest.root_ts,
                    text=fallback, blocks=final_root,
                )
                log.info("root_header_updated", label=dest.label, ts=dest.root_ts,
                         total_items=total_items, total_seconds=total_seconds)
            except Exception as exc:
                log.warning("root_update_failed", label=dest.label, err=str(exc)[:300])

    connector_mirror_email = mirror_email or DEFAULT_MIRROR_EMAIL
    if not dry_run and connector_mirror_email:
        mirror_text = build_connector_mirror_text(period, results, total_seconds)
        log.info("connector_mirror_start", email=connector_mirror_email,
                 chars=len(mirror_text))
        mirror_result = await send_connector_slack_mirror(connector_mirror_email, mirror_text)
        if mirror_result.ok:
            log.info("connector_mirror_sent", email=connector_mirror_email)
        else:
            log.warning("connector_mirror_failed", email=connector_mirror_email,
                        err=(mirror_result.error or "")[:300],
                        preview=mirror_result.raw[:300])

    if dry_run:
        # Print everything as JSON so a human / shell test can diff it.
        soon = local_today() + timedelta(days=PRIORITY_WINDOW_DAYS)
        upcoming = [i for i in all_items
                    if (d := parse_date(i.deadline)) and d <= soon]
        upcoming_sorted = sorted(upcoming,
                                 key=lambda x: parse_date(x.deadline) or date.max)
        out = {
            "period": period,
            "total_items": total_items,
            "total_seconds": total_seconds,
            "err_count": err_count,
            "sources": {
                r.spec.id: {
                    "seconds": r.seconds,
                    "error": r.error,
                    "item_count": len(r.items),
                    "items": [i.__dict__ for i in r.items[:50]],
                }
                for r in results
            },
            "priorities": [
                {"icon": i.icon, "id": i.item_id, "title": i.title,
                 "url": i.url, "deadline": i.deadline, "group": i.group,
                 "source": i.source, "priority": i.priority}
                for i in upcoming_sorted[:12]
            ],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        log.info("dry_run_done", total_items=total_items, total_seconds=total_seconds)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="agent-me brief — fan-out subagents")
    parser.add_argument("--period", default="day",
                        choices=list(PERIOD_PRESETS.keys()),
                        help="time window for 'recently active' (default: day)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print per-source JSON, don't post to Slack")
    parser.add_argument("--channel", help="override target channel id (default: operator DM)")
    parser.add_argument("--thread-ts", help="post all platform messages into this Slack thread")
    parser.add_argument("--mirror-email", help="also mirror the brief into this Slack user's DM by email")
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(period=args.period, dry_run=args.dry_run,
                                      channel_override=args.channel,
                                      thread_ts_override=args.thread_ts,
                                      mirror_email=args.mirror_email))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
