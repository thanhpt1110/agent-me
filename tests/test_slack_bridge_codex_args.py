from __future__ import annotations

import importlib


def test_codex_args_skip_git_check_for_chat_cwd(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    fresh_args = app._codex_args("hello", resume_session_id=None)
    resumed_args = app._codex_args("hello", resume_session_id="session-123")

    assert "--skip-git-repo-check" in fresh_args
    assert "--skip-git-repo-check" in resumed_args
    assert fresh_args.index("--skip-git-repo-check") < fresh_args.index("--cd")
    assert resumed_args.index("--skip-git-repo-check") < resumed_args.index("-m")


def test_codex_args_accept_extra_configs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    args = app._codex_args(
        "hello",
        resume_session_id=None,
        extra_configs=('mcp_servers.maas-playwright.tools.browser_navigate.approval_mode="approve"',),
    )

    assert args[:3] == [
        app.CODEX_BIN,
        "-c",
        'mcp_servers.maas-playwright.tools.browser_navigate.approval_mode="approve"',
    ]
    assert args[3] == "exec"


def test_app_server_args_enable_auto_review(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    args = app._codex_app_server_args("send a test Slack DM")

    assert args[:5] == [
        app.CODEX_BIN,
        "-c",
        'approval_policy="on-request"',
        "-c",
        'approvals_reviewer="auto_review"',
    ]
    assert args[-4:] == ["debug", "app-server", "send-message-v2", "send a test Slack DM"]


def test_model_free_email_request_detection(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    text = "fetch/get/check/read email related to me with subject Model Free 2.0.4"

    assert app.looks_like_model_free_email_request(text)
    assert app.model_free_subject_pattern_from_text(text) == "Model Free 2.0.4"
    assert not app.model_free_request_forces_draft(text)


def test_model_free_draft_request_detection(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    text = "try reply all draft for model-free 2.0.4 email"

    assert app.looks_like_model_free_email_request(text)
    assert app.model_free_subject_pattern_from_text(text) == "Model Free 2.0.4"
    assert app.model_free_request_forces_draft(text)
    assert app.looks_like_model_free_email_request("create draft for Model Free 2.0.4")


def test_model_free_detection_ignores_non_email_requests(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    assert not app.looks_like_model_free_email_request("summarize model-free 2.0.4 bugs")


def test_model_free_followup_request_detection(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    assert app.looks_like_model_free_followup_request("confirm reply all draft")
    assert app.looks_like_model_free_followup_request("create another draft for this email")
    assert app.looks_like_model_free_followup_request("execute it for the right email")


def test_outlook_write_request_detection(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    assert app.looks_like_outlook_write_request("draft an email to me")
    assert app.looks_like_outlook_write_request("so" + "\u1ea1" + "n 1 email draft test codex")
    assert app.looks_like_outlook_write_request("create reply all draft for this email")
    assert not app.looks_like_outlook_write_request("read email about Model Free 2.0.4")


def test_permissioned_connector_write_detection(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    assert app.looks_like_permissioned_connector_write_request(
        "send a slack message to thaphan@nvidia.com"
    )
    assert app.looks_like_permissioned_connector_write_request(
        "create Jira issue for the failing daily brief"
    )
    assert app.looks_like_permissioned_connector_write_request(
        "comment on NVBugs 6156776 that testing started"
    )
    assert app.looks_like_permissioned_connector_write_request(
        "share this Google Drive doc with Thanh"
    )
    assert app.looks_like_permissioned_connector_write_request(
        "so" + "\u1ea1" + "n reply all email cho toi"
    )
    assert not app.looks_like_permissioned_connector_write_request(
        "read Slack messages mentioning me"
    )
    assert not app.looks_like_permissioned_connector_write_request(
        "fetch email related to Model Free 2.0.4"
    )
    assert not app.looks_like_permissioned_connector_write_request(
        "update me about today's meetings"
    )
    assert not app.looks_like_permissioned_connector_write_request(
        'Find open NVBugs where QAEngineerFullName = "Thanh Phan"'
    )
    assert not app.looks_like_permissioned_connector_write_request(
        "show open bugs where ARB is Thanh Phan"
    )
    assert app.looks_like_nvbugs_read_request(
        'Test NVBugs only. Find open NVBugs where QAEngineerFullName = "Thanh Phan"'
    )


def test_help_blocks_include_auto_sfa(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    blocks = app._help_blocks()
    assert any(
        element.get("action_id") == "menu_auto_sfa"
        for block in blocks if block.get("type") == "actions"
        for element in block.get("elements", [])
    )
    assert any(
        element.get("action_id") == "menu_auto_sfa_create"
        for block in blocks if block.get("type") == "actions"
        for element in block.get("elements", [])
    )
    assert app.PLAIN_COMMANDS["auto sfa"] == ("/auto-sfa", "release")
    assert app.PLAIN_COMMANDS["create sfa tasks"] == ("/auto-sfa", "create")


def test_mcp_refresh_shortcuts(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    assert app.PLAIN_COMMANDS["mcp refresh"] == ("/mcp", "refresh")
    assert app.PLAIN_COMMANDS["refresh mcp"] == ("/mcp", "refresh")


def test_mcp_slash_refresh_routes_to_refresh(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    calls: list[bool] = []

    async def fake_cmd_mcp(refresh: bool = False) -> str:
        calls.append(refresh)
        return "ok"

    monkeypatch.setattr(app, "cmd_mcp", fake_cmd_mcp)

    import asyncio

    assert asyncio.run(app.handle_slash("/mcp", "U123", "refresh")) == "ok"
    assert calls == [True]


def test_auto_sfa_start_remembers_thread(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    import asyncio

    text, blocks = asyncio.run(
        app.cmd_auto_sfa_start(
            channel="D123",
            thread_ts="1700000000.000011",
            user_id="U123",
        )
    )
    flow = asyncio.run(app.get_auto_sfa_flow("1700000000.000011"))

    assert "Auto SFA" in text
    assert blocks
    assert flow is not None
    assert flow["status"] == "active"
    assert flow["channel"] == "D123"
    assert flow["inputs"]["flow_type"] == "release"


def test_auto_sfa_create_start_remembers_flow_type(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    import asyncio

    text, blocks = asyncio.run(
        app.cmd_auto_sfa_start(
            channel="D123",
            thread_ts="1700000000.000012",
            user_id="U123",
            flow_type="create",
        )
    )
    flow = asyncio.run(app.get_auto_sfa_flow("1700000000.000012"))

    assert "Create SFA Tasks" in text
    assert blocks
    assert flow is not None
    assert flow["inputs"]["flow_type"] == "create"


def test_model_free_subject_pattern_is_exact(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    assert app.model_free_subject_pattern_in_text("Subject: Model Free 2.0.4")
    assert app.model_free_subject_pattern_in_text("Subject: model-free 2.0.4")
    assert app.model_free_subject_pattern_in_text("ga-model-free-nim 2.0.4") is None


def test_model_free_subject_recovers_from_slack_history(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    class FakeClient:
        async def conversations_replies(self, **kwargs):
            return {
                "messages": [
                    {"text": "fetch email with Subject contains Model Free 2.0.4"},
                    {"text": "create another draft for this email"},
                ],
            }

    import asyncio

    subject = asyncio.run(
        app.recover_model_free_subject_from_slack_thread(
            FakeClient(), "D123", "1778531331.613309",
        )
    )

    assert subject == "Model Free 2.0.4"


def test_model_free_prompt_always_creates_new_draft(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    captured: dict[str, str] = {}

    async def fake_spawn_codex_app_server(prompt):
        captured["prompt"] = prompt
        return "ok", None

    monkeypatch.setattr(app, "spawn_codex_app_server", fake_spawn_codex_app_server)

    import asyncio

    asyncio.run(
        app.cmd_model_free_draft(
            subject_pattern="Model Free 2.0.4",
            user_request="fetch email with subject Model Free 2.0.4",
        )
    )

    prompt = captured["prompt"]
    assert "create exactly one new reply-all draft" in prompt
    assert "Do not skip because a previous user-authored" in prompt
    assert "Reject subjects such as `ga-model-free-nim 2.0.4`" in prompt
    assert "if skipped because an equivalent reply already exists" not in prompt


def test_app_server_final_message_parser(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_ME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    app = importlib.import_module("agent_me.slack_bridge.app")

    transcript = """noise
< {
<   "method": "item/completed",
<   "params": {
<     "item": {
<       "type": "agentMessage",
<       "text": "Draft created. Link: https://outlook.example/item",
<       "phase": "final_answer"
<     }
<   }
< }
tail
"""

    assert app.parse_app_server_final_message(transcript) == (
        "Draft created. Link: https://outlook.example/item"
    )
