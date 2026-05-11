"""Mock pending-tasks-per-platform-group data for the dashboard overview.

All data here is MOCK; wired in Phase 5 to real MCP fetchers (jira, gitlab,
confluence, nvbugs, slack, outlook, github) and to the bridge SQLite store
(threads, sessions). The shape is fixed and consumed by the Overview route
in `agent_me.dashboard.app.page_index` plus the Alpine front-end on
`templates/index.html`.

Key invariants the UI relies on:

* Group order is jira → gitlab → confluence → nvbugs → slack → outlook →
  github → threads → sessions. The Overview renders groups in this order.
* For every group, `pending_count == len(items)`.
* `items[].url` is always something a browser can navigate to (external
  https:// for real platforms; relative `/logs?...` for internal views).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "PendingItem",
    "PendingGroup",
    "get_pending_groups",
    "pending_groups_dicts",
]


# ── Data shapes ──────────────────────────────────────────────────────────

@dataclass
class PendingItem:
    item_id: str
    title: str
    url: str
    kind: str
    priority: str | None = None
    due: str | None = None
    age_label: str = ""
    mock: bool = True


@dataclass
class PendingGroup:
    group_id: str
    label: str
    icon: str
    pending_count: int
    home_url: str
    items: list[PendingItem] = field(default_factory=list)
    mock: bool = True
    note: str = "Mock — wire to real APIs in Phase 5"


# ── Mock content ─────────────────────────────────────────────────────────

def _jira_group() -> PendingGroup:
    items = [
        PendingItem(
            item_id="IPP-4521",
            title="Triage flaky e2e suite on Colossus rev 24.08",
            url="https://jirasw.nvidia.com/browse/IPP-4521",
            kind="issue",
            priority="P1",
            due="Fri",
            age_label="2h ago",
        ),
        PendingItem(
            item_id="IPP-4519",
            title="MaaS Glean re-index after upgrade window",
            url="https://jirasw.nvidia.com/browse/IPP-4519",
            kind="issue",
            priority="P2",
            due="next Mon",
            age_label="1d ago",
        ),
        PendingItem(
            item_id="IPP-4502",
            title="Approve MR backlog from last week",
            url="https://jirasw.nvidia.com/browse/IPP-4502",
            kind="issue",
            priority="P1",
            age_label="3d ago",
        ),
        PendingItem(
            item_id="IPP-4498",
            title="RFC: hybrid PA + Claude routing — sign-off",
            url="https://jirasw.nvidia.com/browse/IPP-4498",
            kind="issue",
            priority="P0",
            due="tomorrow",
            age_label="4h ago",
        ),
        PendingItem(
            item_id="IPP-4480",
            title="Reset NVBugs MCP token rotation",
            url="https://jirasw.nvidia.com/browse/IPP-4480",
            kind="issue",
            priority="P2",
            age_label="5d ago",
        ),
    ]
    return PendingGroup(
        group_id="jira",
        label="Jira",
        icon="📋",
        pending_count=len(items),
        home_url=(
            "https://jirasw.nvidia.com/issues/?jql="
            "assignee%20%3D%20currentUser()%20AND%20status%20!%3D%20Done"
        ),
        items=items,
    )


def _gitlab_group() -> PendingGroup:
    items = [
        PendingItem(
            item_id="!2847",
            title="agent-me: dashboard NVIDIA theme + pending tasks panel",
            url="https://gitlab-master.nvidia.com/thaphan/agent-me/-/merge_requests/2847",
            kind="mr",
            priority="P1",
            age_label="today",
        ),
        PendingItem(
            item_id="!2820",
            title="infra/colossus: bump MaaS MCP catalog to 17.3",
            url="https://gitlab-master.nvidia.com/infra/colossus/-/merge_requests/2820",
            kind="mr",
            priority="P2",
            age_label="1d ago",
        ),
        PendingItem(
            item_id="!2799",
            title="Fix CI cache thrash on small runners",
            url="https://gitlab-master.nvidia.com/infra/ci/-/merge_requests/2799",
            kind="mr",
            priority="P2",
            age_label="2d ago",
        ),
        PendingItem(
            item_id="!2775",
            title="Migrate Phase 2b approval gate hook → systemd",
            url="https://gitlab-master.nvidia.com/thaphan/agent-me/-/merge_requests/2775",
            kind="mr",
            priority="P1",
            age_label="4d ago",
        ),
    ]
    return PendingGroup(
        group_id="gitlab",
        label="GitLab",
        icon="🦊",
        pending_count=len(items),
        home_url=(
            "https://gitlab-master.nvidia.com/dashboard/merge_requests"
            "?state=opened&assignee_username=thaphan"
        ),
        items=items,
    )


def _confluence_group() -> PendingGroup:
    items = [
        PendingItem(
            item_id="EC-9912",
            title="Review Q2 OKR rollout deck — comment by Wed",
            url="https://confluence.nvidia.com/x/EC9912",
            kind="page",
            priority="P1",
            due="Wed",
            age_label="6h ago",
        ),
        PendingItem(
            item_id="EC-9908",
            title="Update agent-me deploy playbook for Colossus",
            url="https://confluence.nvidia.com/x/EC9908",
            kind="page",
            priority="P2",
            age_label="1d ago",
        ),
        PendingItem(
            item_id="EC-9870",
            title="Add hybrid PA/Claude routing diagram",
            url="https://confluence.nvidia.com/x/EC9870",
            kind="page",
            priority="P2",
            age_label="3d ago",
        ),
    ]
    return PendingGroup(
        group_id="confluence",
        label="Confluence",
        icon="📚",
        pending_count=len(items),
        home_url="https://confluence.nvidia.com/dashboard.action#all-updates",
        items=items,
    )


def _nvbugs_group() -> PendingGroup:
    items = [
        PendingItem(
            item_id="5104883",
            title="claude-cli OAuth refresh races on slow disks",
            url="https://nvbugs.nvidia.com/Bug/5104883",
            kind="bug",
            priority="P1",
            age_label="8h ago",
        ),
        PendingItem(
            item_id="5104812",
            title="MCP probe leaks subprocess on timeout",
            url="https://nvbugs.nvidia.com/Bug/5104812",
            kind="bug",
            priority="P2",
            age_label="2d ago",
        ),
        PendingItem(
            item_id="5104755",
            title="Funnel sometimes returns 526 on cold-start",
            url="https://nvbugs.nvidia.com/Bug/5104755",
            kind="bug",
            priority="P2",
            age_label="5d ago",
        ),
    ]
    return PendingGroup(
        group_id="nvbugs",
        label="NVBugs",
        icon="🐛",
        pending_count=len(items),
        home_url="https://nvbugs.nvidia.com/queries/my-assigned",
        items=items,
    )


def _slack_group() -> PendingGroup:
    items = [
        PendingItem(
            item_id="C04ABCDEF",
            title="#proj-agent-me — your PR is mentioned by @minh",
            url="https://nvidia.enterprise.slack.com/archives/C04ABCDEF/p1730000000123456",
            kind="msg",
            priority="P1",
            age_label="30m ago",
        ),
        PendingItem(
            item_id="D02XYZ123",
            title="DM from @leadership-eng — re Q2 priorities",
            url="https://nvidia.enterprise.slack.com/archives/D02XYZ123",
            kind="msg",
            priority="P0",
            age_label="2h ago",
        ),
        PendingItem(
            item_id="C04INFRA",
            title="#infra-eng — Colossus host maintenance window",
            url="https://nvidia.enterprise.slack.com/archives/C04INFRA",
            kind="msg",
            priority="P2",
            age_label="4h ago",
        ),
    ]
    return PendingGroup(
        group_id="slack",
        label="Slack",
        icon="💬",
        pending_count=len(items),
        home_url="https://nvidia.enterprise.slack.com/unreads",
        items=items,
    )


def _outlook_group() -> PendingGroup:
    items = [
        PendingItem(
            item_id="MAIL-0921",
            title="Q2 review meeting prep — please pre-read",
            url="https://outlook.office.com/mail/inbox/id/MAIL-0921",
            kind="email",
            priority="P1",
            due="Thu",
            age_label="1h ago",
        ),
        PendingItem(
            item_id="MAIL-0918",
            title="Action: ECI auth migration — sign-off needed",
            url="https://outlook.office.com/mail/inbox/id/MAIL-0918",
            kind="email",
            priority="P0",
            due="tomorrow",
            age_label="5h ago",
        ),
        PendingItem(
            item_id="MAIL-0901",
            title="FYI: NVIDIA AI Summit travel reimbursement",
            url="https://outlook.office.com/mail/inbox/id/MAIL-0901",
            kind="email",
            priority="P2",
            age_label="3d ago",
        ),
    ]
    return PendingGroup(
        group_id="outlook",
        label="Outlook",
        icon="📧",
        pending_count=len(items),
        home_url="https://outlook.office.com/mail/inbox",
        items=items,
    )


def _github_group() -> PendingGroup:
    items = [
        PendingItem(
            item_id="#42",
            title="feat(dashboard): NVIDIA theme + pending panel",
            url="https://github.com/thanhpt1110/agent-me/pull/42",
            kind="pr",
            priority="P1",
            age_label="today",
        ),
        PendingItem(
            item_id="#41",
            title="docs: deploy-on-host Caddy steps",
            url="https://github.com/thanhpt1110/agent-me/pull/41",
            kind="pr",
            priority="P2",
            age_label="1d ago",
        ),
        PendingItem(
            item_id="#39",
            title="fix: brief fan-out timeout on slow MCPs",
            url="https://github.com/thanhpt1110/agent-me/pull/39",
            kind="pr",
            priority="P2",
            age_label="4d ago",
        ),
    ]
    return PendingGroup(
        group_id="github",
        label="GitHub",
        icon="🐱",
        pending_count=len(items),
        home_url="https://github.com/thanhpt1110/agent-me/pulls",
        items=items,
    )


def _threads_group() -> PendingGroup:
    items = [
        PendingItem(
            item_id="1730123456.789012",
            title="Operator: brief schedule tweak Q2",
            url="/logs?thread_ts=1730123456.789012",
            kind="thread",
            priority="P1",
            age_label="2h ago",
        ),
        PendingItem(
            item_id="1730098765.456789",
            title="Operator: NVBugs reauth follow-up",
            url="/logs?thread_ts=1730098765.456789",
            kind="thread",
            priority="P2",
            age_label="1d ago",
        ),
        PendingItem(
            item_id="1729998877.111222",
            title="Operator: dashboard theme request",
            url="/logs?thread_ts=1729998877.111222",
            kind="thread",
            priority="P1",
            age_label="today",
        ),
    ]
    return PendingGroup(
        group_id="threads",
        label="Slack threads",
        icon="🧵",
        pending_count=len(items),
        home_url="/ops",
        items=items,
    )


def _sessions_group() -> PendingGroup:
    items = [
        PendingItem(
            item_id="7e6b2a4f-1234-5678-9abc-def012345678",
            title="session 7e6b2a4f — dashboard rework, 12 turns",
            url="/logs?session_id=7e6b2a4f-1234-5678-9abc-def012345678",
            kind="session",
            priority="P1",
            age_label="now",
        ),
        PendingItem(
            item_id="a1c8d2e9-9876-5432-10fe-dcba98765432",
            title="session a1c8d2e9 — brief prompt tuning, 8 turns",
            url="/logs?session_id=a1c8d2e9-9876-5432-10fe-dcba98765432",
            kind="session",
            priority="P2",
            age_label="3h ago",
        ),
        PendingItem(
            item_id="bb55cc77-aaaa-bbbb-cccc-dddddddddddd",
            title="session bb55cc77 — Phase 2b approval debug, 5 turns",
            url="/logs?session_id=bb55cc77-aaaa-bbbb-cccc-dddddddddddd",
            kind="session",
            priority="P2",
            age_label="1d ago",
        ),
    ]
    return PendingGroup(
        group_id="sessions",
        label="Claude sessions",
        icon="🧠",
        pending_count=len(items),
        home_url="/logs",
        items=items,
    )


# ── Public API ───────────────────────────────────────────────────────────

def get_pending_groups() -> list[PendingGroup]:
    """Return the full list of platform groups with mock pending items.

    Order matters (UI renders in this order):
      jira, gitlab, confluence, nvbugs, slack, outlook, github, threads, sessions
    """
    return [
        _jira_group(),
        _gitlab_group(),
        _confluence_group(),
        _nvbugs_group(),
        _slack_group(),
        _outlook_group(),
        _github_group(),
        _threads_group(),
        _sessions_group(),
    ]


def pending_groups_dicts() -> list[dict[str, Any]]:
    """Same as get_pending_groups() but returns dicts (for Jinja/Alpine)."""
    return [
        {**asdict(g), "items": [asdict(i) for i in g.items]}
        for g in get_pending_groups()
    ]
