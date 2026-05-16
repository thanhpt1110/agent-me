from __future__ import annotations


def test_mcp_token_store_round_trips_encrypted_credentials(temp_state_dir) -> None:
    from agent_me.auto_sfa_mcp_store import (
        create_mcp_token,
        credentials_for_bearer_token,
        token_db_path,
    )

    created = create_mcp_token(
        username="Thanh.Phan@nvidia.com",
        password="devtest-secret",
        label="pytest",
    )
    assert created.token.startswith("agm_")
    assert created.expires_at is None
    assert token_db_path().parent == temp_state_dir

    stored = credentials_for_bearer_token(created.token)

    assert stored is not None
    assert stored.username == "thanh.phan"
    assert stored.password == "devtest-secret"

    raw_db = token_db_path().read_bytes()
    assert b"devtest-secret" not in raw_db


def test_mcp_token_store_can_revoke_token(temp_state_dir) -> None:
    from agent_me.auto_sfa_mcp_store import (
        create_mcp_token,
        credentials_for_bearer_token,
        revoke_mcp_token,
    )

    created = create_mcp_token(username="thaphan", password="pw")

    assert credentials_for_bearer_token(created.token) is not None
    assert revoke_mcp_token(created.token) is True
    assert credentials_for_bearer_token(created.token) is None


def test_mcp_install_script_escapes_codex_inline_table() -> None:
    from agent_me.auto_sfa_mcp_store import install_script

    script = install_script(endpoint="http://agent-me.nvidia.com/mcp/")

    assert 'endpoint=http://agent-me.nvidia.com/mcp/' in script
    assert 'f\'http_headers = {{ Authorization = "{auth_header}" }}\\n\'' in script
