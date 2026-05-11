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
