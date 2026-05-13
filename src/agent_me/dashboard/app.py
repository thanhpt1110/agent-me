"""Starlette ASGI app for the agent-me dashboard.

Run with `uv run agent-me-dashboard` (the entry point binds via uvicorn).
The app itself is also importable as `agent_me.dashboard.app:app` for
external ASGI runners.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from dotenv import load_dotenv
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from agent_me.auto_sfa import AutoSFAValidationError, build_auto_sfa_request
from agent_me.dashboard.auth import (
    COOKIE_MAX_AGE_S,
    COOKIE_NAME,
    AuthMiddleware,
    auth_required_for_public_bind,
    issue_cookie_value,
)
from agent_me.dashboard.auto_sfa_runner import AutoSFARunner
from agent_me.dashboard.brief_runner import BriefRunner
from agent_me.dashboard.log_sources import (
    tail_bridge_slack_filtered,
    tail_journal_unit,
    tail_session_jsonl,
)
from agent_me.dashboard.mock_pending import pending_groups_dicts
from agent_me.dashboard.state_reader import (
    SOURCE_IDS,
    SOURCES,
    StateReader,
    check_mcp_health,
)
from agent_me.mcp_tokens import (
    codex_mcp_env_file_path,
    credentials_path,
    refresh_codex_mcp_env_file,
    refresh_mcp_tokens,
)

# ── Setup ────────────────────────────────────────────────────────────────

REPO_DIR = Path(os.environ.get("AGENT_ME_REPO_DIR")
                or Path(__file__).resolve().parents[3])
ENV = REPO_DIR / "configs" / ".env"
if ENV.exists():
    load_dotenv(ENV)

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
    ],
    cache_logger_on_first_use=True,
)
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
log = structlog.get_logger("dashboard")

PKG_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PKG_DIR / "templates"))
STATIC_DIR = PKG_DIR / "static"

START_TS = time.time()
RUNNER = BriefRunner()
AUTO_SFA_RUNNER = AutoSFARunner()

# Cached MCP probe — `codex mcp list` takes ~1s and we don't want
# every page hit to trigger one.
_MCP_CACHE: dict[str, Any] = {"servers": [], "checked_at": 0}
_MCP_CACHE_TTL_S = 30


# ── Template helpers ─────────────────────────────────────────────────────

def _ms_to_human(ms: int | None) -> str:
    if not ms:
        return "—"
    delta_s = int(time.time() - ms / 1000)
    if delta_s < 60:
        return f"{delta_s}s ago"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86400:
        return f"{delta_s // 3600}h ago"
    return f"{delta_s // 86400}d ago"


def _coerce_epoch_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        if value <= 0:
            return None
        return int(value if value > 1_000_000_000_000 else value * 1000)
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
        return int(numeric if numeric > 1_000_000_000_000 else numeric * 1000)
    except ValueError:
        pass

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _activity_label(value: Any, fallback_ms: int | None) -> str:
    if ms := _coerce_epoch_ms(value):
        return _ms_to_human(ms)
    if isinstance(value, str) and value.strip():
        return value.strip()[:16]
    return _ms_to_human(fallback_ms) if fallback_ms else ""


def _brief_priority(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).upper()
    for priority in ("P0", "P1", "P2", "P3"):
        if priority in raw:
            return priority
    if "CRITICAL" in raw or "HIGH" in raw:
        return "P1"
    if "MEDIUM" in raw:
        return "P2"
    return None


def _brief_item_to_pending_item(item: dict[str, Any], source: str,
                                fetched_at: int) -> dict[str, Any]:
    extras = item.get("extras") if isinstance(item.get("extras"), dict) else {}
    item_id = str(item.get("item_id") or extras.get("id") or "")
    title = str(item.get("title") or "(untitled)")[:220]
    return {
        "item_id": item_id,
        "title": title,
        "url": str(item.get("url") or f"/source/{source}"),
        "kind": str(extras.get("kind") or item.get("kind") or item.get("group") or "item"),
        "priority": _brief_priority(item.get("priority")),
        "due": item.get("deadline") or item.get("due"),
        "age_label": _activity_label(item.get("last_activity"), fetched_at),
        "mock": False,
        "reason": item.get("reason"),
    }


def _pending_groups_from_snapshots(snapshots: list[Any]) -> list[dict[str, Any]]:
    """Use real brief cache for source groups, falling back to mock data.

    This keeps the overview as the single pending surface: refreshing a
    source writes the brief cache, and the next overview render replaces
    that source's mock group with the latest brief items.
    """
    mock_by_id = {g["group_id"]: g for g in pending_groups_dicts()}
    groups: list[dict[str, Any]] = []

    for snap in snapshots:
        source_url = f"/source/{snap.source}"
        mock = mock_by_id.get(snap.source)
        has_cache = bool(snap.fetched_at)

        if has_cache:
            items = [
                _brief_item_to_pending_item(item, snap.source, snap.fetched_at)
                for item in snap.items
            ]
            note = f"Brief cache · fetched {_ms_to_human(snap.fetched_at)} · {snap.seconds}s"
            cache_state = "error" if snap.error else ("stale" if snap.stale else "fresh")
            cache_label = "error" if snap.error else ("stale" if snap.stale else "fresh")
            mock_flag = False
        elif mock:
            items = [dict(item) for item in mock["items"]]
            note = "Mock fallback · refresh this source to replace with brief data"
            cache_state = "mock"
            cache_label = "mock fallback"
            mock_flag = True
        else:
            items = []
            note = "No brief cache yet"
            cache_state = "empty"
            cache_label = "no cache"
            mock_flag = False

        if snap.error:
            note = f"Brief error · {str(snap.error)[:140]}"

        groups.append({
            "group_id": snap.source,
            "label": snap.label,
            "icon": snap.icon,
            "pending_count": len(items),
            "home_url": source_url,
            "source_url": source_url,
            "items": items,
            "mock": mock_flag,
            "note": note,
            "cache_state": cache_state,
            "cache_label": cache_label,
            "fetched_at": snap.fetched_at,
            "seconds": snap.seconds,
            "error": snap.error,
        })
    return groups


TEMPLATES.env.filters["age"] = _ms_to_human


# ── Routes: HTML pages ───────────────────────────────────────────────────

async def page_index(request: Request):
    snapshots = StateReader.all_snapshots()
    bridge_stats = StateReader.bridge_stats()
    pending_groups = _pending_groups_from_snapshots(snapshots)
    total_pending = sum(g["pending_count"] for g in pending_groups)
    return TEMPLATES.TemplateResponse(request, "index.html", {
        "snapshots": snapshots,
        # Alpine on the client wants a plain JSON-serializable list.
        "snapshots_json": [s.__dict__ for s in snapshots],
        "bridge_stats": bridge_stats,
        "sources": SOURCES,
        "now_ms": int(time.time() * 1000),
        "pending_groups": pending_groups,
        "total_pending": total_pending,
    })


async def page_source(request: Request):
    source_id = request.path_params["source_id"]
    if source_id not in SOURCE_IDS:
        return JSONResponse({"error": f"unknown source: {source_id}"}, status_code=404)
    snapshot = StateReader.brief_snapshot(source_id)
    active_job = RUNNER.active_job_for(source_id)
    return TEMPLATES.TemplateResponse(request, "source.html", {
        "snapshot": snapshot,
        "sources": SOURCES,
        "active_job": active_job.public_dict() if active_job else None,
    })


async def page_ops(request: Request):
    bridge_stats = StateReader.bridge_stats()
    threads = StateReader.recent_threads(limit=20)
    approvals = StateReader.pending_approvals()
    brief_runs = StateReader.recent_brief_runs(limit=10)
    return TEMPLATES.TemplateResponse(request, "ops.html", {
        "bridge_stats": bridge_stats,
        "threads": threads,
        "approvals": approvals,
        "brief_runs": brief_runs,
        "mcp_cache": _MCP_CACHE,
        "sources": SOURCES,
    })


async def page_logs(request: Request):
    """3-tab log viewer: watcher unit / Slack interactions / session trace.

    The threads list is filtered to ones with a `session_id` so the
    session-tab dropdown only shows traceable threads.
    """
    threads = [
        t for t in StateReader.recent_threads(limit=50)
        if t.get("session_id")
    ]
    return TEMPLATES.TemplateResponse(request, "logs.html", {
        "recent_threads": threads,
        "sources": SOURCES,
    })


async def page_auto_sfa(request: Request):
    active_job = AUTO_SFA_RUNNER.active_job()
    return TEMPLATES.TemplateResponse(request, "auto_sfa.html", {
        "sources": SOURCES,
        "active_job": active_job.public_dict() if active_job else None,
        "recent_jobs": [j.public_dict() for j in AUTO_SFA_RUNNER.recent_jobs(limit=5)],
    })


# ── Routes: JSON API ─────────────────────────────────────────────────────

async def api_state(_request: Request):
    return JSONResponse({
        "uptime_s": int(time.time() - START_TS),
        "now_ms": int(time.time() * 1000),
        "bridge_stats": StateReader.bridge_stats().__dict__,
        "snapshots": [
            {**s.__dict__, "items_count": len(s.items)} for s in StateReader.all_snapshots()
        ],
        "active_jobs": [j.public_dict() for j in RUNNER.recent_jobs(limit=5)],
    })


async def api_source(request: Request):
    source_id = request.path_params["source_id"]
    if source_id not in SOURCE_IDS:
        return JSONResponse({"error": "unknown source"}, status_code=404)
    snap = StateReader.brief_snapshot(source_id)
    return JSONResponse({
        **snap.__dict__,
        "items_count": len(snap.items),
    })


async def api_refresh(request: Request):
    source_id = request.path_params["source_id"]
    if source_id not in SOURCE_IDS:
        return JSONResponse({"error": "unknown source"}, status_code=404)
    period = request.query_params.get("period", "day")
    period_days = {"day": 1, "week": 7, "month": 30}.get(period, 1)

    existing = RUNNER.active_job_for(source_id)
    if existing and existing.status in ("pending", "running"):
        return JSONResponse(
            {**existing.public_dict(), "coalesced": True},
            status_code=200,
        )
    job = await RUNNER.start(source_id, period_days=period_days)
    return JSONResponse(job.public_dict(), status_code=202)


async def api_refresh_all(request: Request):
    """Fan out a refresh across all sources in parallel.

    Each source goes through the same single-flight lock as a single
    refresh, so if a source is already running we coalesce onto its
    existing job. Returns the list of job descriptors so the browser
    can subscribe to each one.
    """
    period = request.query_params.get("period", "day")
    period_days = {"day": 1, "week": 7, "month": 30}.get(period, 1)
    jobs: list[dict[str, Any]] = []
    for src_id, _label, _icon in SOURCES:
        existing = RUNNER.active_job_for(src_id)
        if existing and existing.status in ("pending", "running"):
            jobs.append({**existing.public_dict(), "coalesced": True})
            continue
        job = await RUNNER.start(src_id, period_days=period_days)
        jobs.append(job.public_dict())
    log.info("refresh_all_started",
             period_days=period_days,
             jobs={j["source"]: j["job_id"] for j in jobs})
    return JSONResponse({"jobs": jobs, "period_days": period_days},
                        status_code=202)


async def api_mcp_refresh(_request: Request):
    """Force a `codex mcp list` probe, bypassing TTL."""
    servers, checked_at = await check_mcp_health()
    _MCP_CACHE["servers"] = [s.__dict__ for s in servers]
    _MCP_CACHE["checked_at"] = checked_at
    return JSONResponse({
        "servers": _MCP_CACHE["servers"],
        "checked_at": checked_at,
    })


async def api_mcp_auth_refresh(_request: Request):
    """Refresh MaaS OAuth tokens, rewrite Codex env exports, then probe MCPs."""
    report = await asyncio.to_thread(partial(refresh_mcp_tokens, force=True))
    env_count = await asyncio.to_thread(
        partial(refresh_codex_mcp_env_file, refresh_tokens=False)
    )
    servers, checked_at = await check_mcp_health()
    _MCP_CACHE["servers"] = [s.__dict__ for s in servers]
    _MCP_CACHE["checked_at"] = checked_at
    return JSONResponse({
        "attempted": list(report.attempted),
        "refreshed": list(report.refreshed),
        "failed": report.failed,
        "skipped": list(report.skipped),
        "env_exports": env_count,
        "env_file": str(codex_mcp_env_file_path()),
        "credentials": str(credentials_path()),
        "needs_mac_sync": bool(report.failed),
        "servers": _MCP_CACHE["servers"],
        "checked_at": checked_at,
    })


async def api_mcp_status(_request: Request):
    """Return cached MCP probe; refresh in background if stale."""
    age = (time.time() * 1000 - _MCP_CACHE["checked_at"]) / 1000
    stale = age > _MCP_CACHE_TTL_S
    if stale:
        servers, checked_at = await check_mcp_health()
        _MCP_CACHE["servers"] = [s.__dict__ for s in servers]
        _MCP_CACHE["checked_at"] = checked_at
    return JSONResponse({
        "servers": _MCP_CACHE["servers"],
        "checked_at": _MCP_CACHE["checked_at"],
        "stale_when_served": stale,
    })


async def api_auto_sfa_run(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

    values = dict(payload)
    if "start" in payload and "start_date" not in values:
        values["start_date"] = payload.get("start")
    if "finish" in payload and "finish_date" not in values:
        values["finish_date"] = payload.get("finish")
    try:
        sfa_request = build_auto_sfa_request(values)
    except AutoSFAValidationError as exc:
        return JSONResponse({"error": "invalid Auto SFA input", "errors": exc.errors},
                            status_code=400)

    try:
        job = await AUTO_SFA_RUNNER.start(sfa_request)
    except RuntimeError as exc:
        active = AUTO_SFA_RUNNER.active_job()
        return JSONResponse(
            {
                "error": str(exc),
                "active_job": active.public_dict() if active else None,
            },
            status_code=409,
        )
    return JSONResponse(job.public_dict(), status_code=202)


# ── Routes: SSE ─────────────────────────────────────────────────────────

async def sse_logs(_request: Request):
    async def stream():
        async for evt in StateReader.tail_logs(from_lines=50):
            yield {"event": "log", "data": json.dumps(evt, ensure_ascii=False, default=str)}
    return EventSourceResponse(stream())


async def sse_logs_watcher(_request: Request):
    """Stream `journalctl --user -u agent-me-watch -f`."""
    async def stream():
        async for evt in tail_journal_unit("agent-me-watch", from_lines=80):
            yield {"event": "log", "data": json.dumps(evt, ensure_ascii=False, default=str)}
    return EventSourceResponse(stream())


async def sse_logs_slack(_request: Request):
    """Stream the bridge.log filtered to user-facing Slack interaction events."""
    async def stream():
        async for evt in tail_bridge_slack_filtered(from_lines=50):
            yield {"event": "log", "data": json.dumps(evt, ensure_ascii=False, default=str)}
    return EventSourceResponse(stream())


async def sse_logs_session(request: Request):
    """Stream a Codex session JSONL trace by session_id."""
    session_id = request.query_params.get("session_id", "").strip()
    if not session_id:
        return JSONResponse({"error": "missing session_id"}, status_code=400)

    async def stream():
        async for evt in tail_session_jsonl(session_id, from_lines=30):
            yield {"event": "log", "data": json.dumps(evt, ensure_ascii=False, default=str)}
    return EventSourceResponse(stream())


async def sse_refresh(request: Request):
    job_id = request.path_params["job_id"]
    async def stream():
        async for evt in RUNNER.subscribe_events(job_id):
            yield {"event": evt.get("event", "message"),
                   "data": json.dumps(evt, ensure_ascii=False, default=str)}
    return EventSourceResponse(stream())


async def sse_auto_sfa(request: Request):
    job_id = request.path_params["job_id"]

    async def stream():
        async for evt in AUTO_SFA_RUNNER.subscribe_events(job_id):
            yield {"event": evt.get("event", "message"),
                   "data": json.dumps(evt, ensure_ascii=False, default=str)}
    return EventSourceResponse(stream())


# ── Healthz / login ─────────────────────────────────────────────────────

async def healthz(_request: Request):
    return JSONResponse({
        "ok": True,
        "uptime_s": int(time.time() - START_TS),
        "now_ms": int(time.time() * 1000),
    })


async def page_login(request: Request):
    """Tiny login form for browsers that don't have ?t=... handy."""
    return TEMPLATES.TemplateResponse(request, "login.html", {})


