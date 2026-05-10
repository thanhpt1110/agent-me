"""On-demand single-source brief refresh, callable from the dashboard.

Reuses the fetcher + parser stack from `agent_me.scripts.daily_brief`
(no duplication of prompt logic). For each source, holds an asyncio
lock so concurrent "Refresh" clicks for the same source coalesce
into one running job; clicks for different sources run in parallel.

Each job emits progress events that the SSE endpoint streams to the
browser. After completion the result is persisted to the dashboard
cache (`${STATE_DIR}/dashboard-cache/<source>.json`) so subsequent
page loads see the fresh items even after the dashboard restarts.

Important: this never posts to Slack. Slack is the bridge's
6am-cron job's territory; the dashboard refresh is purely a "show
me the latest in the UI" operation.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import structlog

from agent_me.dashboard.state_reader import SOURCES, StateReader
from agent_me.scripts import daily_brief

log = structlog.get_logger("dashboard.brief")

# Default to "day" window for refresh; UI can override later.
DEFAULT_PERIOD_DAYS = 1


@dataclass
class BriefJob:
    job_id: str
    source: str
    started_at: int  # ms epoch
    status: str = "pending"  # pending | running | done | error
    error: str | None = None
    item_count: int = 0
    seconds: int = 0
    finished_at: int | None = None
    # Progress events broadcast through this queue. Each subscriber
    # gets its own queue (see `subscribe`); the runner fan-outs.
    _subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)

    def emit(self, event: dict[str, Any]) -> None:
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._subscribers.append(q)
        return q

    def public_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source": self.source,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "item_count": self.item_count,
            "seconds": self.seconds,
            "error": self.error,
        }


class BriefRunner:
    """Process-wide singleton — one instance per dashboard process."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {s[0]: asyncio.Lock() for s in SOURCES}
        self._active: dict[str, BriefJob] = {}  # source → currently-running job
        self._jobs: dict[str, BriefJob] = {}    # job_id → job (recent history)
        self._history_cap = 50

    def active_job_for(self, source: str) -> BriefJob | None:
        return self._active.get(source)

    def get_job(self, job_id: str) -> BriefJob | None:
        return self._jobs.get(job_id)

    def recent_jobs(self, limit: int = 20) -> list[BriefJob]:
        return sorted(self._jobs.values(),
                      key=lambda j: j.started_at, reverse=True)[:limit]

    async def start(self, source: str, period_days: int = DEFAULT_PERIOD_DAYS) -> BriefJob:
        """Start a new refresh. Returns the existing job if one is in flight."""
        if source not in self._locks:
            raise ValueError(f"unknown source: {source!r}")

        existing = self._active.get(source)
        if existing and existing.status in ("pending", "running"):
            log.info("refresh_coalesced", source=source, job_id=existing.job_id)
            return existing

        job = BriefJob(
            job_id=uuid.uuid4().hex[:12],
            source=source,
            started_at=int(time.time() * 1000),
            status="pending",
        )
        self._active[source] = job
        self._jobs[job.job_id] = job
        # Trim history
        if len(self._jobs) > self._history_cap:
            for old_id in sorted(self._jobs,
                                 key=lambda i: self._jobs[i].started_at)[: -self._history_cap]:
                self._jobs.pop(old_id, None)

        # Fire and forget — the SSE consumer will tail the queue.
        asyncio.create_task(self._run(job, period_days))
        return job

    async def _run(self, job: BriefJob, period_days: int) -> None:
        async with self._locks[job.source]:
            spec = next((s for s in daily_brief.SOURCES if s.id == job.source), None)
            if spec is None:
                job.status = "error"
                job.error = f"no SourceSpec for {job.source!r}"
                job.finished_at = int(time.time() * 1000)
                job.emit({"event": "error", "error": job.error})
                self._active.pop(job.source, None)
                return

            job.status = "running"
            job.emit({"event": "running", "source": job.source,
                      "started_at": job.started_at})
            log.info("refresh_started", source=job.source,
                     job_id=job.job_id, period_days=period_days)

            try:
                result = await daily_brief.run_subagent(spec, period_days)
                job.seconds = result.seconds
                job.item_count = len(result.items)
                if result.error:
                    job.status = "error"
                    job.error = result.error
                    job.emit({"event": "error", "error": result.error,
                              "seconds": result.seconds})
                else:
                    job.status = "done"
                    payload = {
                        "source": job.source,
                        "items": [i.__dict__ for i in result.items],
                        "error": None,
                        "fetched_at": int(time.time() * 1000),
                        "seconds": result.seconds,
                        "period_days": period_days,
                    }
                    StateReader.write_cache(job.source, payload)
                    job.emit({
                        "event": "done",
                        "item_count": job.item_count,
                        "seconds": job.seconds,
                    })
                    log.info("refresh_done", source=job.source,
                             job_id=job.job_id, item_count=job.item_count,
                             seconds=job.seconds)
            except Exception as exc:
                job.status = "error"
                job.error = str(exc)[:500]
                job.emit({"event": "error", "error": job.error})
                log.error("refresh_failed", source=job.source,
                          job_id=job.job_id, err=str(exc))
            finally:
                job.finished_at = int(time.time() * 1000)
                self._active.pop(job.source, None)

    async def subscribe_events(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Stream events for a job. Replays the terminal state if the job
        is already finished, otherwise tails until it terminates."""
        job = self._jobs.get(job_id)
        if job is None:
            yield {"event": "error", "error": "unknown job_id"}
            return

        if job.status in ("done", "error"):
            yield {"event": job.status, "error": job.error,
                   "item_count": job.item_count, "seconds": job.seconds}
            return

        q = job.subscribe()
        # Emit a snapshot first so the client sees current state
        yield {"event": "snapshot", **job.public_dict()}
        try:
            while True:
                evt = await asyncio.wait_for(q.get(), timeout=120.0)
                yield evt
                if evt.get("event") in ("done", "error"):
                    return
        except asyncio.TimeoutError:
            yield {"event": "timeout",
                   "note": "no progress events in 120s; job may be hung"}
