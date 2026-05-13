"""Shared Auto SFA runner for Slack and dashboard entrypoints."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AUTO_SFA_FIELD_ORDER = (
    "display_name",
    "devtest_folder_id",
    "url_path",
    "start_date",
    "finish_date",
    "task_ids",
)

AUTO_SFA_FIELD_LABELS = {
    "display_name": "display_name",
    "username_email": "username",
    "user_login": "user_login",
    "task_ids": "task_ids",
    "task_ids_enabled": "task_ids_enabled",
    "auth_username": "USERNAME",
    "auth_password": "PASSWORD",
    "devtest_project_id": "devtest_project_id",
    "source_folder_id": "source_folder_id",
    "devtest_folder_id": "destination_folder_id",
    "log_file_provider": "log_file_provider",
    "log_file_base_url": "log_file_base_url",
    "planned_dev_start_date": "planned_dev_start_date",
    "planned_dev_finish_date": "planned_dev_finish_date",
    "actual_dev_start_date": "actual_dev_start_date",
    "actual_dev_finish_date": "actual_dev_finish_date",
    "planned_qa_start_date": "planned_qa_start_date",
    "planned_qa_finish_date": "planned_qa_finish_date",
    "actual_qa_start_date": "actual_qa_start_date",
    "complexity_level": "complexity_level",
    "source_code_path": "source_code_path",
    "code_review_path": "code_review_path",
    "url_path": "url_path",
    "start_date": "start",
    "finish_date": "end",
}

AUTO_SFA_REQUIRED_BASE_FIELDS = (
    "display_name",
    "devtest_folder_id",
    "url_path",
    "start_date",
    "finish_date",
)

AUTO_SFA_DATE_FIELDS = (
    "planned_dev_start_date",
    "planned_dev_finish_date",
    "actual_dev_start_date",
    "actual_dev_finish_date",
    "planned_qa_start_date",
    "planned_qa_finish_date",
    "actual_qa_start_date",
)

AUTO_SFA_URL_FIELDS = (
    "log_file_base_url",
    "source_code_path",
    "code_review_path",
)

AUTO_SFA_COMPLEXITY_LEVELS = {"L0", "L1", "L2", "L3", "L4", "TT"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_SEARCH_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
KEY_VALUE_RE = re.compile(r"^\s*(?:[-*]\s*)?([A-Za-z0-9_\- ]+)\s*[:=]\s*(.*?)\s*$")
BOT_PREFIX_RE = re.compile(r"^\s*(?:<@[A-Z0-9]+>|@agent-me)\s*", re.IGNORECASE)
SLACK_LINK_RE = re.compile(r"^<(?P<url>https?://[^>|]+)(?:\|[^>]+)?>$")
INLINE_KEY_RE = re.compile(
    r"(?im)(^|[\n,;])\s*(?:[-*]\s*)?"
    r"(?P<key>"
    r"display[-_ ]?name|automation[-_ ]?dev[-_ ]?linux|dev[-_ ]?display[-_ ]?name|"
    r"username[-_ ]?email|user[-_ ]?email|email|"
    r"user[-_ ]?login|devtest[-_ ]?username|username|user|login|owner|task[-_ ]?owner|taskowner|"
    r"task[-_ ]?ids?|tasks|ids|"
    r"auth[-_ ]?username|devtest[-_ ]?auth[-_ ]?username|password|auth[-_ ]?password|"
    r"devtest[-_ ]?project[-_ ]?id|project[-_ ]?id|"
    r"source[-_ ]?folder[-_ ]?id|source[-_ ]?fodler[-_ ]?id|"
    r"pool[-_ ]?folder[-_ ]?id|from[-_ ]?folder[-_ ]?id|"
    r"devtest[-_ ]?folder[-_ ]?id|destination[-_ ]?folder[-_ ]?id|release[-_ ]?folder[-_ ]?id|"
    r"folder[-_ ]?id|folder|devtest[-_ ]?folder|"
    r"log[-_ ]?file[-_ ]?provider|log[-_ ]?provider|provider|"
    r"log[-_ ]?file[-_ ]?base[-_ ]?url|source[-_ ]?code[-_ ]?path|code[-_ ]?review[-_ ]?path|"
    r"url[-_ ]?path|url|log[-_ ]?url|log[-_ ]?link|link|"
    r"planned[-_ ]?dev[-_ ]?start(?:[-_ ]?date)?|planned[-_ ]?dev[-_ ]?finish(?:[-_ ]?date)?|"
    r"actual[-_ ]?dev[-_ ]?start(?:[-_ ]?date)?|actual[-_ ]?dev[-_ ]?finish(?:[-_ ]?date)?|"
    r"planned[-_ ]?qa[-_ ]?start(?:[-_ ]?date)?|planned[-_ ]?qa[-_ ]?finish(?:[-_ ]?date)?|"
    r"actual[-_ ]?qa[-_ ]?start(?:[-_ ]?date)?|"
    r"complexity(?:[-_ ]?level)?|"
    r"start(?:[-_ ]?date)?|finish(?:[-_ ]?date)?|end(?:[-_ ]?date)?"
    r")\s*(?:[:=\uff1a]|\s+l\u00e0\s+|\s+la\s+)\s*",
)


class AutoSFAValidationError(ValueError):
    """Raised when an Auto SFA request is incomplete or invalid."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class AutoSFARequest:
    display_name: str
    devtest_project_id: int
    devtest_folder_id: int
    source_folder_id: int | None
    log_file_provider: str
    log_file_base_url: str | None
    planned_dev_start_date: str
    planned_dev_finish_date: str
    actual_dev_start_date: str
    actual_dev_finish_date: str
    planned_qa_start_date: str
    planned_qa_finish_date: str
    actual_qa_start_date: str
    complexity_level: str
    source_code_path: str
    code_review_path: str
    task_ids: str | None = None
    auth_username: str | None = None
    auth_password: str | None = None

    @property
    def user_login(self) -> str:
        """Compatibility alias for the legacy name of magic-auto's -u flag."""
        return self.display_name

    def as_input_dict(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "devtest_project_id": self.devtest_project_id,
            "source_folder_id": self.source_folder_id,
            "devtest_folder_id": self.devtest_folder_id,
            "log_file_provider": self.log_file_provider,
            "log_file_base_url": self.log_file_base_url,
            "planned_dev_start_date": self.planned_dev_start_date,
            "planned_dev_finish_date": self.planned_dev_finish_date,
            "actual_dev_start_date": self.actual_dev_start_date,
            "actual_dev_finish_date": self.actual_dev_finish_date,
            "planned_qa_start_date": self.planned_qa_start_date,
            "planned_qa_finish_date": self.planned_qa_finish_date,
            "actual_qa_start_date": self.actual_qa_start_date,
            "complexity_level": self.complexity_level,
            "source_code_path": self.source_code_path,
            "code_review_path": self.code_review_path,
            "task_ids": self.task_ids,
            "auth_username": self.auth_username,
            "auth_password_set": bool(self.auth_password),
        }


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


