from __future__ import annotations

from pathlib import Path

from agent_me.scripts import daily_brief


def test_parse_nvbugs_builds_clickable_bug_url() -> None:
    items = daily_brief.parse_nvbugs(
        {
            "items": [
                {
                    "id": "1234567",
                    "title": "ARB waiver needs QA signoff",
                    "status": "Open",
                    "priority": "P1",
                    "reason": "qa_eng",
                    "group": "gpu",
                    "updated": "2026-05-11T10:00:00Z",
                }
            ]
        },
        next(s for s in daily_brief.SOURCES if s.id == "nvbugs"),
    )

    assert len(items) == 1
    assert items[0].item_id == "1234567"
    assert items[0].url == "https://nvbugspro.nvidia.com/bug/1234567"
    assert items[0].reason == "qa_eng"
    assert items[0].last_activity == "2026-05-11T10:00:00Z"


def test_parse_nvbugs_accepts_raw_nvbugs_fields() -> None:
    items = daily_brief.parse_nvbugs(
        {
            "items": [
                {
                    "BugId": 6064144,
                    "Synopsis": "Benchmark Test Status Tracker",
                    "BugAction": "Dev - Open - To fix",
                    "Priority": "P2",
                    "Module": "Maxine",
                    "RequestDate": "2026-05-01T10:00:00Z",
                    "reason": "arb",
                }
            ]
        },
        next(s for s in daily_brief.SOURCES if s.id == "nvbugs"),
    )

    assert len(items) == 1
    assert items[0].item_id == "6064144"
    assert items[0].url == "https://nvbugspro.nvidia.com/bug/6064144"
    assert items[0].title == "Benchmark Test Status Tracker"
    assert items[0].status == "Dev - Open - To fix"
    assert items[0].group == "Maxine"
    assert items[0].reason == "arb"


def test_nvbugs_direct_fetcher_normalizes_structured_rows() -> None:
    payload = {
        "data": {
            "ReturnValue": {
                "data": [
                    [
                        6064144,
                        "2026-04-09T10:38:28.07",
                        {
                            "BugId": 6064144,
                            "Synopsis": "Benchmark Test Status Tracker",
                            "BugAction": "Dev - Open - To fix",
                            "Priority": "P2",
                            "Module": "Maxine NIM",
                            "RequestDate": "2026-04-09T10:38:28.07",
                        },
                    ]
                ]
            }
        }
    }

    rows = daily_brief._nvbugs_rows(payload)
    item = daily_brief._normalize_nvbug(rows[0], "arb")

    assert item["id"] == "6064144"
    assert item["url"] == "https://nvbugspro.nvidia.com/bug/6064144"
    assert item["title"] == "Benchmark Test Status Tracker"
    assert item["status"] == "Dev - Open - To fix"
    assert item["group"] == "Maxine NIM"
    assert item["reason"] == "arb"


def test_jira_direct_fetcher_normalizes_search_rows() -> None:
    payload = {
        "data": {
            "issues": [
                {
                    "key": "DGXCAT-32217",
                    "fields": {
                        "summary": "Validate worker shutdown cleanup",
                        "status": {"name": "Open"},
                        "priority": {"name": "P0 - Must have"},
                        "duedate": None,
                        "updatedDate": "2026-05-12T04:00:00.000-0700",
                        "project": {"key": "DGXCAT"},
                    },
                }
            ]
        }
    }

    rows = daily_brief._jira_rows(payload)
    item = daily_brief._normalize_jira(rows[0], "assignee")

    assert item["key"] == "DGXCAT-32217"
    assert item["url"] == "https://jirasw.nvidia.com/browse/DGXCAT-32217"
    assert item["summary"] == "Validate worker shutdown cleanup"
    assert item["status"] == "Open"
    assert item["priority"] == "P0 - Must have"
    assert item["group"] == "DGXCAT"
    assert item["reason"] == "assignee"


def test_jira_source_uses_direct_fetcher() -> None:
    spec = next(s for s in daily_brief.SOURCES if s.id == "jira")

    assert spec.fetcher is daily_brief.jira_fetcher


def test_gitlab_direct_fetcher_normalizes_merge_requests() -> None:
    payload = {
        "merge_requests": [
            {
                "iid": 42,
                "title": "Add release checklist",
                "state": "opened",
                "web_url": "https://gitlab.example/project/-/merge_requests/42",
                "merged_at": "2026-05-12T05:00:00Z",
                "project": {"path_with_namespace": "swqa/release"},
            }
        ]
    }

    rows = daily_brief._gitlab_mrs(payload)
    item = daily_brief._normalize_gitlab_mr(rows[0], "authored_waiting_review")

    assert item["iid"] == 42
    assert item["title"] == "Add release checklist"
    assert item["web_url"] == "https://gitlab.example/project/-/merge_requests/42"
    assert item["updated_at"] == "2026-05-12T05:00:00Z"
    assert item["group"] == "swqa/release"
    assert item["reason"] == "authored_waiting_review"


