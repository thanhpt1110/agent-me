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
import time
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

# MCP-fetch backend selection. Two CLIs can drive the data fetch:
#   - claude (default): Claude Code CLI. Reliable JSON output, but MCP
#     auth via Claude Code expires ~daily (NVIDIA org policy disables
#     proactive refresh — see design/mcp-authentication.md).
#   - pa: NVIDIA's Personal Assistant CLI. Hypothesis (pending live
#     verification): better MCP auth retention + possibly faster fetch
#     because it's optimized for NVIDIA's MAAS endpoints. Override:
#     MCP_CLI=pa in configs/.env. Requires `pa login` once on the host.
#
# Bridge stays on Claude Code regardless — it handles Slack I/O,
# orchestration, scripting; PA only fetches MCP data when asked to.
MCP_CLI = os.environ.get("MCP_CLI", "claude").strip().lower() or "claude"

# Per-tool allow-list for the read-only fetch. We avoid `mcp__*` because
# top-level wildcard doesn't match in claude --allowedTools. PA has its
# own permission model and ignores this.
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
PA_TIMEOUT_S = 240.0
MCP_CLI_TIMEOUT_S = float(os.environ.get("MCP_CLI_TIMEOUT_S", CLAUDE_TIMEOUT_S))
PRIORITY_WINDOW_DAYS = 7
MAX_PER_SECTION = 10

PERIOD_PRESETS = {
    "day":   {"days": 1,  "label": "Daily",   "title": "Daily Brief"},
    "week":  {"days": 7,  "label": "Weekly",  "title": "Weekly Brief"},
    "month": {"days": 30, "label": "Monthly", "title": "Monthly Brief"},
}


def mcp_prompt_for(period_days: int) -> str:
    return f"""Return ONLY a JSON object — no markdown fences, no commentary, no preamble.

The user is `thaphan` (NVIDIA). Goal: surface EVERY ticket / MR / issue /
page where this user is mentioned (assignee, reporter, watcher, author,
review-requested, or named in text) AND that is NOT yet Done. Group items
by domain (project / repo / space) so the user can see scope per app.

Schema (use empty arrays where a tool fails or returns nothing). Every
item MUST include a `group` string used for grouping:

{{
  "jira": [
    {{"key": "NGC-123", "summary": "...", "status": "...", "priority": "...",
      "duedate": null, "url": "...", "updated": "...",
      "group": "NGC", "reason": "assignee|reporter|watcher|mentioned"}}
  ],
  "gitlab_mrs": [
    {{"iid": 456, "title": "...", "state": "...", "web_url": "...",
      "milestone_due_date": null, "updated_at": "...",
      "group": "team/repo-name", "reason": "assignee|author|reviewer"}}
  ],
  "gitlab_issues": [
    {{"iid": 789, "title": "...", "state": "...", "web_url": "...",
      "due_date": null, "updated_at": "...",
      "group": "team/repo-name", "reason": "assignee|author"}}
  ],
  "nvbugs": [
    {{"id": "1234567", "title": "...", "priority": "P0", "status": "Open",
      "due": null, "url": "https://nvbugs.nvidia.com/<id>",
      "group": "<module|component|product>", "reason": "assignee|mentioned"}}
  ],
  "confluence": [
    {{"title": "...", "url": "...", "updated": "...",
      "group": "<space-name>", "reason": "mentioned|updated|watched"}}
  ]
}}

Time window for activity: last {period_days} day(s).

Tools you may call (and only these):

1. mcp__maas-jira__jira_search — fetch with JQL covering all involvements:
   `(assignee = currentUser() OR reporter = currentUser() OR watcher = currentUser() OR text ~ "thaphan") AND statusCategory != Done`
   ORDER BY duedate ASC NULLS LAST, max 50.
   For each issue, set `group` = the project key (the part of the issue
   key before the dash, e.g. "NGC" from "NGC-789"). Set `reason` based
   on which clause matched (best-effort; use "assignee" if assignee == me,
   else "reporter" / "watcher" / "mentioned").

2. mcp__maas-gitlab__gitlab_list_merge_requests — call THREE TIMES and
   merge results, deduping by (project_id, iid):
   - scope=assigned_to_me state=opened, max 25
   - scope=created_by_me state=opened, max 25
   - scope=review_requested state=opened, max 25 (if supported)
   For each MR set `group` = the project's full path (e.g.
   "omniverse-ngc/quality-assurance"); `reason` = "assignee"/"author"/"reviewer".

3. mcp__maas-gitlab__gitlab_list_issues — call TWICE and merge:
   - scope=assigned_to_me state=opened, max 25
   - scope=created_by_me state=opened, max 25
   `group` = project path; `reason` = "assignee"/"author".

4. mcp__maas-nvbugs__nvbugs_search_v2 — bugs assigned to current user,
   status not in (Closed, Resolved, WontFix, Duplicate), top 30.
   `group` = module / component / product field if available; otherwise
   "uncategorized". `reason` = "assignee".

5. mcp__maas-confluence__confluence_search — pages updated in last
   {period_days} day(s) where the user is mentioned. Try query
   `"thaphan" updated >= -{period_days}d` (or the closest equivalent
   the tool accepts), top 15. `group` = the space key/name (e.g. "PASNT",
   "AI"). `reason` = "mentioned"/"updated"/"watched".

Important rules:
- Don't hallucinate items. If a tool errors, that source's array is [].
- If the API doesn't expose enough info for `group`, use "uncategorized".
- Don't filter to a top-N "most important" — return what the API gives
  (capped per the limits above). The Slack formatter will collapse long
  groups itself.
- Output JSON ONLY. No prose, no markdown fences."""


