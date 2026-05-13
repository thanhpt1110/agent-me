from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_me.auto_sfa import (
    AutoSFAValidationError,
    auto_sfa_command,
    build_auto_sfa_request,
    build_update_template_request,
    missing_auto_sfa_fields,
    missing_update_template_fields,
    parse_auto_sfa_message,
    parse_update_template_message,
    run_auto_sfa,
    run_update_template,
    update_magic_auto_config,
    update_template_command,
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
        "display_name": "Thanh Phan",
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


def test_update_template_parse_and_build_folder_mode() -> None:
    values = parse_update_template_message(
        """
        display_name: Thanh Phan
        folder_id: 494139
        """
    )
    request = build_update_template_request(values)

    assert missing_update_template_fields(values) == []
    assert request.flow_type == "create"
    assert request.template_project_id == 1072
    assert request.display_name == "Thanh Phan"
    assert request.folder_id == 494139
    assert request.template_ids is None


def test_update_template_template_ids_are_optional_command_mode() -> None:
    request = build_update_template_request({
        "display_name": "Thanh Phan",
        "folder_id": "494139",
        "template_ids_enabled": True,
        "template_ids": "5996784, 5996785\n5996786",
    })

    args = update_template_command(request, uv_bin="/usr/bin/uv")

    assert request.template_ids == "5996784,5996785,5996786"
    assert args == [
        "/usr/bin/uv",
        "run",
        "dtoperator.py",
        "update-template",
        "-u",
        "Thanh Phan",
        "-i",
        "5996784,5996785,5996786",
        "--folder-id",
        "494139",
        "-f",
    ]


def test_update_template_rejects_non_template_project() -> None:
    with pytest.raises(AutoSFAValidationError) as exc:
        build_update_template_request({
            "display_name": "Thanh Phan",
            "folder_id": "494139",
            "project_id": "1074",
        })

    assert "project_id must be 1072 for Create SFA Tasks" in exc.value.errors


def test_auto_sfa_parse_full_keyed_message() -> None:
    values = parse_auto_sfa_message(
        """
        display_name: Thanh Phan
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

    assert request.display_name == "Thanh Phan"
    assert request.devtest_project_id == 1074
    assert request.source_folder_id == 50722
    assert request.devtest_folder_id == 1138081
    assert request.log_file_provider == "Manual"
    assert request.complexity_level == "L2"


def test_auto_sfa_parse_slack_mention_multiline_shortcuts() -> None:
    values = parse_auto_sfa_message(
        """
        @agent-me
        display_name: Thanh Phan
        source_folder_id: 50722
        devtest_folder_id: 1138081
        url_path: https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/blueprint-github-test/-/merge_requests/160
        start: 2026-04-16
        finish: 2026-04-25
        complexity_level: L2
        """
    )
    request = build_auto_sfa_request(values)

    assert request.display_name == "Thanh Phan"
    assert request.source_folder_id == 50722
    assert request.devtest_folder_id == 1138081
    assert request.log_file_base_url.endswith("/merge_requests/160")
    assert request.source_code_path.endswith("/merge_requests/160")
    assert request.planned_dev_start_date == "2026-04-16"
    assert request.planned_qa_finish_date == "2026-04-25"


def test_auto_sfa_parse_slack_code_fence_inline_delimiters() -> None:
    values = parse_auto_sfa_message(
        """@agent-me
        ```display_name: Thanh Phan
        source_folder_id: 50722
        devtest_folder_id: 1138081
        url_path: <https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/blueprint-github-test/-/merge_requests/160>
        start: 2026-04-16
        finish: 2026-04-27
        complexity_level: L2```"""
    )
    request = build_auto_sfa_request(values)

    assert request.display_name == "Thanh Phan"
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
        display_name l\u00e0 Thanh Phan
        source_folder_id: 50722
        devtest_folder_id: 1138081
        url_path: https://gitlab-master.nvidia.com/cloud-service-qa/Blueprint/blueprint-github-test/-/merge_requests/160
        start: 2026-04-16
        finish l\u00e0 2026-04-27
        complexity_level: L2
        """
    )
    request = build_auto_sfa_request(values)

    assert request.display_name == "Thanh Phan"
    assert request.planned_dev_finish_date == "2026-04-27"


def test_auto_sfa_parse_inline_followup_fields() -> None:
    values = parse_auto_sfa_message(
        "@agent-me display_name: Thanh Phan, finish date: 2026-04-28",
        existing=_full_values(),
    )
    request = build_auto_sfa_request(values)

    assert request.display_name == "Thanh Phan"
    assert request.planned_qa_finish_date == "2026-04-28"


def test_auto_sfa_parse_slack_link_url() -> None:
    values = parse_auto_sfa_message(
        """
        display_name: Thanh Phan
        source_folder_id: 50722
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
                "Thanh Phan",
                "1138081",
                "https://gitlab-master.nvidia.com/group/repo/-/merge_requests/159",
                "2026-04-16",
                "2026-04-27",
            )
        )
    )
    request = build_auto_sfa_request(values)

    assert values["display_name"] == "Thanh Phan"
    assert values["devtest_folder_id"] == "1138081"
    assert request.source_code_path.endswith("/merge_requests/159")


def test_auto_sfa_validation_rejects_bad_date() -> None:
    with pytest.raises(AutoSFAValidationError) as exc:
        build_auto_sfa_request(_full_values(start_date="2026-99-16"))

    assert "planned_dev_start_date must use yyyy-MM-dd" in exc.value.errors


def test_auto_sfa_validation_rejects_short_login_as_display_name() -> None:
    with pytest.raises(AutoSFAValidationError) as exc:
        build_auto_sfa_request(_full_values(display_name="thaphan"))

    assert "display_name must be a DevTest display name like Thanh Phan" in exc.value.errors


def test_auto_sfa_missing_fields_requires_compact_fields() -> None:
    values = _full_values(url_path="", source_folder_id="")

    assert "url_path" in missing_auto_sfa_fields(values)
    assert "source_folder_id" not in missing_auto_sfa_fields(values)


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


def test_auto_sfa_config_update_requires_source_folder_override(tmp_path) -> None:
    repo = tmp_path / "magic-auto"
    repo.mkdir()
    (repo / "configs.json").write_text(json.dumps(_sample_config(), indent=2) + "\n")
    request = build_auto_sfa_request(_full_values(source_folder_id="50724"))

    summary = update_magic_auto_config(request, repo)

    updated = json.loads((repo / "configs.json").read_text())
    assert updated["source_folder_id"] == 50724
    assert summary["source_folder_id"] == 50724


def test_auto_sfa_config_update_preserves_source_folder_when_blank(tmp_path) -> None:
    repo = tmp_path / "magic-auto"
    repo.mkdir()
    (repo / "configs.json").write_text(json.dumps(_sample_config(), indent=2) + "\n")
    request = build_auto_sfa_request(_full_values(source_folder_id=""))

    summary = update_magic_auto_config(request, repo)

    updated = json.loads((repo / "configs.json").read_text())
    assert updated["source_folder_id"] == 50722
    assert summary["source_folder_id"] == 50722


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


def test_auto_sfa_command_uses_display_name_as_single_argv() -> None:
    request = build_auto_sfa_request(_full_values(display_name="Thanh Phan"))

    args = auto_sfa_command(request, uv_bin="/usr/bin/uv")

    assert args == [
        "/usr/bin/uv",
        "run",
        "dtoperator.py",
        "sfa",
        "--user-login",
        "Thanh Phan",
        "-f",
    ]


def test_auto_sfa_task_ids_are_optional_command_mode() -> None:
    request = build_auto_sfa_request(
        _full_values(task_ids_enabled=True, task_ids="824423, 824424\n824425")
    )

    args = auto_sfa_command(request, uv_bin="/usr/bin/uv")

    assert request.task_ids == "824423,824424,824425"
    assert args == [
        "/usr/bin/uv",
        "run",
        "dtoperator.py",
        "sfa",
        "-i",
        "824423,824424,824425",
        "--user-login",
        "Thanh Phan",
        "-f",
    ]


def test_auto_sfa_credentials_are_required_only_when_enabled() -> None:
    request = build_auto_sfa_request(
        _full_values(
            use_personal_credentials=True,
            auth_username="thaphan@nvidia.com",
            auth_password="dummy-password",
        )
    )

    assert request.auth_username == "thaphan"
    assert request.auth_password == "dummy-password"
    public = request.as_input_dict()
    assert public["auth_username"] == "thaphan"
    assert public["auth_password_set"] is True
    assert "dummy-password" not in json.dumps(public)


def test_auto_sfa_use_default_credentials_ignores_stale_auth_fields() -> None:
    request = build_auto_sfa_request(
        _full_values(
            use_default_credentials=True,
            use_personal_credentials=True,
            auth_username="thaphan",
            auth_password="dummy-password",
        )
    )

    assert request.auth_username is None
    assert request.auth_password is None


def test_auto_sfa_rejects_empty_task_ids_when_task_id_mode_enabled() -> None:
    with pytest.raises(AutoSFAValidationError) as exc:
        build_auto_sfa_request(_full_values(task_ids_enabled=True, task_ids=""))

    assert "task_ids is required when specific task ID mode is enabled" in exc.value.errors


def test_auto_sfa_blank_source_folder_id_preserves_config_default() -> None:
    request = build_auto_sfa_request(_full_values(source_folder_id=""))

    assert request.source_folder_id is None


def test_auto_sfa_requires_source_folder_when_default_toggle_is_off() -> None:
    with pytest.raises(AutoSFAValidationError) as exc:
        build_auto_sfa_request(
            _full_values(use_default_source_folder=False, source_folder_id="")
        )

    assert "source_folder_id is required when default source folder is disabled" in exc.value.errors


@pytest.mark.asyncio
async def test_auto_sfa_run_passes_custom_credentials_to_magic_auto_env(
    tmp_path, monkeypatch
) -> None:
    repo = tmp_path / "magic-auto"
    repo.mkdir()
    (repo / "configs.json").write_text(json.dumps(_sample_config(), indent=2) + "\n")
    request = build_auto_sfa_request(
        _full_values(
            use_personal_credentials=True,
            auth_username="thaphan@nvidia.com",
            auth_password="dummy-password",
        )
    )
    captured: dict[str, object] = {}

    class FakeStdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeProcess:
        stdout = FakeStdout()

        async def wait(self):
            return 0

        def kill(self):
            captured["killed"] = True

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    await run_auto_sfa(request, repo_dir=repo, uv_bin="/usr/bin/uv")

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["DEVTEST_USERNAME"] == "thaphan"
    assert env["DEVTEST_PASSWORD"] == "dummy-password"
    args = captured["args"]
    assert isinstance(args, tuple)
    assert "-c" in args
    temp_config_path = Path(str(args[args.index("-c") + 1]))
    assert not temp_config_path.exists()
    base_config = json.loads((repo / "configs.json").read_text())
    assert base_config["devtest_folder_id"] == 1155188
    assert base_config["source_folder_id"] == 50722


@pytest.mark.asyncio
async def test_update_template_run_passes_custom_credentials_to_magic_auto_env(
    tmp_path, monkeypatch
) -> None:
    repo = tmp_path / "magic-auto"
    repo.mkdir()
    request = build_update_template_request({
        "display_name": "Thanh Phan",
        "folder_id": "494139",
        "use_personal_credentials": True,
        "auth_username": "thaphan@nvidia.com",
        "auth_password": "dummy-password",
    })
    captured: dict[str, object] = {}

    class FakeStdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeProcess:
        stdout = FakeStdout()

        async def wait(self):
            return 0

        def kill(self):
            captured["killed"] = True

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    await run_update_template(request, repo_dir=repo, uv_bin="/usr/bin/uv")

    assert captured["args"] == (
        "/usr/bin/uv",
        "run",
        "dtoperator.py",
        "update-template",
        "-u",
        "Thanh Phan",
        "--folder-id",
        "494139",
        "-f",
    )
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["DEVTEST_USERNAME"] == "thaphan"
    assert env["DEVTEST_PASSWORD"] == "dummy-password"
