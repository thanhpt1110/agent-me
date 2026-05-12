from __future__ import annotations

import json

import pytest

from agent_me.auto_sfa import (
    AutoSFAValidationError,
    auto_sfa_command,
    build_auto_sfa_request,
    parse_auto_sfa_message,
    update_magic_auto_config,
)


def _sample_config() -> dict:
    return {
        "devtest_project_id": 1074,
        "devtest_folder_id": 1155188,
        "log_file_provider": "Manual",
        "log_file_base_url": "https://old.example/mr",
        "release_configs": {
            "planned_dev_start_date": "2026-01-01",
            "planned_dev_finish_date": "2026-01-02",
            "actual_dev_start_date": "2026-01-01",
            "actual_dev_finish_date": "2026-01-02",
            "planned_qa_start_date": "2026-01-01",
            "planned_qa_finish_date": "2026-01-02",
            "actual_qa_start_date": "2026-01-01",
            "complexity_level": "L2",
            "source_code_path": "https://old.example/repo",
            "code_review_path": "https://old.example/mr",
        },
    }


def test_auto_sfa_parse_keyed_message_preserves_task_owner() -> None:
    values = parse_auto_sfa_message(
        """
        username: Thanh  Phan
        devtest_folder_id: 1155188
        url_path: https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159
        start: 2026-04-16
        finish: 2026-05-08
        """
    )
    request = build_auto_sfa_request(values)

    assert request.username == "Thanh  Phan"
    assert request.devtest_folder_id == 1155188
    assert request.start_date == "2026-04-16"
    assert request.finish_date == "2026-05-08"


def test_auto_sfa_parse_slack_mention_multiline_message() -> None:
    values = parse_auto_sfa_message(
        """
        @agent-me
        username: Thanh Phan
        devtest_folder_id: 1138081
        url_path: https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/blueprint-github-test/-/merge_requests/160
        start: 2026-04-16
        finish: 2026-04-25
        """
    )
    request = build_auto_sfa_request(values)

    assert request.username == "Thanh Phan"
    assert request.devtest_folder_id == 1138081
    assert request.url_path.endswith("/merge_requests/160")
    assert request.start_date == "2026-04-16"
    assert request.finish_date == "2026-04-25"


def test_auto_sfa_parse_slack_code_fence_inline_delimiters() -> None:
    values = parse_auto_sfa_message(
        """@agent-me
        ```username: Thanh Phan
        devtest_folder_id: 1138081
        url_path: <https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/blueprint-github-test/-/merge_requests/160>
        start: 2026-04-16
        finish: 2026-04-27```"""
    )
    request = build_auto_sfa_request(values)

    assert request.username == "Thanh Phan"
    assert request.devtest_folder_id == 1138081
    assert request.url_path == (
        "https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/"
        "blueprint-github-test/-/merge_requests/160"
    )
    assert request.start_date == "2026-04-16"
    assert request.finish_date == "2026-04-27"


def test_auto_sfa_parse_vietnamese_field_separator() -> None:
    values = parse_auto_sfa_message(
        """
        @agent-me
        username l\u00e0 Thanh Phan
        devtest_folder_id: 1138081
        url_path: https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/blueprint-github-test/-/merge_requests/160
        start: 2026-04-16
        finish l\u00e0 2026-04-27
        """
    )
    request = build_auto_sfa_request(values)

    assert request.username == "Thanh Phan"
    assert request.finish_date == "2026-04-27"


def test_auto_sfa_parse_inline_followup_fields() -> None:
    values = parse_auto_sfa_message(
        "@agent-me username: Thanh Phan, finish date: 2026-04-28",
        existing={
            "devtest_folder_id": "1155188",
            "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123",
            "start_date": "2026-04-16",
        },
    )
    request = build_auto_sfa_request(values)

    assert request.username == "Thanh Phan"
    assert request.finish_date == "2026-04-28"


def test_auto_sfa_parse_slack_link_url() -> None:
    values = parse_auto_sfa_message(
        """
        username: Thanh Phan
        devtest_folder_id: 1155188
        url_path: <https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123|https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123>
        start: 2026-04-16
        finish: 2026-05-08
        """
    )
    request = build_auto_sfa_request(values)

    assert request.url_path == "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123"


def test_auto_sfa_parse_ordered_message() -> None:
    values = parse_auto_sfa_message(
        "\n".join(
            (
                "Thanh Phan",
                "1155188",
                "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
                "2026-04-16",
                "2026-05-08",
            )
        )
    )

    assert values == {
        "username": "Thanh Phan",
        "devtest_folder_id": "1155188",
        "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
        "start_date": "2026-04-16",
        "finish_date": "2026-05-08",
    }


def test_auto_sfa_validation_rejects_bad_date() -> None:
    with pytest.raises(AutoSFAValidationError) as exc:
        build_auto_sfa_request(
            {
                "username": "Thanh Phan",
                "devtest_folder_id": "1155188",
                "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
                "start_date": "2026-99-16",
                "finish_date": "2026-05-08",
            }
        )

    assert "start date must use yyyy-MM-dd" in exc.value.errors


def test_auto_sfa_config_update_sets_shared_fields(tmp_path) -> None:
    repo = tmp_path / "magic-auto"
    repo.mkdir()
    (repo / "configs.json").write_text(json.dumps(_sample_config(), indent=2) + "\n")
    request = build_auto_sfa_request(
        {
            "username": "Thanh Phan",
            "devtest_folder_id": "12345",
            "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
            "start_date": "2026-04-16",
            "finish_date": "2026-05-08",
        }
    )

    update_magic_auto_config(request, repo)

    updated = json.loads((repo / "configs.json").read_text())
    release = updated["release_configs"]
    assert updated["devtest_folder_id"] == 12345
    assert updated["log_file_base_url"] == request.url_path
    assert release["source_code_path"] == request.url_path
    assert release["code_review_path"] == request.url_path
    assert all(value == "2026-04-16" for key, value in release.items() if "start" in key)
    assert all(value == "2026-05-08" for key, value in release.items() if "finish" in key)


def test_auto_sfa_command_keeps_owner_as_single_argv() -> None:
    request = build_auto_sfa_request(
        {
            "username": "Thanh Phan",
            "devtest_folder_id": "1155188",
            "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
            "start_date": "2026-04-16",
            "finish_date": "2026-05-08",
        }
    )

    args = auto_sfa_command(request, uv_bin="/usr/bin/uv")

    assert args == [
        "/usr/bin/uv",
        "run",
        "dtoperator.py",
        "sfa",
        "--task-owner",
        "Thanh Phan",
        "-f",
    ]
