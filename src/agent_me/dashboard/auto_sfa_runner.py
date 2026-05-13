"""Auto SFA process runner for the dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

import structlog

from agent_me.auto_sfa import AutoSFARequest, run_auto_sfa
from agent_me.auto_sfa_history import recent_auto_sfa_runs, record_auto_sfa_run
from agent_me.dashboard import state_reader

log = structlog.get_logger("dashboard.auto_sfa")


@dataclass
class AutoSFAJob:
    job_id: str
    started_at: int
    request: AutoSFARequest
    status: str = "pending"
    error: str | None = None
    line_count: int = 0
    seconds: int = 0
    finished_at: int | None = None
    _subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)

    def emit(self, event: dict[str, Any]) -> None:
        for q in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        self._subscribers.append(q)
        return q

    def public_dict(self) -> dict[str, Any]:
        request = self.request.as_input_dict()
        return {
            "job_id": self.job_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "line_count": self.line_count,
            "seconds": self.seconds,
            "error": self.error,
            "request": request,
        }


class AutoSFARunner:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: AutoSFAJob | None = None
        self._jobs: dict[str, AutoSFAJob] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._history_cap = 20

    def active_job(self) -> AutoSFAJob | None:
        if self._active and self._active.status in {"pending", "running"}:
            return self._active
        return None

    def get_job(self, job_id: str) -> AutoSFAJob | None:
        return self._jobs.get(job_id)

    def recent_jobs(self, limit: int = 10) -> list[AutoSFAJob]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)[:limit]

    def recent_history(self, limit: int = 100) -> list[dict[str, Any]]:
        return recent_auto_sfa_runs(state_reader.DB_PATH, limit=limit)

    async def start(self, request: AutoSFARequest) -> AutoSFAJob:
        if active := self.active_job():
            raise RuntimeError(f"Auto SFA job already running: {active.job_id}")

        job = AutoSFAJob(
            job_id=uuid.uuid4().hex[:12],
            started_at=int(time.time() * 1000),
            request=request,
        )
        self._active = job
        self._jobs[job.job_id] = job
        if len(self._jobs) > self._history_cap:
            for old_id in sorted(self._jobs, key=lambda i: self._jobs[i].started_at)[
                : -self._history_cap
            ]:
                self._jobs.pop(old_id, None)

        await asyncio.to_thread(self._record_history, job)
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def _record_history(self, job: AutoSFAJob) -> None:
        record_auto_sfa_run(
            state_reader.DB_PATH,
            run_id=job.job_id,
            triggered_at_ms=job.started_at,
            display_name=job.request.display_name,
            status=job.status,
            trigger_source="dashboard",
            updated_at_ms=job.finished_at or int(time.time() * 1000),
        )

    async def _run(self, job: AutoSFAJob) -> None:
        async with self._lock:
            started = time.monotonic()
            job.status = "running"
            await asyncio.to_thread(self._record_history, job)
            job.emit({"event": "running", **job.public_dict()})
            log.info("auto_sfa_started", job_id=job.job_id, folder_id=job.request.devtest_folder_id)

            async def progress(evt: dict[str, Any]) -> None:
                if evt.get("event") == "line":
                    job.line_count = int(evt.get("line_no") or job.line_count)
                if evt.get("event") in {"done", "error"}:
                    return
                job.emit(evt)

            try:
                await run_auto_sfa(job.request, progress_cb=progress)
                job.status = "done"
                job.seconds = int(time.monotonic() - started)
                job.finished_at = int(time.time() * 1000)
                await asyncio.to_thread(self._record_history, job)
                job.emit({"event": "done", **job.public_dict()})
                log.info(
                    "auto_sfa_done", job_id=job.job_id, seconds=job.seconds, lines=job.line_count
                )
            except Exception as exc:
                job.status = "error"
                job.error = str(exc)[:500]
                job.seconds = int(time.monotonic() - started)
                job.finished_at = int(time.time() * 1000)
                await asyncio.to_thread(self._record_history, job)
                job.emit({"event": "error", **job.public_dict()})
                log.error("auto_sfa_failed", job_id=job.job_id, err=str(exc))
            finally:
                if job.request.auth_password:
                    job.request = replace(job.request, auth_password=None)
                if self._active is job:
                    self._active = None

    async def subscribe_events(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        job = self._jobs.get(job_id)
        if job is None:
            yield {"event": "error", "error": "unknown job_id"}
            return

        if job.status in {"done", "error"}:
            yield {"event": job.status, **job.public_dict()}
            return

        q = job.subscribe()
        yield {"event": "snapshot", **job.public_dict()}
        try:
            while True:
                evt = await asyncio.wait_for(q.get(), timeout=120.0)
                yield evt
                if evt.get("event") in {"done", "error"}:
                    return
        except TimeoutError:
            yield {
                "event": "timeout",
                "note": "no Auto SFA progress events in 120s; job may still be running",
            }