def test_gitlab_source_uses_direct_fetcher() -> None:
    spec = next(s for s in daily_brief.SOURCES if s.id == "gitlab")

    assert spec.fetcher is daily_brief.gitlab_fetcher


def test_gitlab_prompt_covers_requested_review_groups() -> None:
    assert "authored that are awaiting review" in daily_brief.GITLAB_PROMPT
    assert "assigned as reviewer" in daily_brief.GITLAB_PROMPT
    assert "merged in the last 3 days" in daily_brief.GITLAB_PROMPT
    assert 'role="author"' in daily_brief.GITLAB_PROMPT
    assert 'role="reviewer"' in daily_brief.GITLAB_PROMPT


def test_brief_sources_are_in_dashboard_order() -> None:
    source_ids = [s.id for s in daily_brief.SOURCES]

    assert source_ids == [
        "nvbugs", "gitlab", "github", "calendar",
        "outlook", "jira", "teams", "slack",
    ]


def test_parse_teams_preserves_chat_context() -> None:
    spec = next(s for s in daily_brief.SOURCES if s.id == "teams")
    items = daily_brief.parse_teams(
        {
            "items": [
                {
                    "team": "AI Blueprint",
                    "channel": "developer-examples",
                    "user": "Sam",
                    "snippet": "Can you check the failing pipeline?",
                    "timestamp": "2026-05-14T08:00:00Z",
                    "url": "https://teams.microsoft.com/l/message/1",
                    "group": "developer-examples",
                    "reason": "mention",
                }
            ]
        },
        spec,
    )

    assert len(items) == 1
    assert items[0].source == "teams"
    assert items[0].title == "[developer-examples] Can you check the failing pipeline?"
    assert items[0].url == "https://teams.microsoft.com/l/message/1"
    assert items[0].group == "developer-examples"
    assert items[0].reason == "mention"
    assert items[0].extras["team"] == "AI Blueprint"


def test_teams_source_uses_codex_app_prompt() -> None:
    spec = next(s for s in daily_brief.SOURCES if s.id == "teams")

    assert spec.fetcher is daily_brief.codex_fetcher
    assert spec.parser is daily_brief.parse_teams
    assert "microsoft teams_list_chats" in daily_brief.TEAMS_PROMPT
    assert "READ-ONLY" in daily_brief.TEAMS_PROMPT


def test_outlook_prompt_requires_recent_list_before_empty() -> None:
    prompt = daily_brief.OUTLOOK_PROMPT.format(
        user="thaphan",
        full_name="Thanh Phan",
        period_days=1,
        **daily_brief.period_window(1),
    )

    assert "Start with a plain recent message list" in prompt
    assert "Never return an empty list until" in prompt
    assert "Do NOT use Outlook search tools" in prompt
    assert "filter argument omitted" in prompt
    spec = next(s for s in daily_brief.SOURCES if s.id == "outlook")
    assert spec.fetcher is daily_brief.outlook_fetcher


def test_parse_calendar_preserves_meeting_context() -> None:
    spec = next(s for s in daily_brief.SOURCES if s.id == "calendar")
    items = daily_brief.parse_calendar(
        {
            "items": [
                {
                    "subject": "Model Free 2.0 sync",
                    "start": "2026-05-11T09:00:00+07:00",
                    "end": "2026-05-11T09:30:00+07:00",
                    "organizer": "qa-lead@nvidia.com",
                    "location": "Teams",
                    "body_summary": "Daily test status and open blockers",
                    "url": "https://outlook.office.com/calendar/item/1",
                    "group": "2026-05-11",
                    "reason": "required",
                }
            ]
        },
        spec,
    )

    assert len(items) == 1
    assert items[0].source == "calendar"
    assert items[0].group == "2026-05-11"
    assert items[0].extras["start"] == "2026-05-11T09:00:00+07:00"
    assert "Daily test status" in items[0].extras["body_summary"]
    assert "09:00-09:30" in daily_brief._format_item_line(items[0])


def test_dashboard_cache_payload_includes_calendar_meeting_time() -> None:
    spec = next(s for s in daily_brief.SOURCES if s.id == "calendar")
    item = daily_brief.BriefItem(
        source="calendar",
        icon="📅",
        item_id="",
        title="Model Free 2.0 sync",
        url="https://outlook.office.com/calendar/item/1",
        group="2026-05-11",
        extras={
            "start": "2026-05-11T09:00:00+07:00",
            "end": "2026-05-11T09:30:00+07:00",
        },
    )
    result = daily_brief.SubagentResult(
        spec=spec,
        items=[item],
        error=None,
        seconds=4,
    )

    payload = daily_brief.build_dashboard_cache_payload(
        result,
        period_days=1,
        fetched_at_ms=123456,
    )

    assert payload["source"] == "calendar"
    assert payload["fetched_at"] == 123456
    assert payload["items"][0]["meeting_time"] == "Mon 05/11 09:00-09:30"
    assert payload["items"][0]["meeting_time_full"] == (
        "Mon 2026-05-11 09:00-09:30 Asia/Ho_Chi_Minh"
    )
    assert payload["updated_by"] == "brief"