def resolve_magic_auto_repo_dir(repo_dir: str | Path | None = None) -> Path:
    if repo_dir is not None:
        return Path(repo_dir).expanduser()
    env = (
        os.environ.get("AUTO_SFA_REPO_DIR")
        or os.environ.get("MAGIC_AUTO_REPO_DIR")
        or "/localhome/local-thaphan/magic-auto"
    )
    return Path(env).expanduser()


def resolve_uv_bin() -> str:
    if env := os.environ.get("UV_BIN"):
        p = Path(env).expanduser()
        if p.exists():
            return str(p)
    local_bin = Path.home() / ".local" / "bin"
    aug_path = f"{local_bin}:/usr/local/bin:/opt/homebrew/bin:{os.environ.get('PATH', '')}"
    if found := shutil.which("uv", path=aug_path):
        return found
    return "uv"


def auto_sfa_command(request: AutoSFARequest, uv_bin: str | None = None) -> list[str]:
    cmd = [
        uv_bin or resolve_uv_bin(),
        "run",
        "dtoperator.py",
        "sfa",
    ]
    if request.task_ids:
        cmd.extend(["-i", request.task_ids])
    cmd.extend([
        "--user-login",
        request.display_name,
        "-f",
    ])
    return cmd


def canonical_auto_sfa_key(key: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    if normalized in {
        "display_name",
        "automation_dev_linux",
        "dev_display_name",
    }:
        return "display_name"
    if normalized in {
        "username_email",
        "user_email",
        "email",
        "username",
        "user",
        "owner",
        "task_owner",
        "taskowner",
        "user_login",
        "login",
        "devtest_username",
    }:
        return "display_name"
    if normalized in {"task_ids", "task_id", "tasks", "ids"}:
        return "task_ids"
    if normalized in {"auth_username", "devtest_auth_username"}:
        return "auth_username"
    if normalized in {"password", "auth_password"}:
        return "auth_password"
    if normalized in {"devtest_project_id", "project_id"}:
        return "devtest_project_id"
    if normalized in {
        "source_folder_id",
        "source_fodler_id",
        "pool_folder_id",
        "from_folder_id",
    }:
        return "source_folder_id"
    if normalized in {
        "devtest_folder_id",
        "folder_id",
        "folder",
        "devtest_folder",
        "destination_folder_id",
        "release_folder_id",
    }:
        return "devtest_folder_id"
    if normalized in {"log_file_provider", "log_provider", "provider"}:
        return "log_file_provider"
    if normalized == "log_file_base_url":
        return "log_file_base_url"
    if normalized == "source_code_path":
        return "source_code_path"
    if normalized == "code_review_path":
        return "code_review_path"
    if normalized in {
        "planned_dev_start_date",
        "planned_dev_start",
        "planned_devstart",
    }:
        return "planned_dev_start_date"
    if normalized in {
        "planned_dev_finish_date",
        "planned_dev_finish",
        "planned_dev_end_date",
        "planned_dev_end",
    }:
        return "planned_dev_finish_date"
    if normalized in {
        "actual_dev_start_date",
        "actual_dev_start",
        "actual_devstart",
    }:
        return "actual_dev_start_date"
    if normalized in {
        "actual_dev_finish_date",
        "actual_dev_finish",
        "actual_dev_end_date",
        "actual_dev_end",
    }:
        return "actual_dev_finish_date"
    if normalized in {
        "planned_qa_start_date",
        "planned_qa_start",
        "planned_qastart",
    }:
        return "planned_qa_start_date"
    if normalized in {
        "planned_qa_finish_date",
        "planned_qa_finish",
        "planned_qa_end_date",
        "planned_qa_end",
    }:
        return "planned_qa_finish_date"
    if normalized in {
        "actual_qa_start_date",
        "actual_qa_start",
        "actual_qastart",
    }:
        return "actual_qa_start_date"
    if normalized in {"complexity", "complexity_level"}:
        return "complexity_level"
    if normalized in {
        "url_path",
        "url",
        "log_url",
        "log_link",
        "link",
        "log_file_base_url",
        "source_code_path",
        "code_review_path",
    }:
        return "url_path"
    if "start" in normalized:
        return "start_date"
    if "finish" in normalized or "end" in normalized:
        return "finish_date"
    return None


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = stripped[3:]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    lines = stripped.splitlines()
    if lines and lines[0].strip() in {"json", "text", "txt"}:
        lines = lines[1:]
    return "\n".join(lines).strip()


def _clean_auto_sfa_body(text: str | None) -> str:
    body = text or ""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = BOT_PREFIX_RE.sub("", body).strip()
    body = _strip_code_fence(body)
    return BOT_PREFIX_RE.sub("", body).strip()


def _clean_auto_sfa_value(field: str, value: Any) -> str:
    cleaned = str(value or "").strip().strip(",;")
    cleaned = BOT_PREFIX_RE.sub("", cleaned).strip()
    if field in {"url_path", *AUTO_SFA_URL_FIELDS}:
        match = SLACK_LINK_RE.match(cleaned)
        if match:
            cleaned = match.group("url")
    if field in {"start_date", "finish_date", *AUTO_SFA_DATE_FIELDS}:
        match = DATE_SEARCH_RE.search(cleaned)
        if match:
            cleaned = match.group(0)
    return cleaned


def _normalize_provider(value: Any) -> str:
    provider = str(value or "").strip()
    if provider.lower() == "manual":
        return "Manual"
    if provider.lower() == "domino":
        return "Domino"
    return provider


def _derive_auth_username(value: Any) -> str:
    raw = str(value or "").strip()
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    return raw.strip().lower()


def _apply_auto_sfa_shortcuts(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)

    display_name = (
        normalized.get("display_name")
        or normalized.get("username_email")
        or normalized.get("user_login")
        or normalized.get("username")
        or normalized.get("task_owner")
    )
    if display_name:
        normalized["display_name"] = str(display_name).strip()

    auth_username = normalized.get("auth_username")
    if auth_username:
        normalized["auth_username"] = _derive_auth_username(auth_username)

    if "task_ids" in normalized and normalized["task_ids"] is not None:
        normalized["task_ids"] = _clean_auto_sfa_value("task_ids", normalized["task_ids"])

    if url_path := normalized.get("url_path"):
        for field in AUTO_SFA_URL_FIELDS:
            normalized[field] = url_path

    if start_date := normalized.get("start_date"):
        for field in (
            "planned_dev_start_date",
            "actual_dev_start_date",
            "planned_qa_start_date",
            "actual_qa_start_date",
        ):
            normalized[field] = start_date

    if finish_date := normalized.get("finish_date"):
        for field in (
            "planned_dev_finish_date",
            "actual_dev_finish_date",
            "planned_qa_finish_date",
        ):
            normalized[field] = finish_date

    if not normalized.get("devtest_project_id"):
        normalized["devtest_project_id"] = 1074
    if not normalized.get("log_file_provider"):
        normalized["log_file_provider"] = "Manual"
    if not normalized.get("complexity_level"):
        normalized["complexity_level"] = "L2"

    for field in AUTO_SFA_URL_FIELDS:
        if field in normalized and normalized[field] is not None:
            normalized[field] = _clean_auto_sfa_value(field, normalized[field])
    for field in AUTO_SFA_DATE_FIELDS:
        if field in normalized and normalized[field] is not None:
            normalized[field] = _clean_auto_sfa_value(field, normalized[field])
    if "log_file_provider" in normalized:
        normalized["log_file_provider"] = _normalize_provider(normalized["log_file_provider"])
    if "complexity_level" in normalized and normalized["complexity_level"] is not None:
        normalized["complexity_level"] = str(normalized["complexity_level"]).strip().upper()

    return normalized


def _parse_inline_key_values(body: str) -> dict[str, str]:
    matches = list(INLINE_KEY_RE.finditer(body))
    if not matches:
        return {}
    keyed: dict[str, str] = {}
    for idx, match in enumerate(matches):
        canonical = canonical_auto_sfa_key(match.group("key"))
        if canonical is None:
            continue
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        keyed[canonical] = _clean_auto_sfa_value(canonical, body[match.end():end])
    return keyed


def parse_auto_sfa_message(
    text: str | None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse Slack/chat input as keyed fields or ordered values.

    Ordered fallback maps non-empty lines to the compact Slack/dashboard
    fields: username, destination folder, URL path, start, and end.
    """
    values = dict(existing or {})
    body = _clean_auto_sfa_body(text)

    keyed = _parse_inline_key_values(body)
    if keyed:
        values.update(keyed)
        return _apply_auto_sfa_shortcuts(values)

    lines = [line.strip() for line in body.splitlines() if line.strip()]

    ordered: list[str] = []
    for line in lines:
        match = KEY_VALUE_RE.match(line)
        if not match:
            ordered.append(line)
            continue
        canonical = canonical_auto_sfa_key(match.group(1))
        if canonical is None:
            ordered.append(line)
            continue
        values[canonical] = _clean_auto_sfa_value(canonical, match.group(2))

    if ordered:
        missing = [field for field in AUTO_SFA_FIELD_ORDER if not values.get(field)]
        for field, value in zip(missing, ordered, strict=False):
            values[field] = _clean_auto_sfa_value(field, value)
    return _apply_auto_sfa_shortcuts(values)


def missing_auto_sfa_fields(values: dict[str, Any]) -> list[str]:
    normalized = _apply_auto_sfa_shortcuts(values)
    missing: list[str] = []
    for field in AUTO_SFA_REQUIRED_BASE_FIELDS:
        if not str(normalized.get(field) or "").strip():
            missing.append(field)
    return missing


def _valid_ymd(value: str) -> bool:
    if not DATE_RE.match(value):
        return False
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _normalize_task_ids(value: Any) -> tuple[str | None, list[str]]:
    raw = str(value or "").strip()
    if not raw:
        return None, []
    parts = [part for part in re.split(r"[\s,;]+", raw) if part]
    invalid = [part for part in parts if not part.isdigit()]
    if invalid:
        return None, [f"task_ids contains invalid ID(s): {', '.join(invalid)}"]
    normalized = ",".join(str(int(part)) for part in parts)
    return normalized or None, []


def build_auto_sfa_request(values: dict[str, Any]) -> AutoSFARequest:
    values = _apply_auto_sfa_shortcuts(values)
    errors: list[str] = []

    display_name = str(values.get("display_name") or "").strip()
    if not display_name:
        errors.append("display_name is required")
    elif "@" in display_name or re.fullmatch(r"[A-Za-z0-9._-]+", display_name):
        errors.append("display_name must be a DevTest display name like Thanh Phan")

    task_ids_enabled = _truthy(values.get("task_ids_enabled"))
    task_ids, task_id_errors = _normalize_task_ids(values.get("task_ids"))
    errors.extend(task_id_errors)
    if task_ids_enabled and not task_ids and not task_id_errors:
        errors.append("task_ids is required when specific task ID mode is enabled")

    use_default_credentials = _truthy(values.get("use_default_credentials"))
    raw_auth_username = str(values.get("auth_username") or "").strip()
    raw_auth_password = values.get("auth_password")
    use_personal_credentials = _truthy(values.get("use_personal_credentials"))
    auth_username = _derive_auth_username(raw_auth_username) if raw_auth_username else None
    auth_password = None if raw_auth_password in (None, "") else str(raw_auth_password)
    if use_default_credentials:
        auth_username = None
        auth_password = None
    elif use_personal_credentials or auth_username or auth_password:
        if not auth_username:
            errors.append("USERNAME is required when custom DevTest credentials are enabled")
        elif not re.match(r"^[A-Za-z0-9._-]+$", auth_username):
            errors.append("USERNAME must be a short DevTest login like thaphan")
        if not auth_password:
            errors.append("PASSWORD is required when custom DevTest credentials are enabled")
    else:
        auth_username = None
        auth_password = None

    project_raw = str(values.get("devtest_project_id") or "").strip()
    try:
        devtest_project_id = int(project_raw)
        if devtest_project_id <= 0:
            raise ValueError
    except ValueError:
        devtest_project_id = 0
        errors.append("devtest_project_id must be a positive integer")

    has_source_default_toggle = "use_default_source_folder" in values
    use_default_source_folder = _truthy(values.get("use_default_source_folder"))
    source_raw = str(values.get("source_folder_id") or "").strip().lower()
    if use_default_source_folder:
        source_raw = ""
    if source_raw in {"", "none", "null", "default", "use_default", "leave_default"}:
        source_folder_id = None
        if has_source_default_toggle and not use_default_source_folder:
            errors.append("source_folder_id is required when default source folder is disabled")
    else:
        try:
            source_folder_id = int(source_raw)
            if source_folder_id <= 0:
                raise ValueError
        except ValueError:
            source_folder_id = None
            errors.append("source_folder_id must be a positive integer when provided")

    folder_raw = str(values.get("devtest_folder_id") or "").strip()
    try:
        devtest_folder_id = int(folder_raw)
        if devtest_folder_id <= 0:
            raise ValueError
    except ValueError:
        devtest_folder_id = 0
        errors.append("devtest_folder_id must be a positive integer")

    log_file_provider = _normalize_provider(values.get("log_file_provider"))
    if not log_file_provider:
        errors.append("log_file_provider is required")

    log_file_base_url_raw = values.get("log_file_base_url")
    log_file_base_url = _clean_auto_sfa_value("log_file_base_url", str(log_file_base_url_raw or ""))
    if log_file_provider == "Manual" and not log_file_base_url:
        errors.append("log_file_base_url is required when log_file_provider is Manual")
    if log_file_base_url and not log_file_base_url.startswith(("http://", "https://")):
        errors.append("log_file_base_url must start with http:// or https://")

    date_values: dict[str, str] = {}
    for field in AUTO_SFA_DATE_FIELDS:
        date_value = _clean_auto_sfa_value(field, str(values.get(field) or ""))
        date_values[field] = date_value
        if not _valid_ymd(date_value):
            errors.append(f"{field} must use yyyy-MM-dd")

    def _start_after_finish(start_field: str, finish_field: str) -> bool:
        start = date_values.get(start_field, "")
        finish = date_values.get(finish_field, "")
        return _valid_ymd(start) and _valid_ymd(finish) and start > finish

    if _start_after_finish("planned_dev_start_date", "planned_dev_finish_date"):
        errors.append("planned_dev_start_date must be on or before planned_dev_finish_date")
    if _start_after_finish("actual_dev_start_date", "actual_dev_finish_date"):
        errors.append("actual_dev_start_date must be on or before actual_dev_finish_date")
    if _start_after_finish("planned_qa_start_date", "planned_qa_finish_date"):
        errors.append("planned_qa_start_date must be on or before planned_qa_finish_date")

    complexity_level = str(values.get("complexity_level") or "").strip().upper()
    if not complexity_level:
        errors.append("complexity_level is required")
    elif complexity_level not in AUTO_SFA_COMPLEXITY_LEVELS:
        errors.append("complexity_level must be one of L0, L1, L2, L3, L4, TT")

    source_code_path = _clean_auto_sfa_value("source_code_path", str(values.get("source_code_path") or ""))
    if not source_code_path:
        errors.append("source_code_path is required")
    elif not source_code_path.startswith(("http://", "https://")):
        errors.append("source_code_path must start with http:// or https://")

    code_review_path = _clean_auto_sfa_value("code_review_path", str(values.get("code_review_path") or ""))
    if not code_review_path:
        errors.append("code_review_path is required")
    elif not code_review_path.startswith(("http://", "https://")):
        errors.append("code_review_path must start with http:// or https://")

    if errors:
        raise AutoSFAValidationError(errors)

    return AutoSFARequest(
        display_name=display_name,
        devtest_project_id=devtest_project_id,
        devtest_folder_id=devtest_folder_id,
        source_folder_id=source_folder_id,
        log_file_provider=log_file_provider,
        log_file_base_url=log_file_base_url or None,
        planned_dev_start_date=date_values["planned_dev_start_date"],
        planned_dev_finish_date=date_values["planned_dev_finish_date"],
        actual_dev_start_date=date_values["actual_dev_start_date"],
        actual_dev_finish_date=date_values["actual_dev_finish_date"],
        planned_qa_start_date=date_values["planned_qa_start_date"],
        planned_qa_finish_date=date_values["planned_qa_finish_date"],
        actual_qa_start_date=date_values["actual_qa_start_date"],
        complexity_level=complexity_level,
        source_code_path=source_code_path,
        code_review_path=code_review_path,
        task_ids=task_ids,
        auth_username=auth_username,
        auth_password=auth_password,
    )


def update_magic_auto_config(
    request: AutoSFARequest,
    repo_dir: str | Path | None = None,
) -> dict[str, Any]:
    repo = resolve_magic_auto_repo_dir(repo_dir)
    config_path = repo / "configs.json"
    if not config_path.exists():
        raise FileNotFoundError(f"magic-auto config not found: {config_path}")

    data = json.loads(config_path.read_text())
    release_configs = data.setdefault("release_configs", {})

    data["devtest_project_id"] = request.devtest_project_id
    data["devtest_folder_id"] = request.devtest_folder_id
    if request.source_folder_id is not None:
        data["source_folder_id"] = request.source_folder_id
    data["log_file_provider"] = request.log_file_provider
    if request.log_file_base_url:
        data["log_file_base_url"] = request.log_file_base_url
    else:
        data.pop("log_file_base_url", None)

    release_configs.update({
        "planned_dev_start_date": request.planned_dev_start_date,
        "planned_dev_finish_date": request.planned_dev_finish_date,
        "actual_dev_start_date": request.actual_dev_start_date,
        "actual_dev_finish_date": request.actual_dev_finish_date,
        "planned_qa_start_date": request.planned_qa_start_date,
        "planned_qa_finish_date": request.planned_qa_finish_date,
        "actual_qa_start_date": request.actual_qa_start_date,
        "complexity_level": request.complexity_level,
        "source_code_path": request.source_code_path,
        "code_review_path": request.code_review_path,
    })

    tmp_path = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n")
    tmp_path.replace(config_path)
    return {
        "config_path": str(config_path),
        "devtest_project_id": request.devtest_project_id,
        "source_folder_id": data.get("source_folder_id"),
        "devtest_folder_id": request.devtest_folder_id,
        "log_file_provider": request.log_file_provider,
        "log_file_base_url": request.log_file_base_url,
        "planned_dev_start_date": request.planned_dev_start_date,
        "planned_dev_finish_date": request.planned_dev_finish_date,
        "actual_dev_start_date": request.actual_dev_start_date,
        "actual_dev_finish_date": request.actual_dev_finish_date,
        "planned_qa_start_date": request.planned_qa_start_date,
        "planned_qa_finish_date": request.planned_qa_finish_date,
        "actual_qa_start_date": request.actual_qa_start_date,
        "complexity_level": request.complexity_level,
        "source_code_path": request.source_code_path,
        "code_review_path": request.code_review_path,
    }


@contextlib.asynccontextmanager
async def auto_sfa_lock(repo_dir: Path):
    lock_path = repo_dir / ".agent-me-auto-sfa.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fp = lock_path.open("a")
    await asyncio.to_thread(fcntl.flock, lock_fp.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        lock_fp.close()


async def _emit(progress_cb: ProgressCallback | None, event: dict[str, Any]) -> None:
    if progress_cb is None:
        return
    await progress_cb(event)


async def run_auto_sfa(
    request: AutoSFARequest,
    *,
    repo_dir: str | Path | None = None,
    progress_cb: ProgressCallback | None = None,
    timeout_s: float | None = None,
    uv_bin: str | None = None,
) -> int:
    """Update magic-auto config and stream the SFA command output."""
    repo = resolve_magic_auto_repo_dir(repo_dir)
    timeout = timeout_s
    if timeout is None:
        timeout = float(os.environ.get("AUTO_SFA_TIMEOUT_S", 60 * 60))

    async with auto_sfa_lock(repo):
        summary = update_magic_auto_config(request, repo)
        await _emit(progress_cb, {"event": "config_updated", **summary})

        cmd = auto_sfa_command(request, uv_bin=uv_bin)
        await _emit(
            progress_cb,
            {
                "event": "started",
                "cwd": str(repo),
                "command": shlex.join(cmd),
                "display_name": request.display_name,
                "task_ids": request.task_ids,
                "credential_mode": "custom" if request.auth_username else "default",
                "auth_username": request.auth_username,
            },
        )

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if request.auth_username:
            env["DEVTEST_USERNAME"] = request.auth_username
        if request.auth_password:
            env["DEVTEST_PASSWORD"] = request.auth_password
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=16 * 1024 * 1024,
        )

        started = time.monotonic()
        line_no = 0

        async def drain_stdout() -> None:
            nonlocal line_no
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line_no += 1
                await _emit(
                    progress_cb,
                    {
                        "event": "line",
                        "line_no": line_no,
                        "line": raw.decode(errors="replace").rstrip("\n"),
                    },
                )

        try:
            if timeout and timeout > 0:
                async with asyncio.timeout(timeout):
                    await drain_stdout()
                    return_code = await proc.wait()
            else:
                await drain_stdout()
                return_code = await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            seconds = int(time.monotonic() - started)
            await _emit(
                progress_cb,
                {"event": "error", "error": f"Auto SFA timed out after {seconds}s"},
            )
            raise

        seconds = int(time.monotonic() - started)
        terminal_event = "done" if return_code == 0 else "error"
        await _emit(
            progress_cb,
            {
                "event": terminal_event,
                "return_code": return_code,
                "seconds": seconds,
                "line_count": line_no,
            },
        )
        if return_code != 0:
            raise RuntimeError(f"Auto SFA exited with code {return_code}")
        return return_code
