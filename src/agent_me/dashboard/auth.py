"""Bearer-token auth for the dashboard.

Single shared secret model:
- `DASHBOARD_TOKEN` env var holds the canonical token.
- Browsers authenticate via `?t=<token>` once; we set a signed cookie
  (`itsdangerous.URLSafeSerializer`), valid for 30 days.
- API clients pass `Authorization: Bearer <token>`.

If `DASHBOARD_TOKEN` is empty/unset, the app refuses to bind a public
host (the entry point in `app.py` enforces this and falls back to
`127.0.0.1` only). This keeps "forgot to set token → exposed dashboard
on Tailscale Funnel" from happening.

Routes are auth'd via the `AuthMiddleware`; specific paths can be
exempted by listing them in `EXEMPT_PATHS`.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
from typing import Iterable

import structlog
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

log = structlog.get_logger("dashboard.auth")

COOKIE_NAME = "agent_me_dashboard_session"
COOKIE_MAX_AGE_S = 30 * 24 * 60 * 60  # 30 days

# Paths that bypass auth — keep this list short.
EXEMPT_PATHS: tuple[str, ...] = (
    "/healthz",
    "/static/",   # prefix match
    "/favicon.ico",
    "/login",
    "/api/login",
)

# Anti-CSRF: rotate secret per process; cookies don't survive restarts.
# Acceptable for a single-user dashboard. If we ever need stable
# cookies across restarts, persist this in `dashboard.db`.
_COOKIE_SECRET = secrets.token_urlsafe(32)
_serializer = URLSafeSerializer(_COOKIE_SECRET, salt="agent-me-dashboard")


def _expected_token() -> str:
    """Token from env. Empty string means "auth disabled" (only valid
    on 127.0.0.1; the public bind path checks this separately)."""
    return os.environ.get("DASHBOARD_TOKEN", "").strip()


def issue_cookie_value() -> str:
    """Sign a fresh session token. Just a "valid until" timestamp; the
    fact it's signed with our secret is the proof."""
    return _serializer.dumps({"v": 1, "iat": int(time.time())})


def verify_cookie(value: str) -> bool:
    try:
        payload = _serializer.loads(value)
    except BadSignature:
        return False
    iat = payload.get("iat", 0)
    return (time.time() - iat) < COOKIE_MAX_AGE_S


def is_exempt(path: str, exempt: Iterable[str] = EXEMPT_PATHS) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in exempt)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Always allow exempt paths (they handle their own auth if needed).
        if is_exempt(path):
            return await call_next(request)

        expected = _expected_token()
        if not expected:
            # No token configured → assume bind is 127.0.0.1 only and
            # let traffic through with a soft warning header. The public
            # bind path checks this and refuses to start.
            response = await call_next(request)
            response.headers["X-Dashboard-Auth"] = "disabled"
            return response

        # Path 1: API clients via Authorization header.
        auth_hdr = request.headers.get("authorization", "")
        if auth_hdr.startswith("Bearer "):
            given = auth_hdr.removeprefix("Bearer ").strip()
            if hmac.compare_digest(given, expected):
                response = await call_next(request)
                response.headers["X-Dashboard-Auth"] = "bearer"
                return response
            return JSONResponse({"error": "invalid bearer token"}, status_code=401)

        # Path 2: Browser via signed cookie (set on first ?t=... visit).
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie and verify_cookie(cookie):
            response = await call_next(request)
            response.headers["X-Dashboard-Auth"] = "cookie"
            return response

        # Path 3: First-visit handshake via ?t=<token>.
        token = request.query_params.get("t", "")
        if token and hmac.compare_digest(token, expected):
            # Strip the token from the URL via redirect, set cookie.
            target = request.url.path
            qs = "&".join(
                f"{k}={v}" for k, v in request.query_params.items() if k != "t"
            )
            if qs:
                target = f"{target}?{qs}"
            redirect = RedirectResponse(target, status_code=303)
            redirect.set_cookie(
                COOKIE_NAME, issue_cookie_value(),
                max_age=COOKIE_MAX_AGE_S,
                httponly=True, samesite="lax", secure=True,
                path="/",
            )
            log.info("auth_handshake", ua=request.headers.get("user-agent", "")[:80])
            return redirect

        # Reject. Browsers get a tiny HTML hint; APIs get JSON.
        if "application/json" in (request.headers.get("accept", "") or ""):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return Response(
            "Unauthorized — append <code>?t=&lt;DASHBOARD_TOKEN&gt;</code> to the URL.",
            status_code=401,
            media_type="text/html",
        )


def auth_required_for_public_bind() -> None:
    """Raise if the operator forgot to set DASHBOARD_TOKEN on a non-loopback bind."""
    if not _expected_token():
        raise SystemExit(
            "DASHBOARD_TOKEN is unset. Refusing to bind a public host "
            "without auth. Either:\n"
            "  • set DASHBOARD_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(24))') in configs/.env, or\n"
            "  • bind to 127.0.0.1 only with `--host 127.0.0.1`."
        )
