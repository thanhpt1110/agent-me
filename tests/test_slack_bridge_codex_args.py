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
