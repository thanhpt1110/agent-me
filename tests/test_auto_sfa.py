from __future__ import annotations

import json

import pytest

from agent_me.auto_sfa import (
    AutoSFAValidationError,
    auto_sfa_command,
    build_auto_sfa_request,
    missing_auto_sfa_fields,
    parse_auto_sfa_message,
    update_magic_auto_config,
)


def _sample_config() -> dict:
    return {
        "devtest_project_id": 1074,
        "devtest_folder_id": 1155188,
        "source_folder_id": 50722,
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


def _full_values(**overrides) -> dict:
    values = {
        "username_email": "thaphan@nvidia.com",
        "devtest_project_id": "1074",
        "source_folder_id": "50722",
        "devtest_folder_id": "1138081",
        "url_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
        "start_date": "2026-04-16",
        "finish_date": "2026-04-27",
        "log_file_provider": "Manual",
        "log_file_base_url": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
        "planned_dev_start_date": "2026-04-16",
        "planned_dev_finish_date": "2026-04-27",
        "actual_dev_start_date": "2026-04-16",
        "actual_dev_finish_date": "2026-04-27",
        "planned_qa_start_date": "2026-04-16",
        "planned_qa_finish_date": "2026-04-27",
        "actual_qa_start_date": "2026-04-16",
        "complexity_level": "L2",
        "source_code_path": "https://gitlab-master.nvidia.com/group/repo/",
        "code_review_path": "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
    }
    values.update(overrides)
    return values


def test_auto_sfa_parse_full_keyed_message() -> None:
    values = parse_auto_sfa_message(
        """
        username_email: thaphan@nvidia.com
        devtest_project_id: 1074
        source_folder_id: 50722
        devtest_folder_id: 1138081
        log_file_provider: Manual
        log_file_base_url: https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159
        planned_dev_start_date: 2026-04-16
        planned_dev_finish_date: 2026-04-27
        actual_dev_start_date: 2026-04-16
        actual_dev_finish_date: 2026-04-27
        planned_qa_start_date: 2026-04-16
        planned_qa_finish_date: 2026-04-27
        actual_qa_start_date: 2026-04-16
        complexity_level: l2
        source_code_path: https://gitlab-master.nvidia.com/group/repo/
        code_review_path: https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159
        """
    )
    request = build_auto_sfa_request(values)

    assert request.user_login == "thaphan"
    assert request.devtest_project_id == 1074
    assert request.source_folder_id == 50722
    assert request.devtest_folder_id == 1138081
    assert request.log_file_provider == "Manual"
    assert request.complexity_level == "L2"


def test_auto_sfa_parse_slack_mention_multiline_shortcuts() -> None:
    values = parse_auto_sfa_message(
        """
        @agent-me
        username_email: thaphan@nvidia.com
        devtest_folder_id: 1138081
        source_folder_id: null
        url_path: https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/blueprint-github-test/-/merge_requests/160
        start: 2026-04-16
        finish: 2026-04-25
        complexity_level: L2
        """
    )
    request = build_auto_sfa_request(values)

    assert request.user_login == "thaphan"
    assert request.source_folder_id is None
    assert request.devtest_folder_id == 1138081
    assert request.log_file_base_url.endswith("/merge_requests/160")
    assert request.source_code_path.endswith("/merge_requests/160")
    assert request.planned_dev_start_date == "2026-04-16"
    assert request.planned_qa_finish_date == "2026-04-25"


def test_auto_sfa_parse_slack_code_fence_inline_delimiters() -> None:
    values = parse_auto_sfa_message(
        """@agent-me
        ```username_email: thaphan@nvidia.com
        devtest_folder_id: 1138081
        url_path: <https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/blueprint-github-test/-/merge_requests/160>
        start: 2026-04-16
        finish: 2026-04-27
        complexity_level: L2```"""
    )
    request = build_auto_sfa_request(values)

    assert request.user_login == "thaphan"
    assert request.devtest_folder_id == 1138081
    assert request.log_file_base_url == (
        "https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/"
        "blueprint-github-test/-/merge_requests/160"
    )
    assert request.planned_dev_start_date == "2026-04-16"
    assert request.actual_dev_finish_date == "2026-04-27"


def test_auto_sfa_parse_vietnamese_field_separator() -> None:
    values = parse_auto_sfa_message(
        """
        @agent-me
        username_email l\u00e0 thaphan@nvidia.com
        devtest_folder_id: 1138081
        url_path: https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/blueprint-github-test/-/merge_requests/160
        start: 2026-04-16
        finish l\u00e0 2026-04-27
        complexity_level: L2
        """
    )
    request = build_auto_sfa_request(values)

    assert request.user_login == "thaphan"
    assert request.planned_dev_finish_date == "2026-04-27"


def test_auto_sfa_parse_inline_followup_fields() -> None:
    values = parse_auto_sfa_message(
        "@agent-me username_email: thaphan@nvidia.com, finish date: 2026-04-28",
        existing=_full_values(),
    )
    request = build_auto_sfa_request(values)

    assert request.user_login == "thaphan"
    assert request.planned_qa_finish_date == "2026-04-28"


def test_auto_sfa_parse_slack_link_url() -> None:
    values = parse_auto_sfa_message(
        """
        username_email: thaphan@nvidia.com
        devtest_folder_id: 1155188
        url_path: <https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123|https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123>
        start: 2026-04-16
        finish: 2026-05-08
        complexity_level: L2
        """
    )
    request = build_auto_sfa_request(values)

    assert request.log_file_base_url == "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123"


def test_auto_sfa_parse_ordered_compact_message() -> None:
    values = parse_auto_sfa_message(
        "\n".join(
            (
                "thaphan@nvidia.com",
                "1138081",
                "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
                "2026-04-16",
                "2026-04-27",
            )
        )
    )
    request = build_auto_sfa_request(values)

    assert values["username_email"] == "thaphan@nvidia.com"
    assert values["user_login"] == "thaphan"
    assert values["devtest_folder_id"] == "1138081"
    assert request.source_code_path.endswith("/merge_requests/159")


def test_auto_sfa_validation_rejects_bad_date() -> None:
    with pytest.raises(AutoSFAValidationError) as exc:
        build_auto_sfa_request(_full_values(start_date="2026-99-16"))

    assert "planned_dev_start_date must use yyyy-MM-dd" in exc.value.errors


def test_auto_sfa_validation_rejects_display_name_login() -> None:
    with pytest.raises(AutoSFAValidationError) as exc:
        build_auto_sfa_request(_full_values(username_email="Thanh Phan"))

    assert "username must be an NVIDIA account like thaphan" in exc.value.errors


def test_auto_sfa_missing_fields_requires_compact_fields() -> None:
    values = _full_values(url_path="")

    assert "url_path" in missing_auto_sfa_fields(values)


def test_auto_sfa_domino_does_not_require_log_file_base_url() -> None:
    request = build_auto_sfa_request(
        _full_values(log_file_provider="Domino", log_file_base_url="")
    )

    assert request.log_file_provider == "Domino"
    assert request.log_file_base_url == request.code_review_path


def test_auto_sfa_config_update_sets_magic_auto_fields(tmp_path) -> None:
    repo = tmp_path / "magic-auto"
    repo.mkdir()
    (repo / "configs.json").write_text(json.dumps(_sample_config(), indent=2) + "\n")
    request = build_auto_sfa_request(
        _full_values(
            devtest_project_id="2077",
            source_folder_id="50723",
            devtest_folder_id="12345",
            log_file_base_url="https://gitlab-master.nvidia.com/group/repo/-/merge_requests/200",
            source_code_path="https://gitlab-master.nvidia.com/group/repo/",
            code_review_path="https://gitlab-master.nvidia.com/group/repo/-/merge_requests/200",
        )
    )

    summary = update_magic_auto_config(request, repo)

    updated = json.loads((repo / "configs.json").read_text())
    release = updated["release_configs"]
    assert updated["devtest_project_id"] == 2077
    assert updated["source_folder_id"] == 50723
    assert updated["devtest_folder_id"] == 12345
    assert updated["log_file_provider"] == "Manual"
    assert updated["log_file_base_url"] == request.log_file_base_url
    assert release["planned_dev_start_date"] == "2026-04-16"
    assert release["planned_dev_finish_date"] == "2026-04-27"
    assert release["actual_qa_start_date"] == "2026-04-16"
    assert release["complexity_level"] == "L2"
    assert release["source_code_path"] == request.source_code_path
    assert release["code_review_path"] == request.code_review_path
    assert summary["log_file_provider"] == "Manual"


def test_auto_sfa_config_update_keeps_mr_url_for_domino_input(tmp_path) -> None:
    repo = tmp_path / "magic-auto"
    repo.mkdir()
    (repo / "configs.json").write_text(json.dumps(_sample_config(), indent=2) + "\n")
    request = build_auto_sfa_request(
        _full_values(log_file_provider="Domino", log_file_base_url="")
    )

    update_magic_auto_config(request, repo)

    updated = json.loads((repo / "configs.json").read_text())
    assert updated["log_file_provider"] == "Domino"
    assert updated["log_file_base_url"] == request.log_file_base_url


def test_auto_sfa_command_uses_user_login_as_single_argv() -> None:
    request = build_auto_sfa_request(_full_values(user_login="thaphan"))

    args = auto_sfa_command(request, uv_bin="/usr/bin/uv")

    assert args == [
        "/usr/bin/uv",
        "run",
        "dtoperator.py",
        "sfa",
        "--user-login",
        "thaphan",
        "-f",
    ]