async def api_login(request: Request):
    form = await request.form()
    token = (form.get("token") or "").strip()
    expected = os.environ.get("DASHBOARD_TOKEN", "").strip()
    if not expected or token != expected:
        return TEMPLATES.TemplateResponse(request, "login.html",
                                          {"error": "invalid token"},
                                          status_code=401)
    redirect = RedirectResponse("/", status_code=303)
    redirect.set_cookie(
        COOKIE_NAME, issue_cookie_value(),
        max_age=COOKIE_MAX_AGE_S,
        httponly=True, samesite="lax", secure=True,
        path="/",
    )
    return redirect


# ── App factory ─────────────────────────────────────────────────────────

def build_app() -> Starlette:
    routes = [
        Route("/", page_index, name="index"),
        Route("/source/{source_id}", page_source, name="source"),
        Route("/ops", page_ops, name="ops"),
        Route("/logs", page_logs, name="logs"),
        Route("/auto-sfa", page_auto_sfa, name="auto_sfa"),
        Route("/login", page_login, name="login"),
        Route("/api/login", api_login, methods=["POST"], name="api_login"),
        Route("/api/state", api_state, name="api_state"),
        Route("/api/source/{source_id}", api_source, name="api_source"),
        Route("/api/refresh/_all", api_refresh_all, methods=["POST"],
              name="api_refresh_all"),
        Route("/api/refresh/{source_id}", api_refresh, methods=["POST"],
              name="api_refresh"),
        Route("/api/mcp/status", api_mcp_status, name="api_mcp_status"),
        Route("/api/mcp/refresh", api_mcp_refresh, methods=["POST"],
              name="api_mcp_refresh"),
        Route("/api/mcp/auth-refresh", api_mcp_auth_refresh, methods=["POST"],
              name="api_mcp_auth_refresh"),
        Route("/api/auto-sfa/run", api_auto_sfa_run, methods=["POST"],
              name="api_auto_sfa_run"),
        Route("/api/sse/logs", sse_logs, name="sse_logs"),
        Route("/api/sse/logs/watcher", sse_logs_watcher, name="sse_logs_watcher"),
        Route("/api/sse/logs/slack", sse_logs_slack, name="sse_logs_slack"),
        Route("/api/sse/logs/session", sse_logs_session, name="sse_logs_session"),
        Route("/api/sse/refresh/{job_id}", sse_refresh, name="sse_refresh"),
        Route("/api/sse/auto-sfa/{job_id}", sse_auto_sfa, name="sse_auto_sfa"),
        Route("/healthz", healthz, name="healthz"),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    app = Starlette(
        debug=False,
        routes=routes,
        middleware=[Middleware(AuthMiddleware)],
    )
    return app


app = build_app()


# ── Entry point ─────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="agent-me dashboard — read-only web view (Phase 4 draft)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help=(
            "bind host. Default 127.0.0.1 (loopback only). Use 0.0.0.0 when "
            "fronted by a reverse proxy that gates access (e.g. NVIDIA-internal "
            "agent-me.nvidia.com behind VPN — see design/reverse-proxy-config.md). "
            "When binding non-loopback the entry point requires either "
            "DASHBOARD_TOKEN or DASHBOARD_TRUST_NETWORK=1."
        ),
    )
    parser.add_argument("--port", type=int, default=8765,
                        help="bind port (default 8765)")
    parser.add_argument(
        "--forwarded-allow-ips", default=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        help=(
            "Trust X-Forwarded-* headers from these client IPs. Use '*' when "
            "behind a trusted reverse proxy on a private network (e.g. NVIDIA "
            "internal). Defaults to 127.0.0.1, or env FORWARDED_ALLOW_IPS."
        ),
    )
    parser.add_argument("--reload", action="store_true",
                        help="hot-reload on source change (dev only)")
    args = parser.parse_args()

    # Non-loopback bind requires either a token or explicit network-level
    # trust (e.g. behind a VPN-gated reverse proxy).
    if args.host != "127.0.0.1":
        auth_required_for_public_bind()

    log.info(
        "dashboard_start",
        host=args.host, port=args.port,
        forwarded_allow_ips=args.forwarded_allow_ips,
        repo=str(REPO_DIR),
        auth=("token" if os.environ.get("DASHBOARD_TOKEN")
              else ("trust-network" if os.environ.get("DASHBOARD_TRUST_NETWORK") == "1"
                    else "off")),
    )

    uvicorn.run(
        "agent_me.dashboard.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,  # let structlog own stderr
        access_log=False,
        # Honor X-Forwarded-Proto / -For / -Host so request.url.scheme is
        # 'https' when the reverse proxy terminates TLS (matters for cookie
        # `secure=True`, redirect targets, and SSE link generation).
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
