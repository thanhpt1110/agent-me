"""auth: bearer-token middleware + signed cookie + exempt-path handling."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


async def _ok(_request: Request):
    return JSONResponse({"ok": True})


def _app() -> Starlette:
    """Mini app used to test the AuthMiddleware in isolation."""
    from agent_me.dashboard.auth import AuthMiddleware

    return Starlette(routes=[
        Route("/", _ok),
        Route("/api/state", _ok),
        Route("/healthz", _ok),
    ], middleware=[Middleware(AuthMiddleware)])


def test_auth_disabled_when_token_unset(without_token: None) -> None:
    """No DASHBOARD_TOKEN → middleware lets traffic through with a hint header."""
    client = TestClient(_app())
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("X-Dashboard-Auth") == "disabled"


def test_unauth_without_token(with_token: str) -> None:
    client = TestClient(_app())
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 401


def test_bearer_correct_passes(with_token: str) -> None:
    client = TestClient(_app())
    r = client.get("/api/state", headers={"Authorization": f"Bearer {with_token}"})
    assert r.status_code == 200
    assert r.headers.get("X-Dashboard-Auth") == "bearer"


def test_bearer_wrong_rejected(with_token: str) -> None:
    client = TestClient(_app())
    r = client.get("/api/state", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_query_param_handshake_redirects_with_cookie(with_token: str) -> None:
    """First visit `?t=...` → 303 + cookie set + redirect to bare path."""
    client = TestClient(_app())
    r = client.get(f"/?t={with_token}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location") == "/"

    # Cookie should be present in the response.
    from agent_me.dashboard.auth import COOKIE_NAME
    assert COOKIE_NAME in r.cookies


def test_cookie_session_works(with_token: str) -> None:
    """After ?t= handshake, subsequent requests succeed via cookie alone."""
    from agent_me.dashboard.auth import COOKIE_NAME, issue_cookie_value

    client = TestClient(_app(), cookies={COOKIE_NAME: issue_cookie_value()})
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("X-Dashboard-Auth") == "cookie"


def test_healthz_is_exempt(with_token: str) -> None:
    client = TestClient(_app())
    r = client.get("/healthz")
    assert r.status_code == 200


def test_query_token_wrong_rejected(with_token: str) -> None:
    client = TestClient(_app())
    r = client.get("/?t=wrong-value", follow_redirects=False)
    assert r.status_code == 401


def test_query_param_strips_token_from_redirect(with_token: str) -> None:
    """Other query params survive but `t` is dropped on the redirect."""
    client = TestClient(_app())
    r = client.get(f"/?t={with_token}&foo=bar", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers.get("location") or ""
    assert "t=" not in loc
    assert "foo=bar" in loc


def test_verify_cookie_handles_bad_signature(with_token: str) -> None:
    from agent_me.dashboard.auth import verify_cookie

    assert verify_cookie("not-a-real-signed-value") is False


def test_is_exempt_matches_static_prefix() -> None:
    from agent_me.dashboard.auth import is_exempt

    assert is_exempt("/healthz") is True
    assert is_exempt("/static/app.css") is True
    assert is_exempt("/static/sub/x.js") is True
    assert is_exempt("/api/state") is False
    assert is_exempt("/source/jira") is False


def test_unauth_returns_json_when_accept_json(with_token: str) -> None:
    client = TestClient(_app())
    r = client.get("/api/state", headers={"Accept": "application/json"})
    assert r.status_code == 401
    assert "json" in r.headers.get("content-type", "")
    assert "error" in r.json()
