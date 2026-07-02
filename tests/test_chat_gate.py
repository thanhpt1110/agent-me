"""Operator-passcode gate on the dashboard /chat surface."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient


def _client(monkeypatch: pytest.MonkeyPatch, code: str | None) -> TestClient:
    if code is None:
        monkeypatch.delenv("DASHBOARD_OPERATOR_ACTION_CODE", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_OPERATOR_ACTION_CODE", code)
    from agent_me.dashboard import app as dash
    return TestClient(dash.app)


def test_chat_locked_without_passcode(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch, "secret-code")
    assert c.get("/api/chat/sessions").status_code == 403
    assert c.post("/api/chat/sessions", json={}).status_code == 403
    assert c.post("/api/chat/sessions/x/message", json={"text": "hi"}).status_code == 403


def test_unlock_then_access(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch, "secret-code")
    assert c.post("/api/chat/unlock", headers={"X-Agent-Me-Action-Code": "wrong"}).status_code == 403
    assert c.post("/api/chat/unlock", headers={"X-Agent-Me-Action-Code": "secret-code"}).status_code == 200
    assert c.get("/api/chat/sessions").status_code == 200  # cookie carried by TestClient


def test_gate_off_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch, None)
    assert c.get("/api/chat/sessions").status_code == 200
    assert c.post("/api/chat/unlock").json() == {"ok": True, "gate": False}


def test_chat_xdata_attribute_is_well_formed(monkeypatch: pytest.MonkeyPatch) -> None:
    # `tojson` emits raw double-quotes, so the x-data attribute must be
    # SINGLE-quoted or the browser truncates it and Alpine never initialises
    # (gate never renders, New/Send never bind). Regression guard.
    c = _client(monkeypatch, None)
    html = c.get("/chat").text
    assert "x-data='chat(" in html
    assert 'x-data="chat(' not in html


def test_chat_page_is_viewport_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """/chat is an app screen: the body must not scroll (only the transcript pane
    does) and the site footer is skipped, so the composer never slides off-screen.
    Normal pages keep the footer and a scrollable body."""
    c = _client(monkeypatch, None)
    chat = c.get("/chat").text
    assert "overflow-hidden" in chat.split("<body", 1)[1].split(">", 1)[0]
    assert "<footer" not in chat
    home = c.get("/").text
    assert "<footer" in home
    assert "overflow-hidden" not in home.split("<body", 1)[1].split(">", 1)[0]