def test_write_dashboard_cache_updates_state_reader(temp_state_dir: Path) -> None:
    from agent_me.dashboard.state_reader import StateReader

    spec = next(s for s in daily_brief.SOURCES if s.id == "calendar")
    result = daily_brief.SubagentResult(
        spec=spec,
        items=[
            daily_brief.BriefItem(
                source="calendar",
                icon="📅",
                item_id="",
                title="NFP4 Office Hour",
                url="",
                group="2026-05-14",
                status="busy",
                extras={
                    "start": "2026-05-14T06:00:00+07:00",
                    "end": "2026-05-14T07:00:00+07:00",
                },
            )
        ],
        error=None,
        seconds=7,
    )

    daily_brief.write_dashboard_cache(
        [result],
        period_days=1,
        updated_by="slack-brief",
    )
    snap = StateReader.brief_snapshot("calendar")

    assert snap.fetched_at > 0
    assert snap.seconds == 7
    assert snap.updated_by == "slack-brief"
    assert snap.items[0]["title"] == "NFP4 Office Hour"
    assert snap.items[0]["meeting_time_full"] == (
        "Thu 2026-05-14 06:00-07:00 Asia/Ho_Chi_Minh"
    )


def test_slack_destination_replies_to_existing_thread_when_present() -> None:
    threaded = daily_brief.SlackDestination(
        label="primary",
        channel="C123",
        root_ts="222.2",
        thread_ts="111.1",
    )
    top_level = daily_brief.SlackDestination(
        label="mirror",
        channel="D123",
        root_ts="333.3",
    )

    assert threaded.reply_thread_ts == "111.1"
    assert top_level.reply_thread_ts == "333.3"


def test_resolve_cli_bin_finds_local_bin(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path
    fake_bin = fake_home / ".local" / "bin"
    fake_bin.mkdir(parents=True)
    codex = fake_bin / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)

    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", "")

    assert daily_brief.resolve_cli_bin("CODEX_BIN", "codex") == str(codex)


def test_build_connector_mirror_text_contains_source_links() -> None:
    spec = next(s for s in daily_brief.SOURCES if s.id == "nvbugs")
    result = daily_brief.SubagentResult(
        spec=spec,
        items=[
            daily_brief.BriefItem(
                source="nvbugs",
                icon="🐛",
                item_id="1234567",
                title="ARB waiver needs QA signoff",
                url="https://nvbugspro.nvidia.com/bug/1234567",
                group="gpu",
                reason="arb_related",
                status="Open",
            )
        ],
        error=None,
        seconds=4,
    )

    text = daily_brief.build_connector_mirror_text("day", [result], 4)

    assert "Daily Brief" in text
    assert "NVBugs" in text
    assert "<https://nvbugspro.nvidia.com/bug/1234567|1234567 ARB waiver needs QA signoff>" in text


def test_readonly_mcp_approval_configs_cover_core_brief_tools() -> None:
    joined = "\n".join(daily_brief.READONLY_MAAS_APPROVAL_CONFIGS)

    assert "maas-jira.tools.jira_search" in joined
    assert "maas-gitlab.tools.gitlab_list_merge_requests" in joined
    assert "maas-confluence.tools.confluence_search" in joined
    assert "maas-nvbugs.tools.nvbugs_search_v2" in joined
    assert "maas-nvbugs.tools.nvbugs_get_bug_details_v2" in joined
    assert "maas-nvbugs.tools.nvbugs_get_bug_v2" not in joined
    assert "approval_mode=\"approve\"" in joined


def test_connector_mirror_uses_app_server_auto_review(monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def fake_run_codex_app_server(prompt: str, timeout_s: float) -> str:
        captured["prompt"] = prompt
        captured["timeout_s"] = str(timeout_s)
        return '{"ok": true, "user_id": "U123", "link": "https://slack.example/m"}'

    monkeypatch.setattr(daily_brief, "_run_codex_app_server", fake_run_codex_app_server)

    import asyncio

    result = asyncio.run(
        daily_brief.send_connector_slack_mirror(
            "thaphan@nvidia.com",
            "Daily Brief\n- item",
        )
    )

    assert result.ok is True
    assert "Codex app-server auto-review" in captured["prompt"]
    assert "Do not use SLACK_BOT_TOKEN" in captured["prompt"]
    assert "_run_codex(" not in captured["prompt"]


def test_nvbugs_prompt_includes_full_name_alias() -> None:
    prompt = daily_brief.NVBUGS_PROMPT.format(
        user="thaphan",
        full_name="Thanh Phan",
        period_days=1,
        **daily_brief.period_window(1),
    )

    assert "Thanh Phan" in prompt
    assert 'QAEngineerFullName = "Thanh Phan"' in prompt
    assert 'ActionReqByFullName = "Thanh Phan"' in prompt
    assert "Do NOT broaden the search to requester, assignee" in prompt
    assert next(s for s in daily_brief.SOURCES if s.id == "nvbugs").fetcher is daily_brief.nvbugs_fetcher
