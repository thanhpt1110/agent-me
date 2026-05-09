"""agent-me brief — aggregate work across all infra and post to Slack.

Run with:
    uv run agent-me-brief                # daily (today's outlook)
    uv run agent-me-brief --period week  # weekly recap
    uv run agent-me-brief --period month # monthly recap
    uv run agent-me-brief --dry-run      # print Block Kit JSON, don't post
    uv run agent-me-brief --channel C123 # post to a specific channel id

Why on-demand (not cron): MAAS-MCP tokens expire ~24h and need a manual
browser-based SSO refresh, so a true 8am cron would silently fail on
stale-token mornings. Pattern instead: bridge DMs you when MCPs go
stale, you `/reauth`, then `/brief` (or `/brief weekly`).

Sources:
- Jira (mcp__maas-jira__jira_search)
- GitLab MRs / Issues (mcp__maas-gitlab__*)
- NVBugs (mcp__maas-nvbugs__nvbugs_search_v2)
- Confluence (mcp__maas-confluence__confluence_search)
- GitHub (`gh` CLI directly)
- Email — skipped v1 (no Outlook MCP)

Each source's fetch is best-effort; if one fails the brief still posts
with what's available plus a "fetch failed" footer pointing you at
`/reauth`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from dotenv import load_dotenv
from slack_sdk import WebClient

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

# ── Config ───────────────────────────────────────────────────────────────

# Per-tool allow-list for the read-only fetch. We avoid `mcp__*` because
# top-level wildcard doesn't match in claude --allowedTools.
ALLOWED_TOOLS = " ".join((
    "mcp__maas-jira__jira_search",
    "mcp__maas-jira__jira_get_issue",
    "mcp__maas-gitlab__gitlab_list_merge_requests",
    "mcp__maas-gitlab__gitlab_list_issues",
    "mcp__maas-gitlab__gitlab_list_projects",
    "mcp__maas-gitlab__gitlab_get_project_details",
    "mcp__maas-nvbugs__nvbugs_search_v2",
    "mcp__maas-nvbugs__nvbugs_get_bug_details_v2",
    "mcp__maas-confluence__confluence_search",
))

CLAUDE_TIMEOUT_S = 240.0
PRIORITY_WINDOW_DAYS = 7
MAX_PER_SECTION = 10

PERIOD_PRESETS = {
    "day":   {"days": 1,  "label": "Daily",   "title": "Daily Brief"},
    "week":  {"days": 7,  "label": "Weekly",  "title": "Weekly Brief"},
    "month": {"days": 30, "label": "Monthly", "title": "Monthly Brief"},
}


def claude_prompt_for(period_days: int) -> str:
    return f"""Return ONLY a JSON object — no markdown fences, no commentary, no preamble.

Schema (use empty arrays where a tool fails or returns nothing):
{{
  "jira": [{{"key": "NGC-123", "summary": "...", "status": "...", "priority": "...", "duedate": null, "url": "...", "updated": "..."}}],
  "gitlab_mrs": [{{"iid": 456, "title": "...", "state": "...", "web_url": "...", "milestone_due_date": null, "updated_at": "...", "project_path": "..."}}],
  "gitlab_issues": [{{"iid": 789, "title": "...", "state": "...", "web_url": "...", "due_date": null, "updated_at": "...", "project_path": "..."}}],
  "nvbugs": [{{"id": "1234567", "title": "...", "priority": "P0", "status": "Open", "due": null, "url": "https://nvbugs.nvidia.com/<id>"}}],
  "confluence": [{{"title": "...", "url": "...", "updated": "...", "reason": "mentioned|updated|watched"}}]
}}

Time window for "recently active": last {period_days} day(s).

Tools you may call (and only these):
- mcp__maas-jira__jira_search with JQL `assignee = currentUser() AND (statusCategory != Done OR resolved >= -{period_days}d)` ORDER BY duedate ASC, max 25.
- mcp__maas-gitlab__gitlab_list_merge_requests scope=assigned_to_me state=opened, max 15. Also include any merged within the last {period_days} day(s) if the API supports a state filter for that.
- mcp__maas-gitlab__gitlab_list_issues scope=assigned_to_me state=opened, max 15.
- mcp__maas-nvbugs__nvbugs_search_v2 for bugs assigned to current user, status=Open or recently changed in last {period_days} days, top 15.
- mcp__maas-confluence__confluence_search for pages updated in last {period_days} day(s) that mention "thaphan", top 5.

