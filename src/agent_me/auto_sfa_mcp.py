"""MCP endpoint for Auto SFA.

The MCP transport prefers Agent Me bearer tokens created by `/mcp/setup`.
Those tokens resolve to encrypted server-side DevTest credentials. HTTP
Basic auth with direct DevTest credentials remains a temporary fallback.
Tool calls reuse the existing Auto SFA parser/builders and runner; no
agent/LLM is invoked inside this module.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse, Response

from agent_me.auto_sfa import (
    AutoSFAValidationError,
    build_auto_sfa_request,
    build_update_template_request,
    missing_update_template_fields,
    parse_auto_sfa_message,
    parse_update_template_message,
    resolve_destination_folder_id,
)
from agent_me.auto_sfa_mcp_store import credentials_for_bearer_token
from agent_me.dashboard.auto_sfa_runner import AutoSFARunner

AUTO_SFA_MCP_PATH = "/mcp/"
AUTO_SFA_MCP_DEFAULT_HOST = "agent-me.nvidia.com"
AUTO_SFA_MCP_DEFAULT_RELEASE_TYPE = "Linux Release"
AUTO_SFA_MCP_RELEASE_TYPE_SOURCES = {
    "Linux Release": 50722,
    "Release": 47877,
}


@dataclass(frozen=True)
class DevTestCredentials:
    username: str
    password: str


_CURRENT_DEVTEST_CREDENTIALS: contextvars.ContextVar[DevTestCredentials | None] = (
    contextvars.ContextVar("auto_sfa_mcp_devtest_credentials", default=None)
)
_CURRENT_PUBLIC_BASE_URL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "auto_sfa_mcp_public_base_url",
    default=None,
)

MCP_AUTO_SFA_RUNNER = AutoSFARunner(trigger_source="mcp")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _public_base_url() -> str:
    raw = (
        os.environ.get("AUTO_SFA_MCP_PUBLIC_BASE_URL")
        or os.environ.get("DASHBOARD_PUBLIC_BASE_URL")
        or _CURRENT_PUBLIC_BASE_URL.get()
        or f"https://{AUTO_SFA_MCP_DEFAULT_HOST}"
    )
    return raw.rstrip("/")


def _public_base_url_from_scope(scope) -> str | None:
    headers = {
        key.lower(): value.decode("latin-1")
        for key, value in (scope.get("headers") or [])
    }
    host = headers.get(b"host", "").strip()
    if not host:
        server = scope.get("server")
        if isinstance(server, tuple) and server:
            host = str(server[0])
            if len(server) > 1 and server[1] not in (None, 80, 443):
                host = f"{host}:{server[1]}"
    if not host:
        return None
    proto = (
        headers.get(b"x-forwarded-proto", "")
        or headers.get(b"x-forwarded-protocol", "")
        or str(scope.get("scheme") or "http")
    )
    proto = proto.split(",", 1)[0].strip().lower() or "http"
    if proto not in {"http", "https"}:
        proto = "http"
    return f"{proto}://{host}".rstrip("/")


def public_mcp_endpoint_url() -> str:
    return f"{_public_base_url()}{AUTO_SFA_MCP_PATH}"


def auto_sfa_dashboard_url() -> str:
    return f"{_public_base_url()}/auto-sfa"


def auto_sfa_job_url(job_id: str | None) -> str:
    if not job_id:
        return auto_sfa_dashboard_url()
    return f"{auto_sfa_dashboard_url()}?job_id={job_id}"


def _allowed_hosts() -> list[str]:
    raw = os.environ.get("AUTO_SFA_MCP_ALLOWED_HOSTS", "")
    configured = [part.strip() for part in raw.split(",") if part.strip()]
    defaults = [
        AUTO_SFA_MCP_DEFAULT_HOST,
        f"{AUTO_SFA_MCP_DEFAULT_HOST}:*",
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "0.0.0.0",
        "0.0.0.0:*",
        "testserver",
    ]
    return sorted(set(configured + defaults))


def _allowed_origins() -> list[str]:
    raw = os.environ.get("AUTO_SFA_MCP_ALLOWED_ORIGINS", "")
    configured = [part.strip() for part in raw.split(",") if part.strip()]
    defaults = [
        f"https://{AUTO_SFA_MCP_DEFAULT_HOST}",
        f"https://{AUTO_SFA_MCP_DEFAULT_HOST}:*",
        f"http://{AUTO_SFA_MCP_DEFAULT_HOST}",
        f"http://{AUTO_SFA_MCP_DEFAULT_HOST}:*",
        "http://localhost",
        "http://localhost:*",
        "http://127.0.0.1",
        "http://127.0.0.1:*",
    ]
    return sorted(set(configured + defaults))


def _derive_devtest_username(value: str) -> str:
    raw = value.strip()
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    return raw.strip().lower()


def _credentials_from_basic_authorization(value: str) -> DevTestCredentials | None:
    if not value.startswith("Basic "):
        return None
    token = value.removeprefix("Basic ").strip()
    if not token:
        return None
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    raw_username, password = decoded.split(":", 1)
    username = _derive_devtest_username(raw_username)
    if not username or not password:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", username):
        return None
    return DevTestCredentials(username=username, password=password)


def _credentials_from_bearer_authorization(value: str) -> DevTestCredentials | None:
    if not value.startswith("Bearer "):
        return None
    stored = credentials_for_bearer_token(value.removeprefix("Bearer ").strip())
    if stored is None:
        return None
    return DevTestCredentials(username=stored.username, password=stored.password)


class AutoSFAMCPAuthMiddleware:
    """ASGI auth wrapper that exposes DevTest credentials to tools."""

    def __init__(self, app, *, realm: str = "Auto SFA MCP") -> None:
        self.app = app
        self.realm = realm

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        base_token = _CURRENT_PUBLIC_BASE_URL.set(_public_base_url_from_scope(scope))
        authorization = ""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"authorization":
                authorization = value.decode("latin-1")
                break
        credentials = (
            _credentials_from_bearer_authorization(authorization)
            or _credentials_from_basic_authorization(authorization)
        )
        if credentials is None:
            response = JSONResponse(
                {
                    "error": "Agent Me MCP token is required",
                    "detail": (
                        "Open /mcp/setup to create a long-lived Agent Me MCP token. "
                        "DevTest Basic Auth is still accepted as a temporary fallback."
                    ),
                },
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer realm="{self.realm}", '
                        f'Basic realm="{self.realm}", charset="UTF-8"'
                    )
                },
            )
            try:
                await response(scope, receive, send)
            finally:
                _CURRENT_PUBLIC_BASE_URL.reset(base_token)
            return

        token = _CURRENT_DEVTEST_CREDENTIALS.set(credentials)
        try:
            await self.app(scope, receive, send)
        finally:
            _CURRENT_DEVTEST_CREDENTIALS.reset(token)
            _CURRENT_PUBLIC_BASE_URL.reset(base_token)


def _require_credentials() -> DevTestCredentials:
    credentials = _CURRENT_DEVTEST_CREDENTIALS.get()
    if credentials is None:
        raise RuntimeError("DevTest credentials are not available for this MCP request")
    return credentials


def _with_credentials(values: dict[str, Any]) -> dict[str, Any]:
    credentials = _require_credentials()
    normalized = dict(values)
    normalized["use_personal_credentials"] = True
    normalized["use_default_credentials"] = False
    normalized["auth_username"] = credentials.username
    normalized["auth_password"] = credentials.password
    return normalized


def _job_response(job) -> dict[str, Any]:
    public = job.public_dict()
    job_id = public.get("job_id")
    return {
        "status": "started",
        "job": public,
        "job_id": job_id,
        "dashboard_url": auto_sfa_dashboard_url(),
        "job_url": auto_sfa_job_url(job_id),
        "message": (
            "Auto SFA job accepted. Open job_url to watch live terminal "
            "progress in the Auto SFA dashboard. Do not poll MCP tools for "
            "progress unless the user explicitly asks for a one-time status "
            "check and the client can run read-only tools without repeated "
            "approval prompts."
        ),
    }


def _needs_input_response(
    *,
    flow_type: str,
    missing: list[str],
    values: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "needs_input",
        "flow_type": flow_type,
        "plan_mode_required": True,
        "missing_fields": missing,
        "resolved_fields": _safe_values(values),
        "message": (
            "Do not execute yet. Ask the user for the missing fields, then call "
            "this tool again after the user approves the complete plan."
        ),
    }


def _needs_confirmation_response(
    *,
    flow_type: str,
    values: dict[str, Any],
    summary: dict[str, Any],
    confirmation_options: list[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "status": "needs_confirmation",
        "flow_type": flow_type,
        "plan_mode_required": True,
        "resolved_fields": _safe_values(values),
        "summary": summary,
        "confirmation_token": _confirmation_token(
            flow_type=flow_type,
            values=values,
            summary=summary,
        ),
        "message": (
            "Preview only because confirmed=false. If the user approves this "
            "plan, call the same tool with confirmed=true. The confirmation_token "
            "is kept for older clients and is no longer required for execution."
        ),
    }
    if confirmation_options:
        payload["confirmation_options"] = confirmation_options
    return payload


def _confirmation_secret() -> str:
    return (
        os.environ.get("AUTO_SFA_MCP_CONFIRMATION_SECRET")
        or os.environ.get("DASHBOARD_OPERATOR_TOKEN_SECRET")
        or os.environ.get("DASHBOARD_TOKEN")
        or "agent-me-auto-sfa-mcp-dev"
    )


def _confirmation_payload(
    *,
    flow_type: str,
    values: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    return json.dumps(
        {
            "flow_type": flow_type,
            "resolved_fields": _safe_values(values),
            "summary": summary,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _confirmation_token(
    *,
    flow_type: str,
    values: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    payload = _confirmation_payload(flow_type=flow_type, values=values, summary=summary)
    digest = hmac.new(
        _confirmation_secret().encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"v1.{digest}"


def _valid_confirmation_token(
    *,
    flow_type: str,
    values: dict[str, Any],
    summary: dict[str, Any],
    token: str | None,
) -> bool:
    if not token:
        return False
    expected = _confirmation_token(flow_type=flow_type, values=values, summary=summary)
    return hmac.compare_digest(token, expected)


def _invalid_response(flow_type: str, errors: list[str], values: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "invalid_input",
        "flow_type": flow_type,
        "plan_mode_required": True,
        "errors": errors,
        "resolved_fields": _safe_values(values),
        "message": "Correct these values with the user before calling the tool again.",
    }


def _safe_values(values: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(values)
    redacted.pop("auth_password", None)
    if redacted.get("auth_username"):
        redacted["auth_password_set"] = True
    for key in ("use_personal_credentials", "use_default_credentials"):
        redacted.pop(key, None)
    return redacted


def _prompt_has_display_signal(prompt: str | None) -> bool:
    return bool(
        re.search(
            r"(?is)\b(?:display[-_ ]?name|automation[-_ ]?dev[-_ ]?linux|"
            r"dev[-_ ]?display[-_ ]?name|template[-_ ]?owner|owner|for|cho)\b",
            prompt or "",
        )
    )


def _clear_general_prompt_display_name(
    values: dict[str, Any],
    *,
    prompt: str | None,
    explicit_display_name: str | None,
) -> dict[str, Any]:
    if explicit_display_name or not prompt:
        return values
    if "\n" in prompt.strip() or _prompt_has_display_signal(prompt):
        return values
    normalized = dict(values)
    normalized.pop("display_name", None)
    return normalized


def _create_values_from_args(
    *,
    prompt: str | None,
    display_name: str | None,
    folder_id: int | str | None,
    template_ids: str | None,
    template_ids_enabled: bool,
    win_linux: str,
) -> dict[str, Any]:
    existing: dict[str, Any] = {"flow_type": "create"}
    if display_name:
        existing["display_name"] = display_name
    if folder_id not in (None, ""):
        existing["folder_id"] = folder_id
    if template_ids is not None:
        existing["template_ids"] = template_ids
    if template_ids_enabled:
        existing["template_ids_enabled"] = True
    if win_linux:
        existing["win_linux"] = win_linux
    values = parse_update_template_message(prompt, existing) if prompt else existing
    values = _clear_general_prompt_display_name(
        values,
        prompt=prompt,
        explicit_display_name=display_name,
    )
    values["flow_type"] = "create"
    return values


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()


def _normalize_release_type(value: Any) -> str:
    raw = str(value or "").strip()
    compact = re.sub(r"[^a-z0-9]+", "", raw.lower())
    if compact == "release":
        return "Release"
    if compact in {"linuxrelease", "linux"}:
        return "Linux Release"
    return AUTO_SFA_MCP_DEFAULT_RELEASE_TYPE


def _release_source_for_type(release_type: str) -> int:
    return AUTO_SFA_MCP_RELEASE_TYPE_SOURCES[_normalize_release_type(release_type)]


def _release_type_mentioned(text: str | None) -> bool:
    return bool(
        re.search(
            r"(?is)\b(?:release[-_ ]?type|type)\b"
            r"\s*(?:[:=]|\bis\b|\bla\b|\blà\b)?\s*"
            r"(?:linux\s+release|release)\b",
            text or "",
        )
    )


def _source_folder_mentioned(text: str | None) -> bool:
    return bool(
        re.search(
            r"(?is)\b(?:source[-_ ]?folder(?:[-_ ]?id)?|source[-_ ]?fodler(?:[-_ ]?id)?|from[-_ ]?folder(?:[-_ ]?id)?)\b",
            text or "",
        )
    )


def _release_values_from_args(
    *,
    prompt: str | None,
    display_name: str | None,
    url_path: str | None,
    release_type: str | None,
    source_folder_id: int | str | None,
    devtest_folder_id: int | str | None,
    start_date: str | None,
    finish_date: str | None,
    task_ids: str | None,
    task_ids_enabled: bool,
    complexity_level: str,
    log_file_provider: str,
) -> dict[str, Any]:
    existing: dict[str, Any] = {"flow_type": "release"}
    if display_name:
        existing["display_name"] = display_name
    if url_path:
        existing["url_path"] = url_path
    if release_type:
        existing["release_type"] = release_type
    if source_folder_id not in (None, ""):
        existing["source_folder_id"] = source_folder_id
    if devtest_folder_id not in (None, ""):
        existing["devtest_folder_id"] = devtest_folder_id
    if start_date:
        existing["start_date"] = start_date
    if finish_date:
        existing["finish_date"] = finish_date
    if task_ids is not None:
        existing["task_ids"] = task_ids
    if task_ids_enabled:
        existing["task_ids_enabled"] = True
    if complexity_level:
        existing["complexity_level"] = complexity_level
    if log_file_provider:
        existing["log_file_provider"] = log_file_provider

    values = parse_auto_sfa_message(prompt, existing) if prompt else existing
    values = _clear_general_prompt_display_name(
        values,
        prompt=prompt,
        explicit_display_name=display_name,
    )
    values["flow_type"] = "release"

    end_date = _today()
    start = end_date - timedelta(days=7)
    normalized_release_type = _normalize_release_type(
        values.get("release_type") or AUTO_SFA_MCP_DEFAULT_RELEASE_TYPE
    )
    values["release_type"] = normalized_release_type
    if (
        (_release_type_mentioned(prompt) or release_type)
        and not (_source_folder_mentioned(prompt) or source_folder_id not in (None, ""))
    ):
        values["source_folder_id"] = str(_release_source_for_type(normalized_release_type))
        values.pop("devtest_folder_id", None)
    else:
        values.setdefault("source_folder_id", str(_release_source_for_type(normalized_release_type)))
    values.setdefault("start_date", start.isoformat())
    values.setdefault("finish_date", end_date.isoformat())
    return values


def _release_type_explicit(
    *,
    prompt: str | None,
    release_type: str | None,
    source_folder_id: int | str | None,
) -> bool:
    return bool(
        release_type
        or _release_type_mentioned(prompt)
        or source_folder_id not in (None, "")
    )


def _missing_release_fields(values: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not str(values.get("display_name") or "").strip():
        missing.append("display_name")
    if not str(values.get("url_path") or values.get("log_file_base_url") or "").strip():
        missing.append("url_path")
    return missing


async def _resolve_release_destination(values: dict[str, Any]) -> dict[str, Any]:
    normalized = _with_credentials(values)
    if str(normalized.get("devtest_folder_id") or "").strip():
        return normalized
    source_folder_id = int(
        normalized.get("source_folder_id")
        or _release_source_for_type(normalized.get("release_type") or AUTO_SFA_MCP_DEFAULT_RELEASE_TYPE)
    )
    credentials = _require_credentials()
    normalized["devtest_folder_id"] = await resolve_destination_folder_id(
        source_folder_id,
        auth_username=credentials.username,
        auth_password=credentials.password,
    )
    return normalized


TOOL_USAGE_RULES = (
    "Use this tool only when the user has given a concrete Auto SFA request. "
    "If the user asks generally or omits required fields, enter the agent client's "
    "plan/clarification mode and collect the missing fields before calling. "
    "For complete requests, the MCP client's tool approval is the confirmation; "
    "call once with confirmed=true or omit confirmed because it defaults to true. "
    "Include default choices explicitly in tool arguments when possible so the "
    "approval UI shows what will run. "
    "Use confirmed=false only for a preview/dry-run. After a job starts, return "
    "the job_url to the user for live dashboard progress; do not auto-poll MCP "
    "tools for progress because many clients prompt on every tool call. "
    "This server executes deterministic Auto SFA functions directly and never calls another agent."
)


def _new_fastmcp() -> FastMCP:
    return FastMCP(
        "agent-me Auto SFA",
        instructions=(
            "Expose Auto SFA as deterministic tools for external agents. "
            "Authenticate every MCP request with an Agent Me bearer token from "
            "/mcp/setup. Complete tool calls execute after MCP client approval; "
            "incomplete calls return structured clarification responses. Use "
            "the returned job_url for live dashboard progress."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            allowed_hosts=_allowed_hosts(),
            allowed_origins=_allowed_origins(),
        ),
    )


AUTO_SFA_MCP = _new_fastmcp()


@AUTO_SFA_MCP.tool(
    name="create_sfa_tasks",
    title="Create SFA Tasks",
    annotations=ToolAnnotations(
        title="Create SFA Tasks",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
async def create_sfa_tasks(
    prompt: str | None = None,
    display_name: str | None = None,
    folder_id: int | str | None = None,
    template_ids: str | None = None,
    template_ids_enabled: bool = False,
    win_linux: str = "Linux Only",
    confirmed: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Prepare templates for SFA using magic-auto update-template.

    DevTest credentials come from the MCP connection's Agent Me bearer token;
    do not ask for username/password as tool arguments on each call.

    Required business fields: display_name and folder_id. Natural-language
    prompt examples are supported, such as
    `Create SFA Tasks for "Thanh Phan" in folder "494139"`.

    Agent-client rule: if the user's request is general or incomplete,
    use plan/clarification mode and do not call until required fields are
    known. When fields are complete, rely on the MCP client's approval UI and
    call this tool once with confirmed=true, or omit confirmed because true is
    the default. Set confirmed=false only when the client explicitly wants a
    preview/dry-run. After start, show the returned job_url to the user for
    live progress; do not auto-poll MCP tools for progress.
    """

    values = _create_values_from_args(
        prompt=prompt,
        display_name=display_name,
        folder_id=folder_id,
        template_ids=template_ids,
        template_ids_enabled=template_ids_enabled,
        win_linux=win_linux,
    )
    missing = missing_update_template_fields(values)
    if missing:
        return _needs_input_response(flow_type="create", missing=missing, values=values)

    values = _with_credentials(values)
    try:
        request = build_update_template_request(values)
    except AutoSFAValidationError as exc:
        return _invalid_response("create", exc.errors, values)

    summary = {
        "action": "update-template",
        "display_name": request.display_name,
        "folder_id": request.folder_id,
        "template_ids": request.template_ids,
        "win_linux": request.win_linux,
        "devtest_username": request.auth_username,
    }
    if not confirmed:
        return _needs_confirmation_response(
            flow_type="create",
            values=values,
            summary=summary,
            confirmation_options=[
                "Default: Win_Linux = Linux Only.",
                "Alternative: Win_Linux = Windows Only.",
                "Alternative: Win_Linux = Both.",
            ],
        )

    job = await MCP_AUTO_SFA_RUNNER.start(request)
    return _job_response(job)