# ── Data model ──────────────────────────────────────────────────────────

@dataclass
class BriefItem:
    source: str        # "jira" | "gitlab" | "github" | "nvbugs" | "confluence"
    icon: str
    item_id: str
    title: str
    url: str
    group: str = "uncategorized"      # project/repo/space — sub-grouping within source
    reason: str | None = None          # "assignee" | "reporter" | "mentioned" | ...
    status: str | None = None
    priority: str | None = None
    deadline: str | None = None        # ISO date
    last_activity: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# ── Source: claude → MCP fetch ──────────────────────────────────────────

def _build_mcp_cli_args(prompt: str) -> list[str]:
    """Build the subprocess argv for the configured MCP_CLI backend."""
    if MCP_CLI == "pa":
        # PA CLI invocation. `pa -p "<prompt>"` is the documented headless
        # mode. PA has its own permission model — no --allowedTools or
        # --dangerously-skip-permissions equivalent. If PA prompts for
        # auth, the user has to `pa login` once on the host.
        return ["pa", "-p", prompt]
    # Default: Claude Code with explicit flags.
    return [
        "claude", "-p", prompt,
        "--model", os.environ.get("CLAUDE_MODEL", "claude-opus-4-7"),
        "--dangerously-skip-permissions",
        "--allowedTools", ALLOWED_TOOLS,
    ]


