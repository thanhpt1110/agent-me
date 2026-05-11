"""Small file-backed queue for parallel agent execution.

The module is intentionally boring: it owns queue state and deterministic
classification hints. The agent using the `parallel-agent-execution` skill owns
reasoning, batching, subagent prompts, and quality judgment.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

Priority = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
Status = Literal[
    "QUEUED",
    "IN_PROGRESS",
    "PENDING_REVIEW",
    "APPROVED",
    "DONE",
    "FAILED",
    "HUMAN_REQUIRED",
    "BLOCKED",
    "SNOOZED",
]
DispatchClass = Literal["AUTO", "DRAFT", "PREPARE", "PLAYWRIGHT", "BLOCKED", "HUMAN"]

PRIORITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
ACTIONABLE_STATUSES = {"QUEUED", "APPROVED"}
TERMINAL_STATUSES = {"DONE", "FAILED"}
HARD_GATE_TASK_TYPES = {
    "slack-send",
    "email-send",
    "approval",
    "merge",
    "deploy",
    "delete",
}


def resolve_state_dir() -> Path:
    if override := os.environ.get("AGENT_ME_PARALLEL_QUEUE_DIR"):
        return Path(override).expanduser()
    if state := os.environ.get("AGENT_ME_STATE_DIR"):
        return Path(state).expanduser() / "parallel-queue"
    if xdg := os.environ.get("XDG_STATE_HOME"):
        return Path(xdg).expanduser() / "agent-me" / "parallel-queue"
    return Path.home() / ".local" / "state" / "agent-me" / "parallel-queue"


QUEUE_DIR = resolve_state_dir()
QUEUE_PATH = QUEUE_DIR / "queue.json"
HISTORY_PATH = QUEUE_DIR / "history.jsonl"


@dataclass
class QueueItem:
    id: str
    title: str
    description: str
    source: str = "manual"
    project: str = ""
    priority: Priority = "MEDIUM"
    task_type: str = "generic"
    status: Status = "QUEUED"
    skill: str | None = None
    skill_args: dict[str, Any] = field(default_factory=dict)
    deep_links: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    due: str | None = None
    waiting_on: str | None = None
    created_at: str = field(default_factory=lambda: now_iso())
    updated_at: str = field(default_factory=lambda: now_iso())
    started_at: str | None = None
    run_id: str | None = None
    result_artifact: str | None = None


@dataclass
class FilterSpec:
    view: str = "today"
    priorities: list[str] = field(default_factory=list)
    task_types: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    include_in_progress: bool = False


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def make_run_id() -> str:
    return f"parallel-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _empty_queue() -> dict[str, Any]:
    return {"version": 1, "items": []}


def read_queue() -> dict[str, Any]:
    if not QUEUE_PATH.exists():
        return _empty_queue()
    try:
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_queue()
    if not isinstance(data, dict):
        return _empty_queue()
    items = data.get("items")
    if not isinstance(items, list):
        data["items"] = []
    data.setdefault("version", 1)
    return data


def write_queue(data: dict[str, Any]) -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    data["version"] = 1
    fd, tmp_path = tempfile.mkstemp(dir=QUEUE_DIR, prefix=".queue-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, QUEUE_PATH)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def enqueue_item(
    title: str,
    description: str,
    *,
    priority: Priority = "MEDIUM",
    task_type: str = "generic",
    project: str = "",
    source: str = "manual",
    skill: str | None = None,
    due: str | None = None,
    tags: list[str] | None = None,
) -> str:
    item = QueueItem(
        id=f"pq-{uuid.uuid4().hex[:10]}",
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
        project=project,
        source=source,
        skill=skill,
        due=due,
        tags=tags or [],
    )
    data = read_queue()
    data["items"].append(asdict(item))
    write_queue(data)
    return item.id


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _today_eligible(item: dict[str, Any]) -> bool:
    if item.get("status") not in ACTIONABLE_STATUSES:
        return False
    due = _parse_date(item.get("due"))
    if due and due.date() <= datetime.now(UTC).date():
        return True
    priority = str(item.get("priority") or "LOW")
    return priority in {"CRITICAL", "HIGH"}


def _matches_view(item: dict[str, Any], spec: FilterSpec) -> bool:
    status = item.get("status")
    if not spec.include_in_progress and status == "IN_PROGRESS":
        return False
    if spec.view == "all":
        return status not in TERMINAL_STATUSES
    if spec.view == "today":
        return _today_eligible(item)
    if spec.view == "triage":
        return status == "PENDING_REVIEW"
    if spec.view == "waiting":
        return bool(item.get("waiting_on")) or status in {"BLOCKED", "SNOOZED"}
    raise ValueError(f"unsupported view: {spec.view}")


def resolve_filters(spec: FilterSpec, items: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    candidates = items if items is not None else list(read_queue().get("items", []))
    out: list[dict[str, Any]] = []
    for item in candidates:
        if not _matches_view(item, spec):
            continue
        if spec.priorities and item.get("priority") not in spec.priorities:
            continue
        if spec.task_types and item.get("task_type") not in spec.task_types:
            continue
        if spec.projects and item.get("project") not in spec.projects:
            continue
        enriched = dict(item)
        enriched["dispatch_class"] = suggest_dispatch_class(item)
        out.append(enriched)
    out.sort(key=lambda item: (PRIORITY_ORDER.get(item.get("priority", "LOW"), 3), item.get("created_at", "")))
    return out


def suggest_dispatch_class(item: dict[str, Any]) -> DispatchClass:
    status = item.get("status")
    task_type = str(item.get("task_type") or "")
    tags = set(item.get("tags") or [])
    skill = str(item.get("skill") or "")

    if status in {"PENDING_REVIEW", "HUMAN_REQUIRED"}:
        return "HUMAN"
    if item.get("waiting_on") or status in {"BLOCKED", "SNOOZED"}:
        return "BLOCKED"
    if task_type in HARD_GATE_TASK_TYPES or "send" in tags or "external-write" in tags:
        return "DRAFT"
    if "playwright" in skill.lower() or task_type in {"browser", "playwright"}:
        return "PLAYWRIGHT"
    if skill or "auto" in tags:
        return "AUTO"
    if task_type in {"reply", "slack-response", "email-response"}:
        return "DRAFT"
    if task_type in {"decision", "approval"}:
        return "HUMAN"
    return "PREPARE"


def manifest(spec: FilterSpec, *, run_id: str | None = None) -> dict[str, Any]:
    items = resolve_filters(spec)
    return {
        "run_id": run_id or make_run_id(),
        "filter": asdict(spec),
        "count": len(items),
        "items": items,
    }


def claim_items(spec: FilterSpec, *, run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or make_run_id()
    data = read_queue()
    item_ids = {item["id"] for item in resolve_filters(spec, list(data.get("items", [])))}
    claimed: list[dict[str, Any]] = []
    now = now_iso()
    for item in data.get("items", []):
        if item.get("id") not in item_ids:
            continue
        if item.get("status") not in ACTIONABLE_STATUSES | {"HUMAN_REQUIRED"}:
            continue
        item["status"] = "IN_PROGRESS"
        item["started_at"] = now
        item["updated_at"] = now
        item["run_id"] = run_id
        item["dispatch_class"] = suggest_dispatch_class(item)
        claimed.append(dict(item))
    write_queue(data)
    return {"run_id": run_id, "count": len(claimed), "items": claimed}


def aggregate_returns(returns: list[dict[str, Any]], *, run_id: str) -> dict[str, Any]:
    data = read_queue()
    by_id = {item.get("id"): item for item in data.get("items", [])}
    summary: dict[str, Any] = {
        "run_id": run_id,
        "completed": [],
        "failed": [],
        "human_required": [],
        "blocked": [],
        "snoozed": [],
        "drafts": [],
        "missing_item": [],
    }
    now = now_iso()
    for ret in returns:
        item_id = ret.get("item_id")
        item = by_id.get(item_id)
        if not item:
            summary["missing_item"].append({"item_id": item_id})
            continue
        status = ret.get("status", "FAILED")
        artifacts = list(ret.get("artifacts") or [])
        notes = str(ret.get("notes") or "")
        item["updated_at"] = now
        if status == "DONE" and artifacts:
            item["status"] = "DONE"
            item["result_artifact"] = artifacts[0]
            summary["completed"].append({"item_id": item_id, "artifacts": artifacts})
        elif status == "BLOCKED":
            item["status"] = "BLOCKED"
            item["waiting_on"] = ret.get("waiting_on") or notes or "unknown"
            summary["blocked"].append({"item_id": item_id, "waiting_on": item["waiting_on"]})
        elif status == "SNOOZED":
            item["status"] = "SNOOZED"
            item["due"] = ret.get("snoozed_until") or (datetime.now(UTC) + timedelta(days=1)).isoformat()
            summary["snoozed"].append({"item_id": item_id, "until": item["due"]})
        elif status == "HUMAN_REQUIRED":
            item["status"] = "HUMAN_REQUIRED"
            item["notes"] = notes
            summary["human_required"].append({"item_id": item_id, "notes": notes})
        else:
            item["status"] = "FAILED"
            item["result_artifact"] = notes or "subagent failure"
            summary["failed"].append({"item_id": item_id, "reason": item["result_artifact"]})
        for draft in ret.get("drafts") or []:
            summary["drafts"].append({"item_id": item_id, **draft})
    write_queue(data)
    return summary


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _filter_from_args(args: argparse.Namespace) -> FilterSpec:
    return FilterSpec(
        view=args.view,
        priorities=_split_csv(args.priority),
        task_types=_split_csv(args.task_type),
        projects=args.project or [],
        include_in_progress=args.include_in_progress,
    )


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent-me parallel queue")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_filter_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--view", choices=["today", "triage", "waiting", "all"], default="today")
        p.add_argument("--priority")
        p.add_argument("--task-type")
        p.add_argument("--project", action="append")
        p.add_argument("--include-in-progress", action="store_true")

    add = sub.add_parser("add", help="enqueue one item")
    add.add_argument("--title", required=True)
    add.add_argument("--description", required=True)
    add.add_argument("--priority", choices=list(PRIORITY_ORDER), default="MEDIUM")
    add.add_argument("--task-type", default="generic")
    add.add_argument("--project", default="")
    add.add_argument("--source", default="manual")
    add.add_argument("--skill")
    add.add_argument("--due")
    add.add_argument("--tag", action="append", default=[])

    for name in ("plan", "dry-run", "claim"):
        p = sub.add_parser(name)
        add_filter_flags(p)
        p.add_argument("--run-id")

    agg = sub.add_parser("aggregate")
    agg.add_argument("--run-id", required=True)
    agg.add_argument("--returns-file", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "add":
        item_id = enqueue_item(
            args.title,
            args.description,
            priority=args.priority,
            task_type=args.task_type,
            project=args.project,
            source=args.source,
            skill=args.skill,
            due=args.due,
            tags=args.tag,
        )
        _print_json({"item_id": item_id, "queue": str(QUEUE_PATH)})
        return 0
    if args.cmd in {"plan", "dry-run"}:
        _print_json(manifest(_filter_from_args(args), run_id=args.run_id))
        return 0
    if args.cmd == "claim":
        _print_json(claim_items(_filter_from_args(args), run_id=args.run_id))
        return 0
    if args.cmd == "aggregate":
        returns = json.loads(Path(args.returns_file).read_text(encoding="utf-8"))
        if isinstance(returns, dict):
            returns = returns.get("returns", [returns])
        _print_json(aggregate_returns(list(returns), run_id=args.run_id))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