@AUTO_SFA_MCP.tool(
    name="release_sfa_tasks",
    title="Release SFA Tasks",
    annotations=ToolAnnotations(
        title="Release SFA Tasks",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
async def release_sfa_tasks(
    prompt: str | None = None,
    display_name: str | None = None,
    url_path: str | None = None,
    release_type: str | None = None,
    source_folder_id: int | str | None = None,
    devtest_folder_id: int | str | None = None,
    start_date: str | None = None,
    finish_date: str | None = None,
    task_ids: str | None = None,
    task_ids_enabled: bool = False,
    complexity_level: str = "L2",
    log_file_provider: str = "Manual",
    auto_resolve_destination: bool = True,
    confirmed: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Release existing SFA tasks using magic-auto sfa.

    Also use this tool for user wording such as "auto template",
    "mark template auto", "release template auto", or "auto these templates"
    when the intended Auto SFA action is to run the release/auto flow.
    DevTest credentials come from the MCP connection's Agent Me bearer token;
    do not ask for username/password as tool arguments on each call.

    Required business fields: display_name and url_path. If
    devtest_folder_id is omitted and auto_resolve_destination is true,
    the server resolves the current-cycle destination folder from
    source_folder_id using the caller's DevTest credentials.

    Agent-client rule: if the user's request is general or incomplete,
    use plan/clarification mode and do not call until required fields are
    known. When release_type is not specified, the default is Linux Release;
    include `release_type="Linux Release"` in the tool arguments when possible
    so the MCP approval UI shows the default. Use `release_type="Release"` only
    when the user asks for the Release flow. For a complete request, rely on
    the MCP client's approval UI and call this tool once with confirmed=true,
    or omit confirmed because true is the default. Set confirmed=false only
    when the client explicitly wants a preview/dry-run. After start, show the
    returned job_url to the user for live progress; do not auto-poll MCP tools
    for progress.
    """

    values = _release_values_from_args(
        prompt=prompt,
        display_name=display_name,
        url_path=url_path,
        release_type=release_type,
        source_folder_id=source_folder_id,
        devtest_folder_id=devtest_folder_id,
        start_date=start_date,
        finish_date=finish_date,
        task_ids=task_ids,
        task_ids_enabled=task_ids_enabled,
        complexity_level=complexity_level,
        log_file_provider=log_file_provider,
    )
    missing = _missing_release_fields(values)
    if missing:
        return _needs_input_response(flow_type="release", missing=missing, values=values)

    release_type_was_explicit = _release_type_explicit(
        prompt=prompt,
        release_type=release_type,
        source_folder_id=source_folder_id,
    )
    if not confirmed:
        preview = _safe_values(_with_credentials(values))
        summary = {
            "action": "sfa",
            "display_name": values.get("display_name"),
            "task_ids": values.get("task_ids"),
            "release_type": values.get("release_type"),
            "release_type_explicit": release_type_was_explicit,
            "source_folder_id": values.get("source_folder_id"),
            "devtest_folder_id": values.get("devtest_folder_id") or "auto-resolve on execution",
            "date_range": f"{values.get('start_date')} -> {values.get('finish_date')}",
            "url_path": values.get("url_path"),
            "devtest_username": preview.get("auth_username"),
        }
        return _needs_confirmation_response(
            flow_type="release",
            values=preview,
            summary=summary,
            confirmation_options=[
                "Default if the user did not specify type: Linux Release, source_folder_id 50722.",
                "Alternative: Release, source_folder_id 47877. Call again with release_type='Release' or source_folder_id=47877.",
                "Manual override: provide devtest_folder_id to skip destination auto-resolve.",
            ],
        )

    try:
        if not _truthy(auto_resolve_destination) and not str(
            values.get("devtest_folder_id") or ""
        ).strip():
            return _needs_input_response(
                flow_type="release",
                missing=["devtest_folder_id"],
                values=values,
            )
        values = await _resolve_release_destination(values)
        request = build_auto_sfa_request(values)
    except AutoSFAValidationError as exc:
        return _invalid_response("release", exc.errors, values)
    except Exception as exc:
        return {
            "status": "destination_resolve_failed",
            "flow_type": "release",
            "plan_mode_required": True,
            "resolved_fields": _safe_values(values),
            "error": str(exc)[:600],
            "message": (
                "Could not resolve destination folder. Ask the user to provide "
                "devtest_folder_id manually or fix DevTest credentials."
            ),
        }

    job = await MCP_AUTO_SFA_RUNNER.start(request)
    return _job_response(job)


def create_auto_sfa_mcp() -> FastMCP:
    mcp = _new_fastmcp()
    mcp.add_tool(
        create_sfa_tasks,
        name="create_sfa_tasks",
        title="Create SFA Tasks",
        annotations=ToolAnnotations(
            title="Create SFA Tasks",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        structured_output=True,
    )
    mcp.add_tool(
        release_sfa_tasks,
        name="release_sfa_tasks",
        title="Release SFA Tasks",
        annotations=ToolAnnotations(
            title="Release SFA Tasks",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        structured_output=True,
    )
    return mcp


def auto_sfa_mcp_asgi_app(mcp: FastMCP | None = None):
    server = mcp or AUTO_SFA_MCP
    return AutoSFAMCPAuthMiddleware(server.streamable_http_app())


async def health_response() -> Response:
    return JSONResponse({
        "ok": True,
        "name": "agent-me Auto SFA MCP",
        "endpoint": public_mcp_endpoint_url(),
        "tools": ["create_sfa_tasks", "release_sfa_tasks"],
    })