async def fetch_mcp_data(period_days: int) -> dict[str, list[dict]]:
    """Fetch the structured-JSON brief data from the configured CLI
    backend (Claude Code or PA). Both are expected to honour the strict
    JSON-only prompt; minor preamble / code-fence noise is tolerated."""
    prompt = mcp_prompt_for(period_days)
    args = _build_mcp_cli_args(prompt)
    log.info("mcp_cli_spawn", cli=MCP_CLI, cwd=str(REPO_DIR),
             timeout_s=MCP_CLI_TIMEOUT_S)
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(REPO_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                timeout=MCP_CLI_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{MCP_CLI} timed out after {MCP_CLI_TIMEOUT_S}s")
    if proc.returncode != 0:
        raise RuntimeError(
            f"{MCP_CLI} exited {proc.returncode}: "
            f"{stderr.decode(errors='replace')[:400]}"
        )

    text = stdout.decode(errors="replace").strip()
    # Strip optional ```json fences either CLI sometimes adds anyway.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    # Find first '{' in case the CLI emitted preamble before the JSON.
    if not text.startswith("{"):
        i = text.find("{")
        if i >= 0:
            text = text[i:]

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("json_parse_failed", cli=MCP_CLI, err=str(exc),
                  preview=text[:300])
        raise RuntimeError(f"{MCP_CLI} returned non-JSON: {text[:200]}")


# Backwards-compat alias — old name still imported elsewhere.
fetch_via_claude = fetch_mcp_data


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


def _g(d: dict, key: str, default: str = "uncategorized") -> str:
    v = d.get(key)
    return str(v) if v else default


def to_items(claude_data: dict, gh_data: dict) -> list[BriefItem]:
    items: list[BriefItem] = []

    for j in claude_data.get("jira", []) or []:
        # Fallback: derive project from key prefix if `group` missing.
        key = str(j.get("key", "?"))
        derived_group = key.split("-")[0] if "-" in key else "uncategorized"
        items.append(BriefItem(
            source="jira", icon="📋",
            item_id=key,
            title=str(j.get("summary", ""))[:200],
            url=str(j.get("url", "")),
            group=_g(j, "group", derived_group),
            reason=j.get("reason"),
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
            group=_g(m, "group", str(m.get("project_path") or "uncategorized")),
            reason=m.get("reason"),
            status=m.get("state"),
            deadline=m.get("milestone_due_date"),
            last_activity=m.get("updated_at"),
            extras={"kind": "MR"},
        ))

    for i in claude_data.get("gitlab_issues", []) or []:
        items.append(BriefItem(
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

    for nb in claude_data.get("nvbugs", []) or []:
        items.append(BriefItem(
            source="nvbugs", icon="🐛",
            item_id=str(nb.get("id", "?")),
            title=str(nb.get("title", ""))[:200],
            url=str(nb.get("url", "")),
            group=_g(nb, "group", "uncategorized"),
            reason=nb.get("reason"),
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
            group=_g(c, "group", "uncategorized"),
            reason=c.get("reason"),
            last_activity=c.get("updated"),
        ))

    def repo_name(d: dict) -> str:
        repo = d.get("repository") or {}
        if isinstance(repo, dict):
            return str(repo.get("nameWithOwner", "")) or str(repo.get("name", ""))
        return str(repo)

    for ghi in gh_data.get("issues", []) or []:
        repo = repo_name(ghi)
        items.append(BriefItem(
            source="github", icon="🐱",
            item_id=f"#{ghi.get('number', '?')}",
            title=str(ghi.get("title", ""))[:200],
            url=str(ghi.get("url", "")),
            group=repo or "uncategorized",
            reason="assignee",
            status=ghi.get("state"),
            last_activity=ghi.get("updatedAt"),
            extras={"kind": "issue"},
        ))
    for prs_key, reason, kind in (
        ("prs_authored", "author", "PR"),
        ("prs_reviewing", "reviewer", "PR"),
    ):
        for pr in gh_data.get(prs_key, []) or []:
            repo = repo_name(pr)
            items.append(BriefItem(
                source="github", icon="🐱",
                item_id=f"#{pr.get('number', '?')}",
                title=str(pr.get("title", ""))[:200],
                url=str(pr.get("url", "")),
                group=repo or "uncategorized",
                reason=reason,
                status=pr.get("state"),
                last_activity=pr.get("updatedAt"),
                extras={"kind": kind},
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

SLACK_SECTION_MAX_CHARS = 2900   # Slack hard limit is 3000; keep a buffer.
ITEMS_PER_GROUP = 5


def _format_item_line(i: BriefItem) -> str:
    label = md_link(f"{i.item_id} {i.title}".strip(), i.url) if i.item_id else md_link(i.title, i.url)
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
    if i.reason:
        bits.append(i.reason)
    for k in ("kind",):
        if v := i.extras.get(k):
            bits.append(str(v))
    line = f"  • {label}"
    if bits:
        line += " · " + " · ".join(bits)
    return line


def _group_items_for_source(items: list[BriefItem], today: date) -> list[dict]:
    """Group source's items by .group, sort groups by item count desc.

    Returns list of dicts: {name, count, due_soon, items}.
    """
    by_group: dict[str, list[BriefItem]] = {}
    for i in items:
        by_group.setdefault(i.group or "uncategorized", []).append(i)
    out: list[dict] = []
    soon = today + timedelta(days=PRIORITY_WINDOW_DAYS)
    for name, gitems in by_group.items():
        # Sort items in group: by deadline (soonest first, no-deadline last), then updated desc.
        def sort_key(x: BriefItem):
            d = parse_date(x.deadline)
            return (d or date.max, -(parse_date(x.last_activity) or date.min).toordinal())
        gitems_sorted = sorted(gitems, key=sort_key)
        due_soon = sum(1 for x in gitems if (d := parse_date(x.deadline)) and d <= soon)
        out.append({"name": name, "count": len(gitems), "due_soon": due_soon, "items": gitems_sorted})
    out.sort(key=lambda g: (-g["due_soon"], -g["count"], g["name"].lower()))
    return out


def _section_blocks_for_source(header: str, src_items: list[BriefItem],
                                today: date) -> list[dict]:
    """Build one or more section blocks for a source, splitting if a single
    section would exceed Slack's 3000-char limit."""
    groups = _group_items_for_source(src_items, today)
    n_groups = len(groups)

    summary = f"*{header} — {len(src_items)} item(s) across {n_groups} group(s)*"
    blocks: list[dict] = []
    rows: list[str] = [summary, ""]

    def flush() -> None:
        text = "\n".join(rows).rstrip()
        if text.strip():
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    for g in groups:
        # Header line for the group.
        counts = f"{g['count']}"
        if g["due_soon"]:
            counts += f" · *{g['due_soon']} due ≤{PRIORITY_WINDOW_DAYS}d*"
        group_block = [f"📁 *{g['name']}*  ({counts})"]
        for i in g["items"][:ITEMS_PER_GROUP]:
            group_block.append(_format_item_line(i))
        if g["count"] > ITEMS_PER_GROUP:
            group_block.append(f"  _+ {g['count'] - ITEMS_PER_GROUP} more_")
        group_block.append("")  # spacer after group

        proposed = "\n".join(rows + group_block)
        if len(proposed) > SLACK_SECTION_MAX_CHARS and len(rows) > 2:
            # Flush current section, start a new continuation section.
            flush()
            rows = [f"*{header} (cont.)*", ""] + group_block
        else:
            rows.extend(group_block)

    flush()
    return blocks


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
    by_source = {s: sum(1 for i in items if i.source == s) for s in {i.source for i in items}}
    summary_pieces = [f"*{n}* {s}" for s, n in sorted(by_source.items(), key=lambda kv: -kv[1])]
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"_{datetime.now().strftime('%-I:%M %p')} · "
                f"{len(items)} total · {' · '.join(summary_pieces) if summary_pieces else 'no items'}_"
            ),
        }],
    })
    blocks.append({"type": "divider"})

    if priority_items:
        rows = [f"*⏰ Priorities by deadline (next {PRIORITY_WINDOW_DAYS} days)*", ""]
        for i in priority_items[:10]:
            due = fmt_due(i.deadline) or "-"
            label = md_link(f"{i.item_id} {i.title}".strip(), i.url) if i.item_id else md_link(i.title, i.url)
            prio = f" *[{i.priority}]*" if i.priority else ""
            status = f" · _{i.status}_" if i.status else ""
            grp = f" · `{i.group}`" if i.group and i.group != "uncategorized" else ""
            rows.append(f"• `{due}` — {i.icon} {label}{prio}{grp}{status}")
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
        blocks.extend(_section_blocks_for_source(header, src_items, today))

    blocks.append({"type": "divider"})

    # Interactive buttons. action_ids MUST be unique within a block —
    # reuse the menu_brief_* handlers that already exist in the bridge
    # so we don't duplicate logic.
    blocks.append({
        "type": "actions",
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
        ],
    })

    footer_bits = ["_📧 Email: skipped (no Outlook MCP yet)_"]
    if errors:
        footer_bits.append(f"⚠️ fetch errors: {len(errors)} — try Reauth ↑")
    footer_bits.append("`/brief week|month` for other periods · `/mcp` for source health")
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
    started_at = time.time()
    log.info("brief_starting", repo_dir=str(REPO_DIR), period=period, days=preset["days"],
             mcp_cli=MCP_CLI)

    # ── Resolve Slack target FIRST so we can post a progress placeholder
    #    before the (slow) MCP fetch starts. Skip in --dry-run.
    placeholder_channel: str | None = None
    placeholder_ts: str | None = None
    client: WebClient | None = None
    if not dry_run:
        bot_token = os.environ.get("SLACK_BOT_TOKEN")
        target = channel_override or os.environ.get("SLACK_ALLOWED_USER_ID")
        if not bot_token or "REPLACE-ME" in bot_token:
            log.error("missing SLACK_BOT_TOKEN")
            return 2
        if not target:
            log.error("no target — set SLACK_ALLOWED_USER_ID in configs/.env or use --channel")
            return 2
        client = WebClient(token=bot_token)
        if target.startswith("U"):
            try:
                opened = client.conversations_open(users=target)
                placeholder_channel = opened["channel"]["id"]
            except Exception as exc:
                log.error("conversations_open_failed", err=str(exc))
                return 3
        else:
            placeholder_channel = target

        cli_label = "PA" if MCP_CLI == "pa" else "Claude"
        try:
            res = client.chat_postMessage(
                channel=placeholder_channel,
                text=(
                    f"🔄 *{preset['title']} — generating…*\n"
                    f"_Step 1/3: Asking {cli_label} to query MCPs "
                    "(Jira / GitLab / NVBugs / Confluence) + GitHub via `gh`…_"
                ),
            )
            placeholder_ts = res["ts"]
            log.info("placeholder_posted", channel=placeholder_channel, ts=placeholder_ts)
        except Exception as exc:
            log.warning("placeholder_post_failed", err=str(exc))

    def progress(text: str) -> None:
        if not (client and placeholder_channel and placeholder_ts):
            return
        try:
            client.chat_update(channel=placeholder_channel, ts=placeholder_ts, text=text)
        except Exception as exc:
            log.warning("progress_update_failed", err=str(exc))

    # ── Fetch in parallel
    errors: list[str] = []
    mcp_task = asyncio.create_task(fetch_mcp_data(preset["days"]))
    gh_task = asyncio.create_task(fetch_github())

    try:
        claude_data = await mcp_task
    except Exception as exc:
        log.error("mcp_fetch_failed", cli=MCP_CLI, err=str(exc))
        errors.append(f"{MCP_CLI}: {exc}")
        claude_data = {}

    try:
        gh_data = await gh_task
    except Exception as exc:
        log.error("gh_fetch_failed", err=str(exc))
        errors.append(f"github: {exc}")
        gh_data = {}

    elapsed_fetch = int(time.time() - started_at)
    items = to_items(claude_data, gh_data)
    by_source = {s: sum(1 for i in items if i.source == s)
                 for s in {i.source for i in items}}
    log.info("items_collected", count=len(items), by_source=by_source,
             fetch_seconds=elapsed_fetch)

    by_source_pretty = " · ".join(f"{n} {s}" for s, n in sorted(by_source.items(), key=lambda kv: -kv[1]))
    progress(
        f"🔄 *{preset['title']} — generating…*\n"
        f"_Step 2/3: Fetched in {elapsed_fetch}s — {len(items)} items "
        f"({by_source_pretty or 'none'}). Building blocks…_"
    )
    if errors:
        log.warning("fetch_errors", count=len(errors), details=errors)

    blocks = build_blocks(items, errors, period=period)

    # Validation: every action_id within an actions block must be unique
    # per Slack API. Catch duplicates locally so we fail loudly instead
    # of via a chat.update / chat.postMessage error mid-flow.
    for i, b in enumerate(blocks):
        if b.get("type") != "actions":
            continue
        ids = [e.get("action_id") for e in b.get("elements", []) if e.get("action_id")]
        dups = {x for x in ids if ids.count(x) > 1}
        if dups:
            log.error("duplicate_action_ids_in_block", block_index=i, duplicates=sorted(dups))
            raise RuntimeError(f"duplicate action_id(s) in block {i}: {sorted(dups)}")

    if dry_run:
        print(json.dumps(blocks, indent=2, ensure_ascii=False))
        log.info("dry_run_done", item_count=len(items))
        return 0

    progress(
        f"🔄 *{preset['title']} — generating…*\n"
        f"_Step 3/3: Posting {len(items)} items across {len(by_source)} source(s)…_"
    )

    fallback = f"{preset['title']} — {date.today().isoformat()} ({len(items)} items)"
    if placeholder_channel and placeholder_ts and client:
        try:
            res = client.chat_update(
                channel=placeholder_channel, ts=placeholder_ts,
                text=fallback, blocks=blocks,
            )
            log.info("posted_via_update", ok=res.get("ok"), ts=res.get("ts"),
                     channel=placeholder_channel, item_count=len(items),
                     total_seconds=int(time.time() - started_at))
            return 0
        except Exception as exc:
            log.warning("chat_update_failed_falling_back", err=str(exc))

    # Fallback: post a fresh message if we couldn't update the placeholder.
    assert client is not None
    res = client.chat_postMessage(
        channel=placeholder_channel or target,  # type: ignore[name-defined]
        text=fallback, blocks=blocks,
    )
    log.info("posted", ok=res.get("ok"), ts=res.get("ts"),
             channel=placeholder_channel, item_count=len(items),
             total_seconds=int(time.time() - started_at))
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
