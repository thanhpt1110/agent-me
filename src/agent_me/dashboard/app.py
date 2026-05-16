"""Starlette ASGI app for the agent-me dashboard.

Run with `uv run agent-me-dashboard` (the entry point binds via uvicorn).
The app itself is also importable as `agent_me.dashboard.app:app` for
external ASGI runners.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from dotenv import load_dotenv
from itsdangerous import BadSignature, URLSafeSerializer
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from agent_me.auto_sfa import (
    AutoSFAValidationError,
    build_auto_sfa_request,
    build_update_template_request,
    resolve_destination_folder_id,
)
from agent_me.auto_sfa_mcp import (
    auto_sfa_mcp_asgi_app,
    create_auto_sfa_mcp,
    public_mcp_endpoint_url,
)
from agent_me.auto_sfa_mcp_store import (
    create_mcp_token,
    cursor_config_json,
    install_command,
    install_script,
    normalize_devtest_username,
)
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
from agent_me.scripts.daily_brief import (
    BRIEF_TIMEZONE,
    LOCAL_TZ,
    fmt_event_time_full,
    parse_datetime,
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
OPERATOR_ACTION_CODE_HEADER = "x-agent-me-action-code"
OPERATOR_ACTION_TOKEN_HEADER = "x-agent-me-operator-token"

# Cached MCP probe — `codex mcp list` takes ~1s and we don't want
# every page hit to trigger one.
_MCP_CACHE: dict[str, Any] = {"servers": [], "checked_at": 0}
_MCP_CACHE_TTL_S = 30


def _operator_action_code() -> str:
    return os.environ.get("DASHBOARD_OPERATOR_ACTION_CODE", "")


def _operator_token_serializer() -> URLSafeSerializer:
    secret = (
        os.environ.get("DASHBOARD_OPERATOR_TOKEN_SECRET")
        or _operator_action_code()
        or os.environ.get("DASHBOARD_TOKEN")
        or "agent-me-operator-dev"
    )
    return URLSafeSerializer(secret, salt="agent-me-operator-action")


def _issue_operator_token() -> str:
    return _operator_token_serializer().dumps({
        "scope": "operator-action",
        "user": "Thanh Phan",
        "version": 1,
    })


def _operator_token_valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        data = _operator_token_serializer().loads(token)
    except BadSignature:
        return False
    return (
        isinstance(data, dict)
        and data.get("scope") == "operator-action"
        and data.get("version") == 1
    )


def _operator_action_headers_or_denied(request: Request) -> dict[str, str] | JSONResponse:
    if _operator_token_valid(request.headers.get(OPERATOR_ACTION_TOKEN_HEADER)):
        return {}

    configured_code = _operator_action_code()
    submitted_code = request.headers.get(OPERATOR_ACTION_CODE_HEADER) or ""
    if configured_code and secrets.compare_digest(submitted_code, configured_code):
        return {"X-Agent-Me-Operator-Token": _issue_operator_token()}

    if not configured_code:
        return JSONResponse({"error": "operator passcode not configured"}, status_code=403)
    return JSONResponse({"error": "operator passcode required"}, status_code=403)


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


def _ms_to_datetime_label(ms: int | None) -> str:
    if not ms:
        return "—"
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(LOCAL_TZ)
    return f"{dt.strftime('%a %Y-%m-%d %H:%M')} {BRIEF_TIMEZONE}"


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


def _calendar_meeting_time(item: dict[str, Any], source: str) -> str | None:
    if source != "calendar" and item.get("source") != "calendar":
        return None
    if full_time := item.get("meeting_time_full"):
        return str(full_time)
    if meeting_time := item.get("meeting_time"):
        return str(meeting_time)

    extras = item.get("extras") if isinstance(item.get("extras"), dict) else {}
    start = extras.get("start")
    end = extras.get("end")
    meeting_time = fmt_event_time_full(
        start if isinstance(start, str) else None,
        end if isinstance(end, str) else None,
    )
    return meeting_time or None


def _calendar_datetimes(item: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    extras = item.get("extras") if isinstance(item.get("extras"), dict) else {}
    start = extras.get("start")
    end = extras.get("end")
    return (
        parse_datetime(start if isinstance(start, str) else None),
        parse_datetime(end if isinstance(end, str) else None),
    )


def _clock_label(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else ""


def _calendar_meeting_time_display(item: dict[str, Any]) -> str | None:
    start_dt, end_dt = _calendar_datetimes(item)
    if not start_dt:
        return None
    if end_dt and start_dt.date() == end_dt.date():
        return f"{start_dt.strftime('%a %Y-%m-%d')} {start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"
    if end_dt:
        return (
            f"{start_dt.strftime('%a %Y-%m-%d %H:%M')} -> "
            f"{end_dt.strftime('%a %Y-%m-%d %H:%M')}"
        )
    return start_dt.strftime("%a %Y-%m-%d %H:%M")


def _relative_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{max(minutes, 1)}m"
    hours = round(minutes / 60)
    if hours < 48:
        return f"{hours}h"
    return f"{round(hours / 24)}d"


def _calendar_relative_status(item: dict[str, Any]) -> str | None:
    start_dt, end_dt = _calendar_datetimes(item)
    if not start_dt:
        return None
    now = datetime.now(LOCAL_TZ)
    if now < start_dt:
        return f"starts in {_relative_duration((start_dt - now).total_seconds())}"
    if end_dt and now <= end_dt:
        return f"in progress · ends in {_relative_duration((end_dt - now).total_seconds())}"
    anchor = end_dt or start_dt
    return f"ended {_relative_duration((now - anchor).total_seconds())} ago"


def _brief_item_with_ui_fields(item: dict[str, Any], source: str) -> dict[str, Any]:
    enriched = dict(item)
    is_calendar = source == "calendar" or item.get("source") == "calendar"
    if is_calendar:
        enriched.pop("meeting_time_full", None)
        start_dt, end_dt = _calendar_datetimes(item)
        enriched["meeting_start_time"] = _clock_label(start_dt)
        enriched["meeting_end_time"] = _clock_label(end_dt)
        if display_time := _calendar_meeting_time_display(item):
            enriched["meeting_time"] = display_time
            enriched["meeting_time_display"] = display_time
        if relative_status := _calendar_relative_status(item):
            enriched["status"] = relative_status
    elif meeting_time := _calendar_meeting_time(item, source):
        enriched["meeting_time"] = meeting_time
    return enriched


def _brief_item_to_pending_item(item: dict[str, Any], source: str,
                                fetched_at: int) -> dict[str, Any]:
    extras = item.get("extras") if isinstance(item.get("extras"), dict) else {}
    item_id = str(item.get("item_id") or extras.get("id") or "")
    title = str(item.get("title") or "(untitled)")[:220]
    pending = {
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
    is_calendar = source == "calendar" or item.get("source") == "calendar"
    if not is_calendar and (meeting_time := _calendar_meeting_time(item, source)):
        pending["meeting_time"] = meeting_time
    if is_calendar:
        start_dt, end_dt = _calendar_datetimes(item)
        pending["meeting_start_time"] = _clock_label(start_dt)
        pending["meeting_end_time"] = _clock_label(end_dt)
        if display_time := _calendar_meeting_time_display(item):
            pending["meeting_time_display"] = display_time
    return pending


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
            updated_by = snap.updated_by or "cache"
            note = (
                f"Last updated {_ms_to_datetime_label(snap.fetched_at)} "
                f"({_ms_to_human(snap.fetched_at)}) · via {updated_by} · "
                f"{snap.seconds}s fetch"
            )
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
            "updated_by": snap.updated_by,
            "last_update_label": _ms_to_datetime_label(snap.fetched_at),
            "last_update_age": _ms_to_human(snap.fetched_at) if snap.fetched_at else "",
            "error": snap.error,
        })
    return groups


TEMPLATES.env.filters["age"] = _ms_to_human
TEMPLATES.env.filters["datetime_label"] = _ms_to_datetime_label


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
    snapshot = replace(
        snapshot,
        items=[_brief_item_with_ui_fields(item, source_id) for item in snapshot.items],
    )
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


def _auto_sfa_default_source_folder_id() -> str:
    return os.environ.get("AUTO_SFA_DEFAULT_SOURCE_FOLDER_ID", "50722")


def _truthy_payload(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _derive_devtest_auth_username(value: Any) -> str:
    raw = str(value or "").strip()
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    return raw.strip().lower()


def _auto_sfa_credentials_from_payload(
    payload: dict[str, Any],
) -> tuple[str | None, str | None, list[str]]:
    use_default = _truthy_payload(payload.get("use_default_credentials"))
    use_personal = _truthy_payload(payload.get("use_personal_credentials"))
    raw_username = str(payload.get("auth_username") or "").strip()
    raw_password = payload.get("auth_password")
    has_credentials = bool(raw_username or raw_password not in (None, ""))
    if use_default or not (use_personal or has_credentials):
        return None, None, []

    errors: list[str] = []
    auth_username = _derive_devtest_auth_username(raw_username) if raw_username else None
    auth_password = None if raw_password in (None, "") else str(raw_password)
    if not auth_username:
        errors.append("USERNAME is required to resolve destination folder")
    elif not re.match(r"^[A-Za-z0-9._-]+$", auth_username):
        errors.append("USERNAME must be a short DevTest login like thaphan")
    if not auth_password:
        errors.append("PASSWORD is required to resolve destination folder")
    return auth_username, auth_password, errors


def _public_base_url_for_request(request: Request) -> str | None:
    configured_base = (
        os.environ.get("AUTO_SFA_MCP_PUBLIC_BASE_URL")
        or os.environ.get("DASHBOARD_PUBLIC_BASE_URL")
    )
    if configured_base:
        return configured_base.rstrip("/")

    forwarded_host = request.headers.get("x-forwarded-host")
    host = (forwarded_host or request.headers.get("host") or "").split(",", 1)[0].strip()
    if host and not host.startswith("testserver"):
        forwarded_proto = request.headers.get("x-forwarded-proto")
        scheme = (forwarded_proto or request.url.scheme or "https").split(",", 1)[0].strip()
        return f"{scheme}://{host}"

    return None


def _mcp_endpoint_url_for_request(request: Request) -> str:
    public_base = _public_base_url_for_request(request)
    if public_base:
        return f"{public_base}/mcp/"

    return public_mcp_endpoint_url()


def _mcp_setup_base_context(request: Request) -> dict[str, Any]:
    public_base = _public_base_url_for_request(request)
    if public_base is None:
        public_base = _mcp_endpoint_url_for_request(request).removesuffix("/mcp/")
    endpoint = f"{public_base.rstrip('/')}/mcp/"
    return {
        "mcp_endpoint_url": endpoint,
        "setup_url": f"{public_base.rstrip('/')}/mcp/setup",
        "install_url": f"{public_base.rstrip('/')}/mcp/install",
    }


async def page_auto_sfa(request: Request):
    mcp_context = _mcp_setup_base_context(request)
    return TEMPLATES.TemplateResponse(request, "auto_sfa.html", {
        "sources": SOURCES,
        "active_job": None,
        "default_source_folder_id": _auto_sfa_default_source_folder_id(),
        "run_history": AUTO_SFA_RUNNER.recent_history(limit=100),
        "mcp_endpoint_url": mcp_context["mcp_endpoint_url"],
        "mcp_setup_url": mcp_context["setup_url"],
    })


async def page_mcp_setup(request: Request):
    return TEMPLATES.TemplateResponse(request, "mcp_setup.html", {
        **_mcp_setup_base_context(request),
        "username": "",
        "label": "",
    })


async def api_mcp_setup(request: Request):
    form = await request.form()
    username = normalize_devtest_username(str(form.get("username") or ""))
    password = str(form.get("password") or "")
    label = str(form.get("label") or "").strip()
    context = {
        **_mcp_setup_base_context(request),
        "username": username,
        "label": label,
    }
    errors: list[str] = []
    if not username:
        errors.append("DevTest username is required")
    elif not re.fullmatch(r"[A-Za-z0-9._-]+", username):
        errors.append("DevTest username must look like thaphan")
    if not password:
        errors.append("DevTest password is required")
    if errors:
        return TEMPLATES.TemplateResponse(
            request,
            "mcp_setup.html",
            {**context, "errors": errors},
            status_code=400,
        )

    try:
        await resolve_destination_folder_id(
            int(os.environ.get("AUTO_SFA_MCP_VERIFY_SOURCE_FOLDER_ID", "50722")),
            auth_username=username,
            auth_password=password,
            timeout_s=float(os.environ.get("AUTO_SFA_MCP_VERIFY_TIMEOUT_S", "60")),
        )
    except Exception as exc:
        log.warning("auto_sfa_mcp_setup_verify_failed", username=username, err=str(exc))
        return TEMPLATES.TemplateResponse(
            request,
            "mcp_setup.html",
            {
                **context,
                "errors": [
                    "Could not verify DevTest credentials. Check username/password and try again.",
                ],
            },
            status_code=400,
        )

    created = create_mcp_token(username=username, password=password, label=label)
    install_base = (_public_base_url_for_request(request) or "").rstrip("/")
    if not install_base:
        install_base = _mcp_setup_base_context(request)["setup_url"].removesuffix("/mcp/setup")
    endpoint = context["mcp_endpoint_url"]
    return TEMPLATES.TemplateResponse(request, "mcp_setup.html", {
        **context,
        "created": created,
        "mcp_token": created.token,
        "authorization_header": f"Bearer {created.token}",
        "cursor_config": cursor_config_json(endpoint=endpoint, token=created.token),
        "install_command": install_command(base_url=install_base, token=created.token),
        "claude_command": (
            "claude mcp add --transport http --scope user "
            f'--header "Authorization: Bearer {created.token}" '
            f"agent-me {endpoint}"
        ),
        "codex_config": (
            "[mcp_servers.agent-me]\n"
            f'url = "{endpoint}"\n'
            f'http_headers = {{ Authorization = "Bearer {created.token}" }}\n'
        ),
    })


async def mcp_install(request: Request):
    return PlainTextResponse(
        install_script(endpoint=_mcp_endpoint_url_for_request(request)),
        media_type="text/x-shellscript; charset=utf-8",
    )


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
    snap = replace(
        snap,
        items=[_brief_item_with_ui_fields(item, source_id) for item in snap.items],
    )
    return JSONResponse({
        **snap.__dict__,
        "items_count": len(snap.items),
    })


async def api_refresh(request: Request):
    source_id = request.path_params["source_id"]
    if source_id not in SOURCE_IDS:
        return JSONResponse({"error": "unknown source"}, status_code=404)
    operator_headers = _operator_action_headers_or_denied(request)
    if isinstance(operator_headers, JSONResponse):
        return operator_headers

    period = request.query_params.get("period", "day")
    period_days = {"day": 1, "week": 7, "month": 30}.get(period, 1)

    existing = RUNNER.active_job_for(source_id)
    if existing and existing.status in ("pending", "running"):
        return JSONResponse(
            {**existing.public_dict(), "coalesced": True},
            status_code=200,
            headers=operator_headers,
        )
    job = await RUNNER.start(source_id, period_days=period_days)
    return JSONResponse(job.public_dict(), status_code=202, headers=operator_headers)


async def api_refresh_all(request: Request):
    """Fan out a refresh across all sources in parallel.

    Each source goes through the same single-flight lock as a single
    refresh, so if a source is already running we coalesce onto its
    existing job. Returns the list of job descriptors so the browser
    can subscribe to each one.
    """
    operator_headers = _operator_action_headers_or_denied(request)
    if isinstance(operator_headers, JSONResponse):
        return operator_headers

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
    return JSONResponse(
        {"jobs": jobs, "period_days": period_days},
        status_code=202,
        headers=operator_headers,
    )


async def api_mcp_refresh(_request: Request):
    """Force a `codex mcp list` probe, bypassing TTL."""
    servers, checked_at = await check_mcp_health()
    _MCP_CACHE["servers"] = [s.__dict__ for s in servers]
    _MCP_CACHE["checked_at"] = checked_at
    return JSONResponse({
        "servers": _MCP_CACHE["servers"],
        "checked_at": checked_at,
    })


async def api_mcp_auth_refresh(request: Request):
    """Refresh MaaS OAuth tokens, rewrite Codex env exports, then probe MCPs."""
    operator_headers = _operator_action_headers_or_denied(request)
    if isinstance(operator_headers, JSONResponse):
        return operator_headers

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
    }, headers=operator_headers)


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


async def api_auto_sfa_resolve_destination(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

    source_raw = str(payload.get("source_folder_id") or "").strip()
    try:
        source_folder_id = int(source_raw)
        if source_folder_id <= 0:
            raise ValueError
    except ValueError:
        return JSONResponse(
            {"error": "source_folder_id must be a positive integer"},
            status_code=400,
        )

    auth_username, auth_password, credential_errors = _auto_sfa_credentials_from_payload(
        payload
    )
    if credential_errors:
        return JSONResponse(
            {"error": "invalid DevTest credentials", "errors": credential_errors},
            status_code=400,
        )

    try:
        destination_folder_id = await resolve_destination_folder_id(
            source_folder_id,
            auth_username=auth_username,
            auth_password=auth_password,
        )
    except Exception as exc:
        log.warning(
            "auto_sfa_destination_resolve_failed",
            source_folder_id=source_folder_id,
            err=str(exc),
        )
        return JSONResponse({"error": str(exc)}, status_code=502)

    return JSONResponse({
        "source_folder_id": source_folder_id,
        "devtest_folder_id": destination_folder_id,
    })


async def api_auto_sfa_run(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

    values = dict(payload)
    raw_flow_value = values.get("flow_type") or values.get("workflow") or values.get("mode")
    raw_flow = str(raw_flow_value or "").strip().lower()
    create_payload_keys = ("folder_id", "template_folder_id", "template_id", "template_ids")
    release_payload_keys = (
        "devtest_folder_id",
        "destination_folder_id",
        "url_path",
        "start_date",
        "finish_date",
        "start",
        "finish",
        "end",
    )
    if not raw_flow:
        has_create_payload = any(str(values.get(key) or "").strip()
                                 for key in create_payload_keys)
        has_release_payload = any(str(values.get(key) or "").strip()
                                  for key in release_payload_keys)
        raw_flow = "create" if has_create_payload and not has_release_payload else "release"

    if raw_flow in {"create", "create_sfa", "create_sfa_tasks", "update-template", "template"}:
        flow_type = "create"
    elif raw_flow in {"release", "release_sfa", "release_sfa_tasks", "sfa"}:
        flow_type = "release"
    else:
        return JSONResponse({"error": f"unknown Auto SFA flow_type: {raw_flow}"},
                            status_code=400)

    if flow_type == "create":
        if "template_folder_id" in payload and "folder_id" not in values:
            values["folder_id"] = payload.get("template_folder_id")
        if "template_id" in payload and "template_ids" not in values:
            values["template_ids"] = payload.get("template_id")
    else:
        if "source_fodler_id" in payload and "source_folder_id" not in values:
            values["source_folder_id"] = payload.get("source_fodler_id")
        if "source_folder" in payload and "source_folder_id" not in values:
            values["source_folder_id"] = payload.get("source_folder")
        if "destination_folder_id" in payload and "devtest_folder_id" not in values:
            values["devtest_folder_id"] = payload.get("destination_folder_id")
        if "start" in payload and "start_date" not in values:
            values["start_date"] = payload.get("start")
        if "finish" in payload and "finish_date" not in values:
            values["finish_date"] = payload.get("finish")
        if "end" in payload and "finish_date" not in values:
            values["finish_date"] = payload.get("end")
        if (
            _truthy_payload(values.get("auto_resolve_destination"))
            and not str(values.get("devtest_folder_id") or "").strip()
        ):
            source_raw = str(values.get("source_folder_id") or "").strip()
            try:
                source_folder_id = int(source_raw)
                if source_folder_id <= 0:
                    raise ValueError
            except ValueError:
                return JSONResponse(
                    {"error": "source_folder_id must be a positive integer"},
                    status_code=400,
                )
            auth_username, auth_password, credential_errors = (
                _auto_sfa_credentials_from_payload(values)
            )
            if credential_errors:
                return JSONResponse(
                    {"error": "invalid DevTest credentials", "errors": credential_errors},
                    status_code=400,
                )
            try:
                values["devtest_folder_id"] = await resolve_destination_folder_id(
                    source_folder_id,
                    auth_username=auth_username,
                    auth_password=auth_password,
                )
            except Exception as exc:
                log.warning(
                    "auto_sfa_destination_resolve_failed",
                    source_folder_id=source_folder_id,
                    err=str(exc),
                )
                return JSONResponse({"error": str(exc)}, status_code=502)
    try:
        sfa_request = (
            build_update_template_request(values)
            if flow_type == "create"
            else build_auto_sfa_request(values)
        )
    except AutoSFAValidationError as exc:
        return JSONResponse({"error": "invalid Auto SFA input", "errors": exc.errors},
                            status_code=400)

    job = await AUTO_SFA_RUNNER.start(sfa_request)
    return JSONResponse(job.public_dict(), status_code=202)


async def api_auto_sfa_cancel(request: Request):
    job_id = request.path_params["job_id"]
    job = await AUTO_SFA_RUNNER.cancel(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job_id"}, status_code=404)
    return JSONResponse(job.public_dict(), status_code=202)


async def api_auto_sfa_history(_request: Request):
    return JSONResponse({
        "runs": AUTO_SFA_RUNNER.recent_history(limit=100),
        "runs_by_flow": AUTO_SFA_RUNNER.recent_history_by_flow(limit=100),
    })


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
    auto_sfa_mcp = create_auto_sfa_mcp()

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        async with auto_sfa_mcp.session_manager.run():
            yield

    routes = [
        Route("/", page_index, name="index"),
        Route("/source/{source_id}", page_source, name="source"),
        Route("/ops", page_ops, name="ops"),
        Route("/logs", page_logs, name="logs"),
        Route("/auto-sfa", page_auto_sfa, name="auto_sfa"),
        Route("/mcp/setup", page_mcp_setup, methods=["GET"], name="mcp_setup"),
        Route("/mcp/setup", api_mcp_setup, methods=["POST"], name="api_mcp_setup"),
        Route("/mcp/install", mcp_install, methods=["GET"], name="mcp_install"),
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
        Route("/api/auto-sfa/resolve-destination", api_auto_sfa_resolve_destination,
              methods=["POST"], name="api_auto_sfa_resolve_destination"),
        Route("/api/auto-sfa/run", api_auto_sfa_run, methods=["POST"],
              name="api_auto_sfa_run"),
        Route("/api/auto-sfa/{job_id}/cancel", api_auto_sfa_cancel, methods=["POST"],
              name="api_auto_sfa_cancel"),
        Route("/api/auto-sfa/history", api_auto_sfa_history,
              name="api_auto_sfa_history"),
        Route("/api/sse/logs", sse_logs, name="sse_logs"),
        Route("/api/sse/logs/watcher", sse_logs_watcher, name="sse_logs_watcher"),
        Route("/api/sse/logs/slack", sse_logs_slack, name="sse_logs_slack"),
        Route("/api/sse/logs/session", sse_logs_session, name="sse_logs_session"),
        Route("/api/sse/refresh/{job_id}", sse_refresh, name="sse_refresh"),
        Route("/api/sse/auto-sfa/{job_id}", sse_auto_sfa, name="sse_auto_sfa"),
        Route("/healthz", healthz, name="healthz"),
        Mount("/mcp", app=auto_sfa_mcp_asgi_app(auto_sfa_mcp), name="auto_sfa_mcp"),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    app = Starlette(
        debug=False,
        routes=routes,
        middleware=[Middleware(AuthMiddleware)],
        lifespan=lifespan,
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