Don't hallucinate items. If a tool errors, that source's array is []. JSON only."""


# ── Data model ──────────────────────────────────────────────────────────

@dataclass
class BriefItem:
    source: str        # "jira" | "gitlab" | "github" | "nvbugs" | "confluence"
    icon: str
    item_id: str
    title: str
    url: str
    status: str | None = None
    priority: str | None = None
    deadline: str | None = None        # ISO date
    last_activity: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# ── Source: claude → MCP fetch ──────────────────────────────────────────

async def fetch_via_claude(period_days: int) -> dict[str, list[dict]]:
    args = [
        "claude", "-p", claude_prompt_for(period_days),
        "--model", os.environ.get("CLAUDE_MODEL", "claude-opus-4-7"),
        "--dangerously-skip-permissions",
        "--allowedTools", ALLOWED_TOOLS,
    ]
    log.info("claude_spawn", cwd=str(REPO_DIR))
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(REPO_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"claude timed out after {CLAUDE_TIMEOUT_S}s")
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}: {stderr.decode(errors='replace')[:400]}"
        )

    text = stdout.decode(errors="replace").strip()
    # Strip optional ```json fences claude sometimes adds anyway.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    # Sometimes there's preamble before the JSON. Find the first '{'.
    if not text.startswith("{"):
        i = text.find("{")
        if i >= 0:
            text = text[i:]

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("json_parse_failed", err=str(exc), preview=text[:300])
        raise RuntimeError(f"claude returned non-JSON: {text[:200]}")


# ── Source: GitHub via gh CLI ───────────────────────────────────────────

async def fetch_github() -> dict[str, list[dict]]:
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
    issues = await gh("search", "issues", "--assignee", "@me", "--state", "open",
                      "--limit", "20", "--json", fields)
    prs_authored = await gh("search", "prs", "--author", "@me", "--state", "open",
                            "--limit", "20", "--json", fields)
    prs_reviewing = await gh("search", "prs", "--review-requested", "@me",
                             "--state", "open", "--limit", "20", "--json", fields)

    def safe(s: str) -> list[dict]:
        try:
            return json.loads(s)
        except Exception:
            return []

    return {
        "issues": safe(issues),
        "prs_authored": safe(prs_authored),
        "prs_reviewing": safe(prs_reviewing),
    }


# ── Build BriefItems ───────────────────────────────────────────────────

def parse_date(s: str | None) -> date | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    # Try common formats.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.replace("Z", ""), fmt.replace("Z", "")).date()
        except ValueError:
            continue
    # Try ISO with timezone offset
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def to_items(claude_data: dict, gh_data: dict) -> list[BriefItem]:
    items: list[BriefItem] = []

    for j in claude_data.get("jira", []) or []:
        items.append(BriefItem(
            source="jira", icon="📋",
            item_id=str(j.get("key", "?")),
            title=str(j.get("summary", ""))[:200],
            url=str(j.get("url", "")),
            status=j.get("status"),
            priority=j.get("priority"),
            deadline=j.get("duedate"),
            last_activity=j.get("updated"),
        ))

    for m in claude_data.get("gitlab_mrs", []) or []:
        items.append(BriefItem(
            source="gitlab", icon="🦊",
            item_id=f"!{m.get('iid', '?')}",
            title=str(m.get("title", ""))[:200],
            url=str(m.get("web_url", "")),
            status=m.get("state"),
            deadline=m.get("milestone_due_date"),
            last_activity=m.get("updated_at"),
            extras={"kind": "MR", "project": m.get("project_path") or ""},
        ))

    for i in claude_data.get("gitlab_issues", []) or []:
        items.append(BriefItem(
            source="gitlab", icon="🦊",
            item_id=f"#{i.get('iid', '?')}",
            title=str(i.get("title", ""))[:200],
            url=str(i.get("web_url", "")),
            status=i.get("state"),
            deadline=i.get("due_date"),
            last_activity=i.get("updated_at"),
            extras={"kind": "issue", "project": i.get("project_path") or ""},
        ))

    for nb in claude_data.get("nvbugs", []) or []:
        items.append(BriefItem(
            source="nvbugs", icon="🐛",
            item_id=str(nb.get("id", "?")),
            title=str(nb.get("title", ""))[:200],
            url=str(nb.get("url", "")),
            status=nb.get("status"),
            priority=nb.get("priority"),
            deadline=nb.get("due"),
        ))

    for c in claude_data.get("confluence", []) or []:
        items.append(BriefItem(
            source="confluence", icon="📚",
            item_id="",
            title=str(c.get("title", ""))[:200],
            url=str(c.get("url", "")),
            last_activity=c.get("updated"),
            extras={"reason": c.get("reason") or ""},
        ))

    def repo_name(d: dict) -> str:
        repo = d.get("repository") or {}
        if isinstance(repo, dict):
            return str(repo.get("nameWithOwner", "")) or str(repo.get("name", ""))
        return str(repo)

    for ghi in gh_data.get("issues", []) or []:
        items.append(BriefItem(
            source="github", icon="🐱",
            item_id=f"#{ghi.get('number', '?')}",
            title=str(ghi.get("title", ""))[:200],
            url=str(ghi.get("url", "")),
            status=ghi.get("state"),
            last_activity=ghi.get("updatedAt"),
            extras={"kind": "issue", "repo": repo_name(ghi)},
        ))
    for prs_key, kind in (("prs_authored", "PR (yours)"), ("prs_reviewing", "PR (review)")):
        for pr in gh_data.get(prs_key, []) or []:
            items.append(BriefItem(
                source="github", icon="🐱",
                item_id=f"#{pr.get('number', '?')}",
                title=str(pr.get("title", ""))[:200],
                url=str(pr.get("url", "")),
                status=pr.get("state"),
                last_activity=pr.get("updatedAt"),
                extras={"kind": kind, "repo": repo_name(pr)},
            ))

    return items


# ── Format helpers ─────────────────────────────────────────────────────

def fmt_due(deadline: str | None) -> str:
    d = parse_date(deadline)
    if not d:
        return ""
    today = date.today()
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
    delta = (date.today() - d).days
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


def md_link(text: str, url: str) -> str:
    if url:
        # Slack mrkdwn link: <url|text>; sanitize > to avoid breakage
        safe_text = text.replace("|", "·").replace(">", "›")[:120]
        return f"<{url}|{safe_text}>"
    return text[:120]


# ── Block Kit builder ──────────────────────────────────────────────────

def build_blocks(items: list[BriefItem], errors: list[str], period: str = "day") -> list[dict]:
    today = date.today()
    soon = today + timedelta(days=PRIORITY_WINDOW_DAYS)
    preset = PERIOD_PRESETS.get(period, PERIOD_PRESETS["day"])

    priority_items = sorted(
        [i for i in items if (d := parse_date(i.deadline)) and d <= soon],
        key=lambda x: parse_date(x.deadline) or date.max,
    )

    blocks: list[dict] = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"📅 {preset['title']} — {today.strftime('%a %Y-%m-%d')}"},
    })
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"_{datetime.now().strftime('%-I:%M %p')} · "
                f"{len(items)} item(s) across all sources_"
            ),
        }],
    })
    blocks.append({"type": "divider"})

    if priority_items:
        rows = [f"*⏰ Priorities by deadline (next {PRIORITY_WINDOW_DAYS} days)*", ""]
        for i in priority_items[:10]:
            due = fmt_due(i.deadline) or "-"
            label = md_link(f"{i.item_id} {i.title}", i.url) if i.item_id else md_link(i.title, i.url)
            prio = f" *[{i.priority}]*" if i.priority else ""
            status = f" · _{i.status}_" if i.status else ""
            rows.append(f"• `{due}` — {i.icon} {label}{prio}{status}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(rows)}})
        blocks.append({"type": "divider"})

    sources_order = [
        ("nvbugs", "🐛 NVBugs"),
        ("gitlab", "🦊 GitLab"),
        ("github", "🐱 GitHub"),
        ("jira", "📋 Jira"),
        ("confluence", "📚 Confluence"),
    ]
    for src_key, header in sources_order:
        src_items = [i for i in items if i.source == src_key]
        if not src_items:
            continue
        rows = [f"*{header} ({len(src_items)})*", ""]
        for i in src_items[:MAX_PER_SECTION]:
            label = md_link(f"{i.item_id} {i.title}", i.url) if i.item_id else md_link(i.title, i.url)
            bits: list[str] = []
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
            for k in ("kind", "repo", "project", "reason"):
                if v := i.extras.get(k):
                    bits.append(str(v))
            row = f"• {label}"
            if bits:
                row += " · " + " · ".join(bits)
            rows.append(row)
        if len(src_items) > MAX_PER_SECTION:
            rows.append(f"_… and {len(src_items) - MAX_PER_SECTION} more_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(rows)}})

    blocks.append({"type": "divider"})

    footer_bits = ["_📧 Email: skipped (no Outlook MCP yet)_"]
    if errors:
        footer_bits.append(f"⚠️ fetch errors: {len(errors)} (run `/reauth` if MCPs need re-auth)")
    footer_bits.append("`/brief` to refresh · `/mcp` for source health")
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": " · ".join(footer_bits)}],
    })

    return blocks


# ── Main ───────────────────────────────────────────────────────────────

async def main_async(period: str = "day", dry_run: bool = False,
                     channel_override: str | None = None) -> int:
    preset = PERIOD_PRESETS.get(period)
    if not preset:
        log.error("unknown period", period=period, valid=list(PERIOD_PRESETS))
        return 2
    log.info("brief_starting", repo_dir=str(REPO_DIR), period=period, days=preset["days"])

    errors: list[str] = []
    claude_task = asyncio.create_task(fetch_via_claude(preset["days"]))
    gh_task = asyncio.create_task(fetch_github())

    try:
        claude_data = await claude_task
    except Exception as exc:
        log.error("claude_fetch_failed", err=str(exc))
        errors.append(f"claude: {exc}")
        claude_data = {}

    try:
        gh_data = await gh_task
    except Exception as exc:
        log.error("gh_fetch_failed", err=str(exc))
        errors.append(f"github: {exc}")
        gh_data = {}

    items = to_items(claude_data, gh_data)
    by_source = {s: sum(1 for i in items if i.source == s)
                 for s in {i.source for i in items}}
    log.info("items_collected", count=len(items), by_source=by_source)

    blocks = build_blocks(items, errors, period=period)

    if dry_run:
        print(json.dumps(blocks, indent=2, ensure_ascii=False))
        log.info("dry_run_done", item_count=len(items))
        return 0

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token or "REPLACE-ME" in bot_token:
        log.error("missing SLACK_BOT_TOKEN")
        return 2

    target = channel_override or os.environ.get("SLACK_ALLOWED_USER_ID")
    if not target:
        log.error("no target — set SLACK_ALLOWED_USER_ID in configs/.env or use --channel")
        return 2

    client = WebClient(token=bot_token)
    if target.startswith("U"):
        opened = client.conversations_open(users=target)
        channel = opened["channel"]["id"]
    else:
        channel = target

    fallback = f"{preset['title']} — {date.today().isoformat()} ({len(items)} items)"
    res = client.chat_postMessage(channel=channel, text=fallback, blocks=blocks)
    log.info("posted", ok=res.get("ok"), ts=res.get("ts"), channel=channel,
             item_count=len(items))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="agent-me brief")
    parser.add_argument("--period", default="day",
                        choices=list(PERIOD_PRESETS.keys()),
                        help="time window for 'recently active' (default: day)")
    parser.add_argument("--dry-run", action="store_true", help="print blocks JSON, don't post")
    parser.add_argument("--channel", help="override target channel id (default: operator DM)")
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(period=args.period, dry_run=args.dry_run,
                                      channel_override=args.channel))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
