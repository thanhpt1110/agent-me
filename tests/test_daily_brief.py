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
