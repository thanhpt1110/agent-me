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
        daily_brief.SOURCES[3],
    )

    assert len(items) == 1
    assert items[0].item_id == "1234567"
    assert items[0].url == "https://nvbugs.nvidia.com/Bug/1234567"
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
        daily_brief.SOURCES[3],
    )

    assert len(items) == 1
    assert items[0].item_id == "6064144"
    assert items[0].url == "https://nvbugs.nvidia.com/Bug/6064144"
    assert items[0].title == "Benchmark Test Status Tracker"
    assert items[0].status == "Dev - Open - To fix"
    assert items[0].group == "Maxine"
    assert items[0].reason == "arb"


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
    spec = daily_brief.SOURCES[3]
    result = daily_brief.SubagentResult(
        spec=spec,
        items=[
            daily_brief.BriefItem(
                source="nvbugs",
                icon="🐛",
                item_id="1234567",
                title="ARB waiver needs QA signoff",
                url="https://nvbugs.nvidia.com/Bug/1234567",
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
    assert "<https://nvbugs.nvidia.com/Bug/1234567|1234567 ARB waiver needs QA signoff>" in text


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
