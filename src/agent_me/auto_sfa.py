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
    "username",
    "devtest_folder_id",
    "url_path",
    "start_date",
    "finish_date",
)

AUTO_SFA_FIELD_LABELS = {
    "username": "username",
    "devtest_folder_id": "devtest_folder_id",
    "url_path": "url_path",
    "start_date": "start date",
    "finish_date": "finish date",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY_VALUE_RE = re.compile(r"^\s*(?:[-*]\s*)?([A-Za-z0-9_\- ]+)\s*[:=]\s*(.*?)\s*$")


class AutoSFAValidationError(ValueError):
    """Raised when an Auto SFA request is incomplete or invalid."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class AutoSFARequest:
    username: str
    devtest_folder_id: int
    url_path: str
    start_date: str
    finish_date: str

    def as_input_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "devtest_folder_id": self.devtest_folder_id,
            "url_path": self.url_path,
            "start_date": self.start_date,
            "finish_date": self.finish_date,
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
    return [
        uv_bin or resolve_uv_bin(),
        "run",
        "dtoperator.py",
        "sfa",
        "--task-owner",
        request.username,
        "-f",
    ]


def canonical_auto_sfa_key(key: str) -> str | None:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"username", "user", "owner", "task_owner", "taskowner"}:
        return "username"
    if normalized in {
        "devtest_folder_id",
        "folder_id",
        "folder",
        "devtest_folder",
    }:
        return "devtest_folder_id"
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
    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().endswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_auto_sfa_message(
    text: str | None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse Slack/chat input as keyed fields or ordered values.

    Ordered fallback maps non-empty lines to missing fields in this order:
    username, devtest_folder_id, url_path, start_date, finish_date.
    """
    values = dict(existing or {})
    body = _strip_code_fence(text or "")
    lines = [line.strip() for line in body.splitlines() if line.strip()]

    keyed: dict[str, str] = {}
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
        keyed[canonical] = match.group(2).strip()

    if keyed:
        values.update(keyed)
        return values

    missing = [field for field in AUTO_SFA_FIELD_ORDER if not values.get(field)]
    for field, value in zip(missing, ordered, strict=False):
        values[field] = value.strip()
    return values


def missing_auto_sfa_fields(values: dict[str, Any]) -> list[str]:
    return [field for field in AUTO_SFA_FIELD_ORDER if not str(values.get(field) or "").strip()]


def _valid_ymd(value: str) -> bool:
    if not DATE_RE.match(value):
        return False
    try:
        time.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def build_auto_sfa_request(values: dict[str, Any]) -> AutoSFARequest:
    errors: list[str] = []

    username = str(values.get("username") or "").strip()
    if not username:
        errors.append("username is required")

    folder_raw = str(values.get("devtest_folder_id") or "").strip()
    try:
        devtest_folder_id = int(folder_raw)
        if devtest_folder_id <= 0:
            raise ValueError
    except ValueError:
        devtest_folder_id = 0
        errors.append("devtest_folder_id must be a positive integer")

    url_path = str(values.get("url_path") or "").strip()
    if not (url_path.startswith("http://") or url_path.startswith("https://")):
        errors.append("url_path must start with http:// or https://")

    start_date = str(values.get("start_date") or "").strip()
    if not _valid_ymd(start_date):
        errors.append("start date must use yyyy-MM-dd")

    finish_date = str(values.get("finish_date") or "").strip()
    if not _valid_ymd(finish_date):
        errors.append("finish date must use yyyy-MM-dd")

    if errors:
        raise AutoSFAValidationError(errors)

    return AutoSFARequest(
        username=username,
        devtest_folder_id=devtest_folder_id,
        url_path=url_path,
        start_date=start_date,
        finish_date=finish_date,
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

    data["devtest_folder_id"] = request.devtest_folder_id
    data["log_file_base_url"] = request.url_path
    release_configs["source_code_path"] = request.url_path
    release_configs["code_review_path"] = request.url_path

    for key in list(release_configs):
        lowered = key.lower()
        if "start" in lowered:
            release_configs[key] = request.start_date
        if "finish" in lowered:
            release_configs[key] = request.finish_date

    tmp_path = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n")
    tmp_path.replace(config_path)
    return {
        "config_path": str(config_path),
        "devtest_folder_id": request.devtest_folder_id,
        "log_file_base_url": request.url_path,
        "start_date": request.start_date,
        "finish_date": request.finish_date,
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
                "task_owner": request.username,
            },
        )

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
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
